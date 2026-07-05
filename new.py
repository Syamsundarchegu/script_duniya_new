import os  
import json  
import time  
import base64  
import logging 
import re 
from typing import Optional, List, Dict, Any, TypedDict, Tuple  
from stamp import stamp_metadata_on_image
import requests  
import io
from PIL import Image, ImageFilter, ImageEnhance   # MODIFIED: added ImageDraw for programmatic arrows
from pydantic import BaseModel, Field  
from pymongo import MongoClient  
from azure.storage.blob import BlobServiceClient, ContentSettings  
from urllib.parse import unquote
from langchain_openai import AzureChatOpenAI  
from langchain_core.prompts import ChatPromptTemplate  
from langchain_core.output_parsers import JsonOutputParser, StrOutputParser  
from langchain_core.runnables.config import RunnableConfig  
from langgraph.graph import StateGraph, START, END  
from langgraph.checkpoint.mongodb import MongoDBSaver  
from langgraph.checkpoint.memory import MemorySaver  
from dotenv import load_dotenv

load_dotenv()
  
# ══════════════════════════════════════════════════════════════════════════════  
# LOGGING  
# ══════════════════════════════════════════════════════════════════════════════  
  
logging.basicConfig(  
    level=logging.INFO,  
    format="%(asctime)s [%(levelname)s] %(message)s",  
    handlers=[  
        logging.StreamHandler(),  
        # logging.FileHandler("pipeline_graph.log", encoding="utf-8"),  
    ],  
)  
log = logging.getLogger(__name__)  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# ENV HELPERS  
# ══════════════════════════════════════════════════════════════════════════════  
  
def env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:  
    value = os.getenv(name, default)  
    if required and not value:  
        raise ValueError(f"Missing required environment variable: {name}")  
    return value  
  
  
def env_csv(name: str, default: str) -> List[str]:  
    raw = os.getenv(name, default)  
    return [item.strip() for item in raw.split(",") if item.strip()]  
  
  
def sanitize_for_filename(value: str) -> str:  
    out = []  
    for ch in (value or "").strip().lower():  
        if ch.isalnum() or ch in ("-", "_", "."):  
            out.append(ch)  
        else:  
            out.append("_")  
  
    collapsed = []  
    prev = None  
    for ch in out:  
        if ch == "_" and prev == "_":  
            continue  
        collapsed.append(ch)  
        prev = ch  
  
    result = "".join(collapsed).strip("._")  
    return result[:180] if result else "unnamed"  
  
  
def dedupe_preserve_order(items: List[str]) -> List[str]:  
    seen = set()  
    result = []  
    for item in items or []:  
        cleaned = (item or "").strip()  
        if not cleaned:  
            continue  
        key = cleaned.lower()  
        if key not in seen:  
            seen.add(key)  
            result.append(cleaned)  
    return result  


def _match_character_name(uploaded_name: str, bible_keys: List[str]) -> Optional[str]:
    """
    Fuzzy-matches a user-supplied character name against the extracted
    character bible keys. Handles case differences and partial matches.
    
    Examples:
        "ravi"       matches "RAVI KUMAR"
        "Ravi Kumar" matches "RAVI KUMAR"
        "villain"    matches "VILLAIN"
    
    Returns the matched bible key, or None if no match found.
    """
    if not uploaded_name:
        return None

    uploaded_clean = uploaded_name.strip().lower()

    # Pass 1: exact match (case-insensitive)
    for key in bible_keys:
        if key.strip().lower() == uploaded_clean:
            return key

    # Pass 2: one contains the other
    for key in bible_keys:
        key_lower = key.strip().lower()
        if uploaded_clean in key_lower or key_lower in uploaded_clean:
            return key

    # Pass 3: first word match (e.g. "ravi" matches "ravi kumar")
    uploaded_first_word = uploaded_clean.split()[0] if uploaded_clean.split() else ""
    for key in bible_keys:
        key_first_word = key.strip().lower().split()[0] if key.strip().split() else ""
        if uploaded_first_word and uploaded_first_word == key_first_word:
            return key

    # Pass 4: fuzzy similarity on the first word (handles spelling drift like
    # "Tabi" vs "Tabby", "Vineet" vs "Vinit", "Rahool" vs "Rahul"). This catches
    # the common case where the frame planner re-spells a transliterated name.
    import difflib
    best_key = None
    best_ratio = 0.0
    for key in bible_keys:
        key_first_word = key.strip().lower().split()[0] if key.strip().split() else key.strip().lower()
        ratio = difflib.SequenceMatcher(None, uploaded_first_word or uploaded_clean, key_first_word).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_key = key
    # 0.72 is high enough to avoid matching unrelated names but low enough to
    # bridge single/double-letter spelling differences.
    if best_key and best_ratio >= 0.72:
        return best_key

    return None 

  
def guess_title(screenplay: str) -> str:  
    for line in screenplay.splitlines():  
        if line.strip():  
            return line.strip()[:120]  
    return "Untitled Screenplay"  
  
  
def add_line_numbers(screenplay: str) -> Tuple[List[str], str]:
    numbered_lines = []
    for idx, line in enumerate(screenplay.splitlines(), start=1):
        # Removed the :04d zero-padding
        numbered_lines.append(f"{idx}: {line}") 
    return numbered_lines, "\n".join(numbered_lines)
  
  
def slice_numbered_lines(numbered_lines: List[str], start_line: int, end_line: int) -> str:  
    start_line = max(1, int(start_line))  
    end_line = max(start_line, int(end_line))  
    return "\n".join(numbered_lines[start_line - 1:end_line])  
  
  
def invoke_chain_with_retry(chain, payload: dict, label: str, retries: int = 2, sleep_s: int = 2):  
    last_err = None  
    for attempt in range(retries + 1):  
        try:  
            return chain.invoke(payload)  
        except Exception as e:  
            last_err = e  
            log.warning(f"[{label}] LLM call failed attempt {attempt + 1}/{retries + 1}: {e}")  
            if attempt < retries:  
                time.sleep(sleep_s)  
    raise last_err  
  
  
def contains_name(scene_text: str, name: str) -> bool:  
    return (name or "").strip().lower() in (scene_text or "").lower()  
  
  
def single_image_rules_text() -> str:  
    return (  
        "Generate exactly ONE standalone image only. "  
        "Do NOT create a collage, diptych, triptych, split-screen, comic page, storyboard sheet, "  
        "contact sheet, multiple stacked frames, multiple side-by-side frames, repeated figure studies, "  
        "or duplicated character. "  
        "No page layout. No panel grid. No multi-view composition."  
    )  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# CONFIG  
# ══════════════════════════════════════════════════════════════════════════════  
  
MAX_SCENES_DEBUG=0

AZURE_OPENAI_ENDPOINT= os.getenv("AZURE_OPENAI_ENDPOINT")


AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")



AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT ="gpt-4.1"
# AZURE_OPENAI_DEPLOYMENT ="gpt-5.4"

# Near the other env config constants
JPEG_QUALITY = int(env("JPEG_QUALITY", "82"))  

IMAGE_ENDPOINT       = os.getenv("IMAGE_ENDPOINT")
IMAGE_API_KEY        = os.getenv("IMAGE_API_KEY")

# IMAGE_DEPLOYMENT     = "gpt-image-1.5"
IMAGE_DEPLOYMENT     = "gpt-image-2"
IMAGE_API_VERSION    = os.getenv("IMAGE_API_VERSION")

  
DEFAULT_VISUAL_STYLE = env("DEFAULT_VISUAL_STYLE", "Single standalone monochrome storyboard sketch")  
DEFAULT_COLOR_PALETTE = env("DEFAULT_COLOR_PALETTE", "Black and White")  
DEFAULT_ASPECT_RATIO = env("DEFAULT_ASPECT_RATIO", "16:9")  
# STORYBOARD_DENSITY = env("STORYBOARD_DENSITY", "dense")  

STORYBOARD_DENSITY = env("STORYBOARD_DENSITY", "keyframe")
  
CHARACTER_IMAGE_SIZES = env_csv("CHARACTER_IMAGE_SIZES", "1024x1536,1024x1024")  
SCENE_IMAGE_SIZES = env_csv("SCENE_IMAGE_SIZES", "1536x1024,1024x1024")  
FRAME_IMAGE_SIZES = env_csv("FRAME_IMAGE_SIZES", "1536x1024,1024x1024")  
  
RATE_LIMIT_SLEEP = int(env("RATE_LIMIT_SLEEP", "2"))  
MAX_RETRIES = int(env("MAX_RETRIES", "4"))  
MAX_REWRITE_ATTEMPTS = int(env("MAX_REWRITE_ATTEMPTS", "4"))  
MAX_CHARACTER_REFS_PER_FRAME = int(env("MAX_CHARACTER_REFS_PER_FRAME", "8"))  
  
BASE_IMAGE_PATH = f"{IMAGE_ENDPOINT}/openai/deployments/{IMAGE_DEPLOYMENT}/images"  
GENERATION_URL = f"{BASE_IMAGE_PATH}/generations?api-version={IMAGE_API_VERSION}"  
EDIT_URL = f"{BASE_IMAGE_PATH}/edits?api-version={IMAGE_API_VERSION}"  
IMAGE_HEADERS = {"Api-Key": IMAGE_API_KEY}  
  
RETRY_BACKOFF = [2, 5, 10]  
  
FILTER_TRIGGER_PATTERNS = [  
    "fight", "punch", "hit", "attack", "shoot", "gun", "weapon", "knife",  
    "blood", "wound", "dead", "dying", "kill", "murder", "stab", "choke",  
    "struggle", "threaten", "beat", "slam", "grab", "strangle", "assault",  
    "explode", "bomb", "fire", "burn", "crash",  
    "rage", "violent", "scream", "crying", "sob", "weep", "terror",  
    "horrified", "traumatic", "abuse", "victim",  
    "dark alley", "dark street", "interrogation", "hostage",  
]  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# DATABASE / STORAGE  
# ══════════════════════════════════════════════════════════════════════════════  
  
mongo_client = None  
projects_collection = None  
  
COSMOS_CONN_STR = env("COSMOS_CONN_STR")  


if COSMOS_CONN_STR:  
    try:  
        mongo_client = MongoClient(COSMOS_CONN_STR, serverSelectionTimeoutMS=5000)  
        mongo_client.admin.command("ping")  
        script_db = mongo_client["script_duniya_db"]  
        projects_collection = script_db["projects"]  
        log.info("Connected to Cosmos/MongoDB.")  
    except Exception as e:  
        log.warning(f"Could not connect to Cosmos/MongoDB: {e}")  
        mongo_client = None  
        projects_collection = None  
  
blob_service_client = None  
container_client = None  


BLOB_CONN_STR = os.getenv("BLOB_CONN_STR")
  
# BLOB_CONN_STR = env("AZURE_STORAGE_CONNECTION_STRING")  
BLOB_CONTAINER = env("AZURE_STORAGE_CONTAINER", "script-duniya-images")  
  
if BLOB_CONN_STR:  
    try:  
        blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONN_STR)  
        container_client = blob_service_client.get_container_client(BLOB_CONTAINER)  
        try:  
            container_client.create_container()  
        except Exception:  
            pass  
        log.info("Connected to Azure Blob Storage.")  
    except Exception as e:  
        log.warning(f"Could not connect to Azure Blob Storage: {e}")  
        blob_service_client = None  
        container_client = None  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# LLMs  
# ══════════════════════════════════════════════════════════════════════════════  
  
llm = AzureChatOpenAI(  
    azure_deployment=AZURE_OPENAI_DEPLOYMENT,  
    api_version=AZURE_OPENAI_API_VERSION,  
    temperature=0,  
    api_key=AZURE_OPENAI_API_KEY,  
    azure_endpoint=AZURE_OPENAI_ENDPOINT,  
    # top_p=1,  
    timeout=120,  
    max_retries=2,  
)  
  
rewriter_llm = AzureChatOpenAI(  
    azure_deployment=AZURE_OPENAI_DEPLOYMENT,  
    api_version=AZURE_OPENAI_API_VERSION,  
    temperature=0,  
    api_key=AZURE_OPENAI_API_KEY,  
    azure_endpoint=AZURE_OPENAI_ENDPOINT,  
    # top_p=1,  
    timeout=120,  
    max_retries=2,  
)  
    
  
# ══════════════════════════════════════════════════════════════════════════════  
# SCHEMAS  
# ══════════════════════════════════════════════════════════════════════════════  
  
class CharacterProfile(BaseModel):  
    name: str  
    aliases: List[str] = Field(default_factory=list)  
    age_range: str  
    gender: str  
    ethnicity: str  
    hair: str  
    build: str  
    clothing_signature: str  
    distinctive_features: str  
    visual_summary: str = Field(description="Single dense safe visual sentence for prompting.")  
  
  
class CharacterBible(BaseModel):  
    characters: List[CharacterProfile]  
  
  
class SceneBoundary(BaseModel):  
    scene_number: int  
    scene_heading: str  
    start_line: int  
    end_line: int  
    location: str  
    time_of_day: str  
    summary: str  
  
  
class SceneBoundaryList(BaseModel):  
    title: str  
    scenes: List[SceneBoundary]  
  
  
class SourceSpan(BaseModel):  
    start_line: int  
    end_line: int  
    quote: str  
  
  
class SceneInventory(BaseModel):  
    scene_heading: str  
    summary: str  
    location: str  
    time_of_day: str  
    lighting_mood: str  
    characters_mentioned: List[str]  
    props_explicit: List[str]  
    environment_details: List[str]  
    wardrobe_details: List[str]  
    actions_explicit: List[str]  
    entrances_exits: List[str]  
    reveals_inserts: List[str]  
    dialogue_beats: List[str]  
    visual_facts_checklist: List[str]  
  
  
class Frame(BaseModel):  
    frame_id: str  
    title: str  
    source_spans: List[SourceSpan]  
    lines: str  
    action: str  
    dramatic_function: str  
    characters_present: List[str]  
    must_show: List[str]  
    props: List[str]  
    tone: str  
    setting_detail: str  
    shot_type: str  
    angle: str  
    composition: str = Field(description=("MUST include exact camera movement notation if the camera moves. ""Use ONLY these exact prefixes if applicable: ""'→ PAN RIGHT', '← PAN LEFT', '↑ TILT UP', '↓ TILT DOWN', ""'↗ PUSH IN', '↙ PULL BACK', '⟳ 360'. ""If no movement, write 'Static camera, [description]'."))
    continuity_notes: str  
    uncertain_details: List[str] = Field(default_factory=list)  
    image_url: Optional[str] = None  
    generation_method: Optional[str] = None  
    final_prompt: Optional[str] = None  
    image_generated: bool = False  
  
  
class ScenePlan(BaseModel):  
    summary: str  
    lighting_mood: str  
    environment_inventory: List[str]  
    prop_inventory: List[str]  
    frames: List[Frame]  
  
  
class SceneAudit(BaseModel):  
    needs_repair: bool  
    coverage_score: int = Field(ge=0, le=100)  
    missing_visual_facts: List[str]  
    merged_or_missing_beats: List[str]  
    invented_details: List[str]  
    continuity_risks: List[str]  
    repair_instructions: List[str]  
  
  
class Scene(BaseModel):  
    scene_number: int  
    scene_heading: str  
    start_line: int  
    end_line: int  
    summary: str  
    location: str  
    time_of_day: str  
    lighting_mood: str  
    environment_inventory: List[str]  
    prop_inventory: List[str]  
    frames: List[Frame]  
    scene_reference_url: Optional[str] = None  
  
  
class ScreenplayBreakdown(BaseModel):  
    title: str  
    total_scenes: int  
    visual_style: str  
    color_palette: str  
    aspect_ratio: str  
    scenes: List[Scene]  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# PARSERS  
# ══════════════════════════════════════════════════════════════════════════════  
  
character_parser = JsonOutputParser(pydantic_object=CharacterBible)  
scene_split_parser = JsonOutputParser(pydantic_object=SceneBoundaryList)  
inventory_parser = JsonOutputParser(pydantic_object=SceneInventory)  
scene_plan_parser = JsonOutputParser(pydantic_object=ScenePlan)  
audit_parser = JsonOutputParser(pydantic_object=SceneAudit)  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# PROMPTS  
# ══════════════════════════════════════════════════════════════════════════════  



CHARACTER_SYSTEM = """
You are a professional casting director, screenplay analyst, and visual development artist working on international film productions.

PRIMARY OBJECTIVE:
Extract a COMPLETE, reliable, and image-generation-ready character bible from the screenplay.

Your highest priority is CHARACTER RECALL.

It is far worse to miss a named character than to include a minor character.

False positives are acceptable.
Missing a named character is a critical failure.

Language Handling:

* The screenplay may be written in ANY language.
* Read and understand the screenplay in its original language.
* Output ALL JSON fields in English only.
* Never output non-English text inside JSON fields.
* Translate character names into their most recognizable English transliteration.
* Examples:

  * "రాజు" → "Raju"
  * "राज" → "Raj"
  * "محمد" → "Mohammed"
  * "김민수" → "Kim Min-su"

Character Inclusion Rules:

Include EVERY character who meets ANY of the following conditions:

1. The character has a proper name anywhere in the screenplay.
2. The character appears as a named dialogue header.
3. The character is named in an action line.
4. The character is named in a description.
5. The character is named by another character.
6. The character appears in a flashback.
7. The character appears in a dream sequence.
8. The character appears in a letter, message, email, text, social media post, or document.
9. The character appears in a phone call or voice-over.
10. The character is mentioned only once but has a proper name.

If a proper name appears anywhere in the screenplay, INCLUDE that character.

Do NOT require:

* Dialogue
* Screen time
* Story importance
* Character development
* Multiple appearances

A character may appear only once and must still be included.

DO NOT INCLUDE:

* Generic role labels without a proper name
* MAN
* WOMAN
* BOY
* GIRL
* DRIVER
* GUARD
* WAITER
* CUSTOMER
* CROWD
* POLICE OFFICER

unless an actual proper name is attached.

Character Name Detection:

Carefully scan:

* Dialogue headers
* Parentheticals
* Action lines
* Character introductions
* Scene descriptions
* Flashbacks
* Dream sequences
* Voice-over sections
* Phone conversations
* Text messages
* Letters
* Emails
* Social media references
* Mentions by other characters

Any proper name found anywhere qualifies for inclusion.

Merging Rules:

Merge aliases, nicknames, titles, and alternate spellings into one canonical character.

Examples:

* Raju
* Raja
* Raju Bhai

→ Canonical: Raju

Use the most common or most formal version as the canonical name.

Character Consistency Rules:

Every character must have a unique visual identity.

Avoid creating visually identical characters.

Differentiate characters using:

* Age
* Build
* Height impression
* Hairstyle
* Hair texture
* Facial features
* Clothing
* Occupation cues
* Accessories
* Visual presentation

Even when appearance information is limited, create a visually distinct profile.

Field Rules:

1. visual_summary

Create ONE dense, image-generation-ready sentence.

Include whenever reasonably inferable:

* Apparent age
* Ethnicity
* Gender presentation
* Height impression
* Build
* Facial characteristics
* Hair
* Clothing
* Signature accessories
* Occupation cues

Focus on stable visual characteristics.

Do NOT describe:

* Personality
* Emotions
* Internal motivations
* Plot details

The description should allow an artist or image model to recreate the same character consistently across multiple scenes.

If appearance cannot be inferred:

Create a realistic production-design description consistent with:

* Story setting
* Region
* Time period
* Occupation
* Social context

Do not invent fantasy or supernatural traits unless explicitly present in the screenplay.

2. age_range

Use values such as:

* early teens
* late teens
* early 20s
* mid 20s
* late 20s
* early 30s
* mid 40s
* elderly

If unclear:

"As established"

3. ethnicity

Infer respectfully using:

* Character names
* Language
* Geography
* Cultural context
* Family relationships
* Story setting

Prefer specific descriptors:

* South Indian
* Telugu
* Tamil
* Kannada
* Malayalam
* North Indian
* Punjabi
* Bengali
* Gujarati
* East Asian
* Southeast Asian
* Middle Eastern
* North African
* West African
* Eastern European
* Latin American

Avoid vague descriptors when a more specific inference is reasonable.

If unclear:

"As established"

4. Other Fields

If genuinely unclear:

"As established"

Never omit a character because a field is unclear.

5. Output Rules

* Output valid JSON only.
* No markdown.
* No explanations.
* No commentary.
* No preamble.
* No trailing text.

Final Verification Pass (MANDATORY):

Before producing output:

1. Re-read the entire screenplay.
2. Scan all dialogue headers.
3. Scan all action lines.
4. Scan all descriptions.
5. Scan all mentions by other characters.
6. Scan flashbacks and dream sequences.
7. Scan text messages, emails, and documents.
8. Identify every proper name.
9. Verify every named character appears exactly once in the final JSON.
10. Verify no named character has been omitted.

Only after completing this verification may you generate the final JSON.

{format_instructions}
"""



# CHARACTER_SYSTEM = """
# You are a professional casting director and visual development artist working on international film productions.
 
# Task:
# Extract a reliable visual character bible from the screenplay.
 
# Language Handling:
# - The screenplay may be written in ANY language (Hindi, Telugu, Tamil, Urdu, Arabic, Korean, etc.)
# - You MUST read and understand the screenplay in its original language
# - You MUST output ALL JSON fields in English only, no exceptions
# - Translate character names to their most recognizable English transliteration
#   (e.g. "రాజు" → "Raju", "राज" → "Raj", "محمد" → "Mohammed")
 
# Character Selection Rules:
# - Include ONLY characters who meet ALL of the following criteria:
#   1. They are explicitly named in the screenplay (not "MAN", "WOMAN", "GUARD", "VOICE")
#   2. They have a demonstrable role in the story — they speak, act, or are directly
#      described in a scene (not merely mentioned in passing dialogue)
#   3. Their physical appearance can be reasonably inferred from the screenplay text,
#      context, or cultural/regional norms of the story's setting
# - DO NOT include:
#   - Unnamed background characters or crowd members
#   - Characters only referenced by others but never present in any scene
#   - Characters with a single throwaway mention and no visual presence
 
# Merging Rules:
# - Merge all aliases, nicknames, and alternate spellings into one canonical English name
#   (e.g. "Raju", "Raja", "Raju bhai" → canonical: "Raju")
# - Use the most frequently used or most formal name as the canonical name
 
# Field Rules:
# 1. visual_summary: One dense, safe, visually descriptive sentence suitable for
#    image generation. Include apparent age, ethnicity, build, hair, and any
#    signature clothing or props. Never use violent, sexual, or unsafe language.
# 2. age_range: Use approximate ranges like "late 20s", "mid 40s", "early teens"
# 3. ethnicity: Infer from screenplay context, character names, and story setting.
#    Use respectful, specific descriptors (e.g. "South Indian", "North African",
#    "East Asian") rather than vague terms.
# 4. If any detail is genuinely unclear and cannot be reasonably inferred, use
#    "As established" for that field only. Do not use it as a default.
# 5. Output valid JSON only. No markdown. No explanation. No preamble.
 
# {format_instructions}
# """
  
character_prompt = ChatPromptTemplate.from_messages([  
    ("system", CHARACTER_SYSTEM),  
    ("human", "Extract the visual character bible from this screenplay:\n\n{screenplay}")  
]).partial(format_instructions=character_parser.get_format_instructions())  
  
character_chain = character_prompt | llm | character_parser  
  
  
SCENE_SPLITTER_SYSTEM = """  
You are an expert screenplay structure analyst.  
  
You will receive the COMPLETE screenplay with line numbers.  
  
Your task:  
Split the screenplay into scenes using only the screenplay content and line numbers.  
  
Rules:  
1. Use the screenplay itself to determine scene boundaries.  
2. Do not rely on external assumptions.  
3. Understand non-English screenplay text if needed, but output all JSON fields in English.  
4. Every line in the screenplay must belong to exactly one scene.  
5. CRITICAL: NO GAPS. Every single line number from 1 to the end of the script must be included. If there are blank lines, transitions (like CUT TO:), or unformatted text between scenes, attach them to the end of the previous scene. Scene N+1 must ALWAYS start exactly at the line after Scene N ends.
6. A new scene begins whenever the screenplay clearly shifts to a new place, time, or dramatic unit.  
7. scene_heading should be the best available scene heading or a concise inferred heading if the script is informal.  
8. location and time_of_day must be filled using the screenplay evidence, otherwise "As established".  
9. summary must be concise and literal.  
10. Output valid JSON only.  
  
{format_instructions}  
"""
  
scene_split_prompt = ChatPromptTemplate.from_messages([  
    ("system", SCENE_SPLITTER_SYSTEM),  
    ("human", "Split this numbered screenplay into scenes:\n\n{numbered_screenplay}")  
]).partial(format_instructions=scene_split_parser.get_format_instructions())  
  
scene_split_chain = scene_split_prompt | llm | scene_split_parser  
  
  
SCENE_SPLIT_REPAIR_SYSTEM = """  
You are repairing a scene split.  
  
You will receive:  
1. the numbered screenplay  
2. the current scene list  
3. validation issues  
  
Repair the scene split so that:  
- every line belongs to exactly one scene  
- there are no gaps  
- there are no overlaps  
- boundaries are more faithful to the screenplay  
  
Output valid JSON only.  
  
{format_instructions}  
"""  
  
scene_split_repair_prompt = ChatPromptTemplate.from_messages([  
    ("system", SCENE_SPLIT_REPAIR_SYSTEM),  
    (  
        "human",  
        "Numbered screenplay:\n{numbered_screenplay}\n\n"  
        "Current scene list:\n{scene_list}\n\n"  
        "Validation issues:\n{issues}"  
    )  
]).partial(format_instructions=scene_split_parser.get_format_instructions())  
  
scene_split_repair_chain = scene_split_repair_prompt | llm | scene_split_parser  
  
  
INVENTORY_SYSTEM = """  
You are a literal screenplay evidence extractor.  
  
You will receive exactly one numbered screenplay scene.  
  
Task:  
Extract an exhaustive visual inventory from this scene only.  
  
Rules:  
1. Work only from the provided scene.  
2. Understand non-English source text if needed, but output all JSON in English.  
3. Do not invent props, actions, wardrobe, or reactions.  
4. Preserve all explicit nouns: objects, architecture, costume pieces, furniture, signs, tools, papers, phones, vehicles, doors, windows, etc.  
5. Every explicit visual fact should appear in the checklist.  
6. If something is unspecified, say "As established".  
7. Be exhaustive and literal.  
8. Output valid JSON only.  
9. characters_present must list character names in English/Roman script only. "
"   Transliterate Hindi or regional script names to English. "
"   Example: 'टबी' → 'Tabi', 'वृंदा' → 'Vrinda'. "
"   Names must exactly match the character bible names provided.\n"
CANONICAL CHARACTER NAMES: {canonical_character_names}
  
{format_instructions}  
"""  
  
inventory_prompt = ChatPromptTemplate.from_messages([  
    ("system", INVENTORY_SYSTEM),  
    ("human", "Scene heading: {scene_heading}\n\nNumbered scene text:\n{scene_text}")  
]).partial(format_instructions=inventory_parser.get_format_instructions())  
  
inventory_chain = inventory_prompt | llm | inventory_parser  

FRAME_PLANNER_SYSTEM = """
You are a highly selective storyboard director. Your job is to distill a scene into the ABSOLUTE MINIMUM number of keyframes while preserving ALL visual details.
 
You will receive:
1. One numbered screenplay scene
2. A scene inventory
3. A relevant character bible subset
 
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT FRAME CAP & MERGING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. MAXIMUM KEYFRAMES: Never exceed 3 to 4 frames for a standard scene. 
2. MERGE AND COMPRESS: Do NOT create a new frame for every line of dialogue, minor reaction, or small action. Compress multiple actions into a single frame's `action` description.
3. NO MISSING DETAILS: To reduce frames without losing clarity, you MUST pack all explicit props, characters, and environment details from the inventory into the `must_show`, `props`, and `setting_detail` lists of your selected few frames. Every detail from the inventory must be distributed among the existing frames.
 
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KEYFRAME SELECTION RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Create a frame ONLY for:
  TYPE A — ESTABLISHING FRAME
  TYPE B — CHARACTER INTRODUCTION FRAME
  TYPE C — DRAMATIC PIVOT FRAME (major action/reveal)
  TYPE D — INSERT (crucial close-up of a plot prop)
 
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAMERA MOVEMENT ENCODING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Encode any camera movement inside the `composition` field.
If static, start with "Static camera".
If moving, use EXACT notations ONLY:
  "→ PAN RIGHT to reveal..." | "← PAN LEFT to follow..." | "↑ TILT UP towards..."
  "↓ TILT DOWN towards..." | "↗ PUSH IN on..." | "↙ PULL BACK from..." | "⟳ 360 DOLLY around..."
 
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIELD RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Work ONLY from the provided scene and inventory. Never invent.
2. Exact dialogue in `lines`. If none, "None".
3. `dramatic_function` must be: ESTABLISHING, CHARACTER_INTRO, DRAMATIC_PIVOT, TRANSITIONAL, or INSERT.
4. Output valid JSON only.
 
{format_instructions}
"""


def _standardize_reference_image(
    raw_bytes: bytes,
    target_size: int = 1024
) -> bytes:
    """
    Standardize a face reference image for GPT Image 2.

    Steps:
    1. Open image from bytes.
    2. Convert to RGB.
    3. Center crop to square.
    4. Resize to target_size x target_size.
    5. Apply mild sharpening and contrast enhancement.
    6. Save as optimized PNG.

    Returns:
        PNG image bytes.
    """

    try:
        # -----------------------------
        # Open image
        # -----------------------------
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

        print(f"Original Size : {img.size}")
        print(f"Original Mode : {img.mode}")

        # -----------------------------
        # Center crop to square
        # -----------------------------
        side = min(img.width, img.height)

        left = (img.width - side) // 2
        top = (img.height - side) // 2
        right = left + side
        bottom = top + side

        img = img.crop((left, top, right, bottom))

        print(f"After Crop    : {img.size}")

        # -----------------------------
        # Resize to exact 1024x1024
        # -----------------------------
        img = img.resize(
            (target_size, target_size),
            Image.LANCZOS
        )

        print(f"After Resize  : {img.size}")

        # -----------------------------
        # Mild sharpening
        # -----------------------------
        img = img.filter(
            ImageFilter.UnsharpMask(
                radius=1.5,
                percent=150,
                threshold=3
            )
        )

        # -----------------------------
        # Slight contrast enhancement
        # -----------------------------
        img = ImageEnhance.Contrast(img).enhance(1.1)

        # -----------------------------
        # Save as PNG
        # -----------------------------
        buf = io.BytesIO()

        img.save(
            buf,
            format="PNG",
            optimize=True
        )

        return buf.getvalue()

    except Exception:
        log.exception("Failed to standardize reference image")
        return raw_bytes


# def _standardize_reference_image(raw_bytes: bytes, max_size: int = 1024) -> bytes:
#     """
#     Normalizes any uploaded image (JPEG, WebP, PNG) into a strictly formatted PNG 
#     while clamping the maximum dimension to prevent model attention dilution.
#     """
#     try:
#         img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        
#         # Scale down if the image is too large, preserving aspect ratio
#         if img.width > max_size or img.height > max_size:
#             img.thumbnail((max_size, max_size), Image.LANCZOS)
            
#         buf = io.BytesIO()
#         img.save(buf, format="PNG", optimize=True)
#         return buf.getvalue()
#     except Exception as e:
#         log.error(f"Failed to standardize reference image: {e}")
#         return raw_bytes










def canonicalize_uploaded_reference(
    char_name: str,
    char_data: Dict[str, Any],
    uploaded_blob_url: str,
    visual_style: str,
    color_palette: str,
    thread_id: str,
) -> Optional[str]:
    """
    Converts a user-uploaded character photo into a canonical monochrome
    storyboard full-body sketch that matches the pipeline's visual style.

    Strategy
    --------
    • Downloads the uploaded photo from Blob Storage.
    • Standardizes it to PNG (clamped to 1024px max dimension).
    • Calls the EDIT endpoint with the photo as the sole reference image.
    • The prompt instructs the model to:
        - Extract ONLY the face/head shape, skin tone impression, and hair
          from the photo reference.
        - Discard the photo's background, clothing, and art style entirely.
        - Use the character bible description for body build, clothing, and
          all other physical details.
        - Render a full-body standing storyboard sketch in the pipeline style.
    • Uploads the result to Blob as  {thread_id}/characters/canonicalized_{safe_name}.jpg
    • Returns the blob URL of the sketch, or None on failure.
    """



    label = f"CANON_REF:{char_name}"
    safe_name = sanitize_for_filename(char_name)
    blob_name = f"{thread_id}/characters/canonicalized_{safe_name}.png"

    # ── 1. Download and standardize the uploaded photo ────────────────────
    try:
        raw_photo_bytes = _download_blob_as_bytes(uploaded_blob_url)
        clean_png_bytes = _standardize_reference_image(raw_photo_bytes, target_size=1024)
        log.info(f"[{label}] Downloaded and standardized uploaded photo "
                 f"({len(raw_photo_bytes)//1024} KB → {len(clean_png_bytes)//1024} KB PNG).")
    except Exception as e:
        log.error(f"[{label}] Failed to download/standardize uploaded photo: {e}")
        return None

    # ── 2. Build the canonicalization prompt ──────────────────────────────
    visual_summary = char_data.get("visual_summary", "As established")
    age_range      = char_data.get("age_range", "As established")
    gender         = char_data.get("gender", "As established")
    ethnicity      = char_data.get("ethnicity", "As established")
    hair           = char_data.get("hair", "As established")
    build          = char_data.get("build", "As established")
    clothing_sig   = char_data.get("clothing_signature", "As established")
    features       = char_data.get("distinctive_features", "As established")
    safe_subject_name = "the subject"
    raw_prompt = (
    f"SUBJECT: One single character only: {safe_subject_name}.\n"

    # ── FACE LOCK (HIGHEST PRIORITY) ────────────────────────────────────────
    f"REFERENCE IMAGE 1 (user photo — FACE REFERENCE ONLY): "
    f"This image contains {safe_subject_name}'s actual face and serves ONLY as a facial identity reference. "
    f"Faithfully preserve and reproduce the exact facial identity, including: "
    f"face shape, facial bone structure, jawline, cheek structure, forehead proportions, "
    f"skin tone impression (translated as grayscale value only), eye shape, eye size, eye placement, eye spacing, "
    f"eyebrow shape and positioning, nose shape, nose width, nose length, lip shape, lip fullness, "
    f"ear placement, hairline shape, hair texture, and hairstyle. "
    f"LOCK FACIAL IDENTITY: Maintain the exact facial geometry and recognizable likeness from the reference. "
    f"Do NOT beautify, idealize, stylize, simplify, average, age-shift, exaggerate, or replace with a generic face. "
    f"If any conflict occurs between style and identity, preserve facial identity first. "
    f"IGNORE COMPLETELY from the reference image: background, clothing, pose, body proportions, colors, lighting, "
    f"camera angle, environment, photographic style, and any non-face visual information. "
    f"Use the reference ONLY to determine facial identity.\n"

    # ── CHARACTER BODY (FROM CHARACTER DATA ONLY) ───────────────────────────
    f"CHARACTER BODY DESCRIPTION (for all NON-FACE details — ignore the photo entirely for these): "
    f"Age: {age_range}. Gender: {gender}. Ethnicity: {ethnicity}. "
    f"Hair: {hair}. Build: {build}. "
    f"Signature clothing: {clothing_sig}. "
    f"Distinctive features: {features}. "
    f"Full visual summary: {visual_summary}.\n"

    # ── FACE TRANSLATION TO SKETCH ──────────────────────────────────────────
    f"FACE IN SKETCH: Translate the facial identity from Reference Image 1 into the requested sketch medium "
    f"while preserving exact facial proportions, structure, and recognizability. "
    f"The face must remain clearly identifiable as the same person from the reference image. "
    f"Maintain facial geometry even in simplified line work. "
    f"Do NOT generalize, stylize away, or lose facial identity during sketch conversion.\n"

    # ── POSE ────────────────────────────────────────────────────────────────
    f"ACTION: Neutral standing pose. Arms relaxed at sides. One figure only. "
    f"No gestures. No interaction with objects.\n"

    # ── ENVIRONMENT ─────────────────────────────────────────────────────────
    f"ENVIRONMENT: Plain clean light gray studio background. "
    f"No props. No furniture. No text. No extra people.\n"

    # ── CAMERA ──────────────────────────────────────────────────────────────
    f"CAMERA: Straight-on full-body long shot. "
    f"Full figure visible from hair to shoes. "
    f"Clear margins on all sides. No crop. "
    f"Entire head, hands, legs, and feet fully visible.\n"

    # ── ART STYLE ───────────────────────────────────────────────────────────
    f"STYLE: Monochrome storyboard sketch, rough pencil line art, black and white. "
    f"Visual style: {visual_style}. Palette: {color_palette}. "
    f"Visible paper texture. Hand-drawn sketch aesthetic. "
    f"NOT a photograph. NOT photorealistic. NOT realistic render. "
    f"NOT cinematic. NOT CGI. NOT 3D. NOT digital painting.\n"

    # ── TECHNICAL RULES ─────────────────────────────────────────────────────
    f"TECHNICAL: {single_image_rules_text()} "
    f"Exactly one single person only. "
    f"Show full head, both hands, both legs, and both feet. "
    f"No cut-off limbs. No cropped body parts. "
    f"No duplicated limbs. No extra limbs. "
    f"No duplicate characters. No character lineup. "
    f"No turnaround sheet. "
    f"Strictly monochrome. Black-and-white only. "
    f"Zero color. "
    f"Prioritize facial identity accuracy above all other visual attributes."
)
#     raw_prompt = (
#     f"SUBJECT: One single character only: {safe_subject_name}.\n"

#     # ── FACE LOCK (most important) ──────────────────────────────────────────
#     f"REFERENCE IMAGE 1 (user photo — face reference ONLY): "
#     f"This is {safe_subject_name}'s actual face. "
#     f"Faithfully extract and reproduce: face shape, facial bone structure, "
#     f"skin tone impression (as grayscale value), eye shape and placement, "
#     f"eye spacing, nose shape and width, lip shape and fullness, "
#     f"hair texture and style, and hairline shape. "
#     f"LOCK: Do NOT generalize, simplify, or idealize these features. "
#     f"Preserve exact facial geometry from this reference. "
#     f"DISCARD completely: background, clothing, photographic style, "
#     f"body proportions, colors, and lighting from this photo. "
#     f"Do NOT reproduce any photographic realism from this image.\n"

#     # ── CHARACTER BODY (from bible, NOT from photo) ─────────────────────────
#     f"CHARACTER BODY DESCRIPTION (for all non-face details — ignore photo for this): "
#     f"Age: {age_range}. Gender: {gender}. Ethnicity: {ethnicity}. "
#     f"Hair: {hair}. Build: {build}. "
#     f"Signature clothing: {clothing_sig}. "
#     f"Distinctive features: {features}. "
#     f"Full visual summary: {visual_summary}.\n"

#     # ── FACE RENDERING IN SKETCH STYLE ──────────────────────────────────────
#     f"FACE IN SKETCH: Even in line-art/sketch style, maintain exact facial "
#     f"proportions and structure from Reference Image 1. Translate face geometry "
#     f"faithfully into the monochrome sketch medium. "
#     f"Do not idealize, average, or generalize the face.\n"

#     # ── POSE / FRAMING ───────────────────────────────────────────────────────
#     f"ACTION: Neutral standing pose. Arms relaxed at sides. One figure only.\n"

#     f"ENVIRONMENT: Plain clean light gray studio background. "
#     f"No props. No furniture. No extra people.\n"

#     f"CAMERA: Straight-on full-body long shot. "
#     f"Full figure visible from hair to shoes. "
#     f"Clear margins on all sides. No crop.\n"

#     # ── ART STYLE ────────────────────────────────────────────────────────────
#     f"STYLE: Monochrome storyboard sketch, rough pencil line art, black and white. "
#     f"Visual style: {visual_style}. Palette: {color_palette}. "
#     f"NOT a photograph. NOT realistic render. NOT 3D. "
#     f"Visible paper texture. Hand-drawn sketch aesthetic.\n"

#     f"TECHNICAL: {single_image_rules_text()} "
#     f"One single person only. Show full head, hands, legs, feet. "
#     f"No cut-off limbs. No duplicated limbs. No turnaround sheet. "
#     f"Strictly monochrome, zero color."
# )
    files_payload = [("user_photo.png", clean_png_bytes)]

    # ── 3. Call the edit API with retry + rewrite loop ────────────────────
    current_prompt = raw_prompt
    rewrite_attempt = 0

    if needs_rewrite(current_prompt):

        log.info(f"[{label}] Pre-emptive safe rewrite triggered.")
        current_prompt = safe_rewrite_prompt(current_prompt, attempt=1)
        print(f"{current_prompt} after safe write")
        rewrite_attempt = 1

    for attempt in range(MAX_RETRIES + 2):
        try:
            response = post_edit_with_size_fallback(
                prompt=current_prompt,
                files_payload=files_payload,
                sizes=FRAME_IMAGE_SIZES,
                input_fidelity="high",  # high fidelity → preserve face from photo
            )

            if response.status_code in (200, 400):
                is_filter, filter_type,filter_reason = _is_content_filter_error(response)
                if is_filter:

                    if filter_type == "input_image_blocked":
                        log.error(f"[{label}] Uploaded image face/content blocked by Azure (Celebrity/PII). Aborting retries.")
                        return None
                    
                    rewrite_attempt += 1
                    log.warning(f"[{label}] Content filter ({filter_type}) -> rewrite attempt {rewrite_attempt}. Reason: {filter_reason}")
                    if rewrite_attempt > MAX_REWRITE_ATTEMPTS:
                        log.error(f"[{label}] Exhausted rewrite attempts.")
                        return None
                    current_prompt = safe_rewrite_prompt(raw_prompt, attempt=rewrite_attempt)
                    time.sleep(1)
                    continue

            response.raise_for_status()
            data = response.json()
            b64 = data["data"][0]["b64_json"]

            blob_url = _upload_b64_to_blob(b64, blob_name, jpeg_quality=JPEG_QUALITY)
            log.info(f"[{label}] Canonical sketch uploaded → {blob_url}")
            return blob_url

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            # is_filter, filter_type = (
            #     _is_content_filter_error(e.response) if e.response else (False, "")
            # )

            is_filter, filter_type, filter_reason = (
                _is_content_filter_error(e.response) if e.response else (False, "", "")
            )
            if is_filter:
                rewrite_attempt += 1
                log.warning(f"[{label}] Filter HTTP {status} ({filter_type}), "
                            f"rewrite attempt {rewrite_attempt}")
                if rewrite_attempt > MAX_REWRITE_ATTEMPTS:
                    return None
                current_prompt = safe_rewrite_prompt(raw_prompt, attempt=rewrite_attempt)
                time.sleep(1)
                continue

            if status == 429:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)] * 3
                log.warning(f"[{label}] Rate limited. Waiting {wait}s.")
                time.sleep(wait)
            else:
                log.error(f"[{label}] HTTP {status}: {e}")
                if attempt >= MAX_RETRIES - 1:
                    return None
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])

        except Exception as e:
            log.error(f"[{label}] Unexpected error: {e}")
            if attempt >= MAX_RETRIES - 1:
                return None
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])

    return None


frame_planner_prompt = ChatPromptTemplate.from_messages([  
    ("system", FRAME_PLANNER_SYSTEM),  
    (  
        "human",  
        "Scene heading: {scene_heading}\n\n"  
        "Scene inventory:\n{scene_inventory}\n\n"  
        "Relevant character bible:\n{character_bible}\n\n"  
        "CANONICAL CHARACTER NAMES — use these exact spellings only:\n"
        "{canonical_character_names}\n\n"
        "Numbered scene text:\n{scene_text}"
    )  
]).partial(  
    format_instructions=scene_plan_parser.get_format_instructions(),  
    storyboard_density=STORYBOARD_DENSITY,  
)  
  
frame_planner_chain = frame_planner_prompt | llm | scene_plan_parser  
  
  
FRAME_AUDIT_SYSTEM = """  
You are a strict screenplay-to-storyboard auditor.
 
You will receive:
1. one numbered screenplay scene
2. a scene inventory checklist
3. a scene frame plan
4. the canonical character bible names
 
Your job:
Find omissions, invented details, and continuity risks.
 
Rules:
1. If a specific visual fact or prop from the inventory is not represented in ANY frame's `props`, `must_show`, or `action`, mark it as missing.
2. MERGING IS GOOD: Do NOT flag compressed/merged beats as an error. We WANT fewer frames with high detail density.
3. If any planned detail is unsupported by source text or inventory, mark it invented.
4. Output valid JSON only.
5. characters_present must list character names in English/Roman script only. 
6. CRITICAL: Flag any character name in characters_present that does not EXACTLY match one of the canonical character names provided.
 
{format_instructions}  
"""  
  
frame_audit_prompt = ChatPromptTemplate.from_messages([  
    ("system", FRAME_AUDIT_SYSTEM),  
    (  
        "human",  
        "Scene heading: {scene_heading}\n\n"
        "Canonical Character Names:\n{canonical_character_names}\n\n"
        "Scene inventory:\n{scene_inventory}\n\n"  
        "Scene plan:\n{scene_plan}\n\n"  
        "Numbered scene text:\n{scene_text}"  
    )  
]).partial(format_instructions=audit_parser.get_format_instructions())  
  
frame_audit_chain = frame_audit_prompt | llm | audit_parser  
  
  
FRAME_REPAIR_SYSTEM = """  
You are repairing a storyboard frame set after a strict audit.
 
You will receive:
1. the original numbered scene
2. the scene inventory
3. the current scene plan
4. the audit report
5. the relevant character bible and canonical names
 
Repair the scene plan so that:
- missing visual facts/props are INJECTED into the `props` or `must_show` lists of the EXISTING frames.
- invented details are removed.
- continuity notes are improved.
 
CRITICAL RULES:
1. DO NOT ADD NEW FRAMES unless absolutely necessary. Distribute missing facts into the existing frames to maintain a low frame count.
2. Keep exact dialogue in lines.
3. Do not invent unsupported actions or props.
4. Output valid JSON only.
5. characters_present must list character names in English/Roman script only. 
6. Ensure all characters_present names exactly match the canonical character bible names provided. Fix any name that has drifted.
 
{format_instructions}  
"""  
  
frame_repair_prompt = ChatPromptTemplate.from_messages([  
    ("system", FRAME_REPAIR_SYSTEM),  
    (  
        "human",  
        "Scene heading: {scene_heading}\n\n" 
        "Canonical Character Names:\n{canonical_character_names}\n\n"
        "Relevant Character Bible:\n{character_bible}\n\n"
        "Scene inventory:\n{scene_inventory}\n\n"  
        "Current scene plan:\n{scene_plan}\n\n"  
        "Audit report:\n{audit_report}\n\n"  
        "Numbered scene text:\n{scene_text}"  
    )  
]).partial(format_instructions=scene_plan_parser.get_format_instructions())  
  
frame_repair_chain = frame_repair_prompt | llm | scene_plan_parser  


REWRITER_SYSTEM = """
You are a professional storyboard prompt rewriter.

Your task is to rewrite raw image prompts into SAFE image-generation prompts while preserving the original visual intent, composition, staging, camera framing, character design, and scene structure as much as possible.

PRIMARY OBJECTIVE:
Preserve visual fidelity to the original prompt while removing or softening only content that may violate image-generation safety policies.

CRITICAL RULES:

1. SINGLE IMAGE ONLY

* The output must describe exactly one standalone image.
* Never convert the scene into multiple images.

2. NEVER CREATE MULTI-PANEL LAYOUTS
   Do NOT introduce:

* Storyboard sheets
* Comic pages
* Manga pages
* Split-screen layouts
* Diptychs
* Triptychs
* Multi-panel compositions
* Contact sheets
* Turnaround sheets
* Character lineups
* Before/after comparisons
* Collages
* Grids

3. OUTPUT FORMAT

Use exactly these blocks and in this exact order:

SUBJECT:
ACTION:
ENVIRONMENT:
CAMERA:
STYLE:
TECHNICAL:

Do not add additional headings.
Do not omit headings.
Do not use markdown.

4. IDENTITY PRESERVATION (HIGHEST PRIORITY)

If the input prompt contains ANY of the following:

* INVARIANTS:
* Reference image instructions
* Face-lock instructions
* Character identity instructions
* Gender-lock instructions
* Likeness instructions
* Facial preservation instructions
* Character consistency instructions
* Any statement mentioning:

  * reference images
  * photographs
  * sketches
  * facial identity
  * likeness
  * gender
  * age
  * ethnicity
  * hair
  * clothing
  * body build

Then:

COPY THOSE INSTRUCTIONS VERBATIM.

Do not:

* Rewrite
* Rephrase
* Summarize
* Compress
* Simplify
* Reorder
* Interpret
* Translate
* Modify punctuation
* Remove repetition

Preserve them character-for-character whenever possible.

Append all identity/reference instructions unchanged at the END of the TECHNICAL block.

Identity preservation instructions are the highest-priority content in the prompt.

If there is a conflict between rewriting and identity preservation:
PRESERVE THE IDENTITY INSTRUCTIONS.

5. SAFE REWRITING

Only modify content when necessary for safety compliance.

When modifying content:

* Preserve character count whenever possible.
* Preserve composition.
* Preserve camera angle.
* Preserve framing.
* Preserve location.
* Preserve character placement.
* Preserve emotional tone.
* Preserve visual storytelling.

Prefer softening rather than replacing.

Example:

Instead of removing an action entirely:

* Replace with a visually similar but safer action.

6. DO NOT INVENT DETAILS

Do not add:

* New characters
* New objects
* New locations
* New actions
* New costumes
* New camera angles

unless required to create a coherent safe prompt.

7. CHARACTER CONSISTENCY

Never alter:

* Character identity
* Face
* Hair
* Gender
* Age
* Ethnicity
* Body build
* Signature clothing

if these are specified in the prompt or reference instructions.

8. OUTPUT REQUIREMENTS

Return only the rewritten structured prompt.

No explanations.
No commentary.
No reasoning.
No markdown fences.
No notes.
No warnings.
No preamble.
"""




  
# REWRITER_SYSTEM = """  
# You are a professional storyboard prompt rewriter.  
  
# Your job:  
# Rewrite a raw image prompt into a SAFE prompt while preserving the visual staging as much as possible.  
  
# CRITICAL RULES:  
# 1. Keep it as one single standalone image.  
# 2. Never suggest a collage, page layout, storyboard sheet, multiple panels, diptych, triptych, or split-screen.  
# 3. Use exactly these labeled blocks:  
  
# SUBJECT:  
# ACTION:  
# ENVIRONMENT:  
# CAMERA:  
# STYLE:  
# TECHNICAL:  
  
# 4. IDENTITY PRESERVATION (most important): If the input prompt contains an
#    "INVARIANTS:" section or any instruction about reference images, character
#    faces, gender, or likeness, you MUST copy that text VERBATIM into your
#    output, unchanged, at the end of the TECHNICAL block. Never drop, soften,
#    summarize, or paraphrase identity/reference instructions — only soften
#    unsafe ACTION wording. The character's face, hair, build, and gender from
#    the reference images must always be preserved exactly.
  
# Output only the structured brief.  
# """  
  
rewriter_prompt = ChatPromptTemplate.from_messages([  
    ("system", REWRITER_SYSTEM),  
    ("human", "Rewrite this raw image prompt into a safe version:\n\n{raw_prompt}")  
])  
  
rewriter_chain = rewriter_prompt | rewriter_llm | StrOutputParser()  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# IMAGE HELPERS  
# ══════════════════════════════════════════════════════════════════════════════  
  
def needs_rewrite(prompt: str) -> bool:  
    prompt_lower = (prompt or "").lower()  
    return any(pattern in prompt_lower for pattern in FILTER_TRIGGER_PATTERNS)  
  
  
def _extract_invariants_block(raw_prompt: str) -> str:
    """
    Pulls the 'INVARIANTS:' section (identity / reference-image instructions)
    out of a raw prompt so it can be re-attached after a safe rewrite. The
    rewriter LLM can't be fully trusted to preserve it, so we re-append it
    deterministically. Returns "" if no invariants section is present.
    """
    if not raw_prompt or "INVARIANTS:" not in raw_prompt:
        return ""
    return "INVARIANTS:" + raw_prompt.split("INVARIANTS:", 1)[1]






def safe_rewrite_prompt(raw_prompt: str, attempt: int = 1) -> str:  
    extra = ""  
    if attempt == 2:  
        extra = (  
            " Be very conservative. Abstract unsafe action wording into safe visual staging, "  
            "composition, lighting, silhouette, and environment."  
        )  
    elif attempt >= 3:  
        extra = (  
            " Maximum abstraction. Keep only safe environment, composition, and implied mood. "  
            "Still preserve single-image composition."  
        )  

    # Identity instructions must survive the rewrite no matter what the LLM does.
    invariants = _extract_invariants_block(raw_prompt)

    try:  
        rewritten = rewriter_chain.invoke({"raw_prompt": raw_prompt + extra}).strip()
        # Re-attach the invariants verbatim if the rewriter dropped them.
        if invariants and "INVARIANTS:" not in rewritten:
            rewritten = f"{rewritten}\n{invariants}"
        return rewritten
    except Exception as e:  
        log.error(f"Prompt rewrite failed: {e}")  
        fallback = (  
            "SUBJECT: A safe single standalone scene composition.\n"  
            "ACTION: Minimal safe implied action only.\n"  
            "ENVIRONMENT: Atmospheric location details and clear readable staging.\n"  
            "CAMERA: One single composition only, not a collage or multi-panel layout.\n"  
            "STYLE: Single standalone monochrome storyboard sketch, rough pencil drawing, black and white.\n"  
            "TECHNICAL: Generate exactly one image only. No split-screen, no multiple panels, no page layout, no collage."  
        )
        # Even in the hard fallback, keep identity anchoring.
        if invariants:
            fallback = f"{fallback}\n{invariants}"
        return fallback


def _compress_b64_to_jpeg(
    b64_data: str,
    quality: int = 82,
    max_width: int = 1536,
) -> bytes:
    """
    Decode a base64 PNG from the image API, compress it to JPEG,
    and return the compressed bytes.

    quality=82 gives roughly 85–95% visual quality for monochrome
    storyboard sketches at ~300–600 KB vs the original ~14 MB PNG.
    max_width caps the long edge so oversized generations don't slip through.
    """
    raw_bytes = base64.b64decode(b64_data)
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

    # Downscale only if wider than max_width (preserve aspect ratio)
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)
        log.info(f"Image resized from {img.width}px → {max_width}px wide.")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    buf.seek(0)
    compressed = buf.read()

    original_kb = len(base64.b64decode(b64_data)) / 1024
    compressed_kb = len(compressed) / 1024
    log.info(f"Compression: {original_kb:.0f} KB PNG → {compressed_kb:.0f} KB JPEG "
             f"({100 * compressed_kb / original_kb:.1f}% of original)")
    return compressed


def _upload_b64_to_blob(
    b64_data: str,
    blob_name: str,
    jpeg_quality: int = 82,
) -> str:
    """
    Compress the raw base64 PNG to JPEG, then upload to Azure Blob Storage.
    The blob_name extension is forced to .jpg regardless of what was passed.
    """
    if container_client is None:
        raise RuntimeError("Azure Blob Storage is not configured.")

    # Force .jpg extension
    if blob_name.endswith(".png"):
        blob_name = blob_name[:-4] + ".jpg"

    img_bytes = _compress_b64_to_jpeg(b64_data, quality=jpeg_quality)

    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        img_bytes,
        overwrite=True,
        content_settings=ContentSettings(content_type="image/jpeg"),
    )
    return blob_client.url


  
def _download_blob_as_bytes(blob_url: str) -> bytes:  
    if container_client is None:  
        raise RuntimeError("Azure Blob Storage is not configured.")  
  
    clean_url = blob_url.split("?")[0]  
    marker = f"/{BLOB_CONTAINER}/"  
    if marker not in clean_url:  
        raise ValueError("Blob URL does not match configured container.")  
  
    blob_name = unquote(clean_url.split(marker, 1)[1])
    blob_client = container_client.get_blob_client(blob_name)  
    stream = blob_client.download_blob()  
    return stream.readall()  

def _is_content_filter_error(response: requests.Response) -> Tuple[bool, str, str]:  
    try:  
        body = response.json()  
        error = body.get("error", {})  
        code = error.get("code", "")  
        # Keep original case for readable logs
        msg = str(error.get("message", ""))  
        msg_lower = msg.lower()
        
        # NEW: Extract the exact reason or category that triggered the filter
        detailed_reason = msg
        inner_error = error.get("innererror", {})
        if inner_error and "contentFilterResults" in inner_error:
            results = inner_error["contentFilterResults"]
            # Find which exact categories (violence, hate, etc.) caused the block
            triggers = [cat for cat, data in results.items() if isinstance(data, dict) and data.get("filtered")]
            if triggers:
                detailed_reason = f"Categories triggered: {', '.join(triggers)}"
        
        if code == "contentFilter" or "content filter" in msg_lower or "safety system" in msg_lower: 
            if "image" in msg_lower and ("input" in msg_lower or "upload" in msg_lower or "provided" in msg_lower):
                return True, "input_image_blocked", detailed_reason
            if "generated image" in msg_lower:  
                return True, "output_blocked", detailed_reason  
            return True, "prompt_blocked", detailed_reason  
            
        if response.status_code == 400 and "filter" in msg_lower:  
            return True, "prompt_blocked", detailed_reason  
            
    except Exception:  
        pass  
        
    return False, "", ""


  
# def _is_content_filter_error(response: requests.Response) -> Tuple[bool, str]:  
#     try:  
#         body = response.json()  
#         error = body.get("error", {})  
#         code = error.get("code", "")  
#         msg = str(error.get("message", "")).lower()  
#         if code == "contentFilter" or "content filter" in msg or "safety system" in msg:  
#             if "generated image" in msg:  
#                 return True, "output_blocked"  
#             return True, "prompt_blocked"  
#         if response.status_code == 400 and "filter" in msg:  
#             return True, "prompt_blocked"  
#     except Exception:  
#         pass  
#     return False, ""  
  
  
def _looks_like_size_error(response: requests.Response) -> bool:  
    try:  
        body = response.json()  
        error = body.get("error", {})  
        msg = str(error.get("message", "")).lower()  
        return "size" in msg and ("unsupported" in msg or "invalid" in msg or "allowed" in msg)  
    except Exception:  
        return False  
  
  
def post_generation_with_size_fallback(prompt: str, sizes: List[str]) -> requests.Response:  
    last_response = None  
    for size in sizes:  
        response = requests.post(  
            GENERATION_URL,  
            headers={**IMAGE_HEADERS, "Content-Type": "application/json"},  
            json={  
                "prompt": prompt,  
                "n": 1,  
                "size": size,  
                "quality": "high",  
                "output_format": "png",  
            },  
            timeout=120,  
        )  
        last_response = response  
        if response.status_code != 400:  
            return response  
        if not _looks_like_size_error(response):  
            return response  
        log.warning(f"Image size {size} unsupported, trying next size.")  
    return last_response  
  
  
def post_edit_with_size_fallback(prompt: str, files_payload: List[Tuple[str, bytes]], sizes: List[str], input_fidelity: str) -> requests.Response:  
    last_response = None  
    for size in sizes:  
        files = [("image[]", (name, content, "image/png")) for name, content in files_payload]  
        response = requests.post(  
            EDIT_URL,  
            headers=IMAGE_HEADERS,  
            files=files,  
            data={  
                "prompt": prompt,  
                "n": "1",  
                "size": size,  
                "quality": "high",  
                "input_fidelity": input_fidelity,  
            },  
            timeout=180,  
        )  
        last_response = response  
        if response.status_code != 400:  
            return response  
        if not _looks_like_size_error(response):  
            return response  
        log.warning(f"Edit image size {size} unsupported, trying next size.")  
    return last_response  
  
# <-- Added parameters for frame metadata and logging label for better traceability in retries and rewrites --> 
def _call_with_retry_and_rewrite(build_request_fn, raw_prompt: str, label: str, blob_name: str, frame_metadata=None) -> Optional[str]:  
    current_prompt = raw_prompt  
    rewrite_attempt = 0  

    if needs_rewrite(current_prompt):  
        log.info(f"[{label}] Pre-emptive safe rewrite triggered.")  
        current_prompt = safe_rewrite_prompt(current_prompt, attempt=1)  
        rewrite_attempt = 1  

    for attempt in range(MAX_RETRIES + 2):  
        try:  
            response = build_request_fn(current_prompt)  

            if response.status_code in (200, 400):  
                is_filter, filter_type, filter_reason = _is_content_filter_error(response)  

                try:
                    is_filter, filter_type, filter_reason = _is_content_filter_error(response)
                except ValueError:
                    # ఒకవేళ పాత ఫంక్షన్ కేవలం రెండే ఇస్తుంటే దాన్ని క్యాచ్ చేసి హ్యాండిల్ చేయడం
                    res_tuple = _is_content_filter_error(response)
                    is_filter = res_tuple[0]
                    filter_type = res_tuple[1]
                    filter_reason = ""
                    
                if is_filter:  
                    rewrite_attempt += 1  
                    log.warning(f"[{label}] Content filter ({filter_type}) -> rewrite attempt {rewrite_attempt}. Reason: {filter_reason}")
                    # log.warning(f"[{label}] Content filter ({filter_type}) -> rewrite attempt {rewrite_attempt}")  
                    if rewrite_attempt > MAX_REWRITE_ATTEMPTS:  
                        log.error(f"[{label}] Exhausted rewrite attempts.")  
                        return None  
                    current_prompt = safe_rewrite_prompt(raw_prompt, attempt=rewrite_attempt)  
                    time.sleep(1)  
                    continue  

            response.raise_for_status()  
            data = response.json()  
            b64 = data["data"][0]["b64_json"] 

            # --- STAMP METADATA AND ARROWS ONTO IMAGE ---
            # stamp.py now handles both the text banner AND the visual geometric arrows
            if frame_metadata:
                try:
                    b64 = stamp_metadata_on_image(b64, frame_metadata)
                    log.info(f"[{label}] Metadata banner and movement arrows applied to image.")
                except Exception as stamp_err:
                    log.error(f"[{label}] Failed to stamp metadata: {stamp_err}")
            # ----------------------------------

            blob_url = _upload_b64_to_blob(b64, blob_name, jpeg_quality=JPEG_QUALITY)  
            log.info(f"[{label}] Uploaded -> {blob_url}")  
            return blob_url  

        except requests.exceptions.HTTPError as e:  
            status = e.response.status_code if e.response else 0  
            is_filter, filter_type,filter_reason = _is_content_filter_error(e.response) if e.response else (False, "","")  
            if is_filter:  
                rewrite_attempt += 1  
                # log.warning(f"[{label}] Filter error HTTP {status} ({filter_type}), rewrite attempt {rewrite_attempt}")  
                log.warning(f"[{label}] Filter error HTTP {status} ({filter_type}), rewrite attempt {rewrite_attempt}. Reason: {filter_reason}")
                if rewrite_attempt > MAX_REWRITE_ATTEMPTS:  
                    return None  
                current_prompt = safe_rewrite_prompt(raw_prompt, attempt=rewrite_attempt)  
                time.sleep(1)  
                continue  

            if status == 429:  
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)] * 3  
                log.warning(f"[{label}] Rate limited. Waiting {wait}s.")  
                time.sleep(wait)  
            else:  
                log.error(f"[{label}] HTTP {status}: {e}")  
                if attempt >= MAX_RETRIES - 1:  
                    return None  
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])  

        except Exception as e:  
            log.error(f"[{label}] Unexpected error: {e}")  
            if attempt >= MAX_RETRIES - 1:  
                return None  
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])  

    return None
  
  
def _get_input_fidelity(frame: Dict[str, Any]) -> str:
    return "high" if frame.get("characters_present") else "low"
  
  
def should_use_prev_frame_anchor(prev_frame: Optional[Dict[str, Any]], current_frame: Dict[str, Any]) -> bool:  
    if not prev_frame:  
        return False  
    if not prev_frame.get("image_url"):  
        return False  
    # NEW: never chain the previous frame when a character is present.
    # Identity must come ONLY from the canonical sketch + uploaded photo anchor,
    # not from a previous frame that may already have drifted.
    if current_frame.get("characters_present"):
        return False
    if set(prev_frame.get("characters_present", [])) != set(current_frame.get("characters_present", [])):  
        return False  
    if (prev_frame.get("shot_type") or "").strip().lower() != (current_frame.get("shot_type") or "").strip().lower():  
        return False  
    return True  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# IMAGE GENERATION  
# ══════════════════════════════════════════════════════════════════════════════  
  
def generate_reference_image(character: Dict[str, Any], visual_style: str, color_palette: str, thread_id: str) -> Optional[str]:  
    label = f"CHAR_REF:{character['name']}"  
    safe_name = sanitize_for_filename(character["name"])  
    blob_name = f"{thread_id}/characters/{safe_name}_reference.png"  
  
    raw_prompt = (  
        f"SUBJECT: One single character only: {character['name']}. {character['visual_summary']}\n"  
        f"ACTION: Neutral standing pose for canonical full-body character reference. One figure only.\n"  
        f"ENVIRONMENT: Plain clean light gray studio background. No props. No furniture. No extra people. No background scene.\n"  
        f"CAMERA: Straight-on full-body long shot. Entire figure must be visible from the very top of the hair to the bottom of the shoes. "  
        f"Leave clear empty margin above the head, below the feet, and on both sides. No crop. No close-up. No medium shot.\n"  
        f"STYLE: Single standalone monochrome storyboard sketch, rough pencil line art, black and white. "  
        f"Visual style: {visual_style}. Palette: {color_palette}.\n"  
        f"TECHNICAL: {single_image_rules_text()} "  
        f"One single person only. Show full head, full hands, full legs, and full feet. "  
        f"No cut-off limbs. No duplicated limbs. No second pose. No turnaround sheet. No front-and-side combo. "  
        f"No page layout. No multiple character studies. Strictly monochrome, zero color, visible paper texture, not photorealistic, not 3D."  
    )  
  
    def build_request(prompt: str):  
        return post_generation_with_size_fallback(prompt, CHARACTER_IMAGE_SIZES)  
  
    return _call_with_retry_and_rewrite(build_request, raw_prompt, label, blob_name)  
  
  
def generate_scene_reference(scene: Dict[str, Any], breakdown: Dict[str, Any], thread_id: str) -> Optional[str]:  
    label = f"SCENE_REF:S{scene['scene_number']:02d}"  
    blob_name = f"{thread_id}/scenes/scene_{scene['scene_number']:02d}_reference.png"  
  
    env_block = "; ".join(scene.get("environment_inventory", [])) or "As established"  
  
    raw_prompt = (  
        f"SUBJECT: Empty location environment only. No people. No handheld props.\n"  
        f"ACTION: Static establishing environment — the empty set before any character enters.\n"  
        f"ENVIRONMENT: {scene['location']}, {scene['time_of_day']}. Lighting mood: {scene['lighting_mood']}. "  
        f"Permanent fixtures, architecture, furniture, and landscape only: {env_block}. "  
        f"Do NOT draw loose or handheld objects such as cups, phones, papers, bottles, bags, "  
        f"or food — those are introduced by characters in individual frames, not in this empty plate.\n"  
        f"CAMERA: One single wide establishing composition. Clear readable geography. No split composition.\n"  
        f"STYLE: Single standalone monochrome storyboard sketch, rough pencil line art, black and white. "  
        f"Visual style: {breakdown.get('visual_style', DEFAULT_VISUAL_STYLE)}. "  
        f"Palette: {breakdown.get('color_palette', DEFAULT_COLOR_PALETTE)}.\n"  
        f"TECHNICAL: {single_image_rules_text()} "  
        f"No storyboard sheet. No page border layout. No stacked frames. Strictly monochrome, zero color, visible paper texture, not photorealistic, not 3D."  
    )  
  
    def build_request(prompt: str):  
        return post_generation_with_size_fallback(prompt, SCENE_IMAGE_SIZES)  
  
    return _call_with_retry_and_rewrite(build_request, raw_prompt, label, blob_name)  



# ══════════════════════════════════════════════════════════════════════════════
# MOVEMENT NOTATION + ARROW HELPERS
# ══════════════════════════════════════════════════════════════════════════════

import re as _re   # add this at the very top of the file if not already there
 
def _extract_movement_notation(composition: str) -> str:
    """
    Detects camera/subject movement notation in a composition string.
    Returns the movement clause, or empty string if none found.
    Examples that trigger:  "→ PAN RIGHT"  "↑ TILT UP"  "TRACKING SHOT left"
    Examples that don't:    "Wide establishing shot, static"
    """
    if not composition:
        return ""
    arrow = _re.search(r"[→←↑↓↗↘↙↖⟳].*", composition)
    if arrow:
        return arrow.group(0).strip()
    kw = _re.search(
        r"\b(pan|tilt|push in|pull back|zoom in|zoom out|tracking shot|dolly|crane|whip pan|rack focus)\b.*",
        composition,
        _re.IGNORECASE,
    )
    if kw:
        return kw.group(0).strip()
    return ""





def _classify_unmatched_character(char_name: str, scene_context: str) -> Dict[str, str]:
    """
    Uses LLM to classify a character that has no bible entry.
    Returns a dict with:
        - "type": "background" | "named_missing"
        - "description": a safe visual description for the image prompt
    Works for any language, any screenplay type.
    """
    classify_prompt = f"""You are a screenplay character classifier.

A character appears in a storyboard frame but has NO entry in the character bible.
Your job is to classify them and write a safe visual description for image generation.

Character name / label as written: "{char_name}"
Scene context: "{scene_context}"

Classify into ONE of these two types:

TYPE A — BACKGROUND FIGURE:
  The character has no name, or is described by role only
  (e.g. "Woman in window", "Frightened villager", "Old man at door",
  "Crowd", "Guard", "Passerby").
  These are incidental figures with no established appearance.

TYPE B — NAMED MISSING:
  The character has a proper name but was not captured in the character bible.
  They have a specific role in the scene.

Respond ONLY with valid JSON in this exact format, nothing else:
{{
  "type": "background" or "named_missing",
  "description": "one safe visual sentence describing appearance for image generation"
}}

Rules for description:
- For background: generic appearance fitting the scene. Not distinctive. No specific face.
- For named_missing: infer appearance from scene context and name. Be specific but safe.
- Always in English.
- No violence, no unsafe content.
- Maximum 30 words.
"""

    try:
        result = llm.invoke([{"role": "user", "content": classify_prompt}])
        text = result.content.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        char_type = parsed.get("type", "background")
        description = parsed.get("description", "Generic figure appropriate to scene context.")
        if char_type not in ("background", "named_missing"):
            char_type = "background"
        return {"type": char_type, "description": description}
    except Exception as e:
        log.warning(f"Character classification LLM call failed for '{char_name}': {e}")
        return {
            "type": "background",
            "description": "Generic figure appropriate to scene location and time period."
        }


def _dedupe_visual_terms(primary: List[str], secondary: List[str]) -> Tuple[List[str], List[str]]:
    """
    Removes from `secondary` any term already represented in `primary`
    (case-insensitive, substring match in either direction), so the same
    object is not named twice across must_show / props / environment.

    Naming a prop in multiple prompt blocks makes the image model render
    duplicate copies of it (e.g. four tea cups instead of two). Keeping each
    object mentioned exactly once removes that duplication pressure.

    Returns (primary_unchanged, secondary_filtered).
    """
    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    primary_norms = [_norm(p) for p in primary if _norm(p)]
    kept_secondary: List[str] = []
    for term in secondary:
        t = _norm(term)
        if not t:
            continue
        is_dup = any(t == pn or t in pn or pn in t for pn in primary_norms)
        if not is_dup:
            kept_secondary.append(term)
    return primary, kept_secondary











def generate_frame_image(

    frame: Dict[str, Any],

    scene: Dict[str, Any],

    breakdown: Dict[str, Any],

    character_bible: Dict[str, Any],

    reference_images: Dict[str, str],

    scene_reference_url: Optional[str] = None,

    prev_frame: Optional[Dict[str, Any]] = None,

    thread_id: str = "default_thread",

    user_uploaded_images: Optional[Dict[str, str]] = None,

) -> Tuple[Optional[str], str, str]:
 
    user_uploaded_images = user_uploaded_images or {}
 
    frame_id = frame["frame_id"]

    label = f"FRAME:{frame_id}"

    blob_name = f"{thread_id}/frames/{sanitize_for_filename(frame_id)}.png"
 
    characters_present = frame.get("characters_present", [])
 
    # ── DIAGNOSTIC ───────────────────────────────────────────────────────

    log.info(f"[{label}] ── DIAGNOSTIC ──────────────────────────────────────")

    log.info(f"[{label}] characters_present    : {characters_present}")

    log.info(f"[{label}] reference_images keys : {list(reference_images.keys())}")

    log.info(f"[{label}] uploaded photo keys   : {list(user_uploaded_images.keys())}")

    log.info(f"[{label}] character_bible keys  : {list(character_bible.keys())}")

    log.info(f"[{label}] ────────────────────────────────────────────────────")

    # ─────────────────────────────────────────────────────────────────────
 
    primary_refs: List[Tuple[str, str, str]] = []  # (frame_name, bible_key, sketch_url)

    resolved_char_names: Dict[str, str] = {}
 
    # ── Reference lookup with fuzzy fallback ─────────────────────────────

    for char_name in characters_present:

        ref_url = reference_images.get(char_name)

        matched_key = char_name

        if not ref_url:

            fuzzy_key = _match_character_name(char_name, list(reference_images.keys()))

            if fuzzy_key:

                ref_url = reference_images.get(fuzzy_key)

                matched_key = fuzzy_key

                log.info(f"[{label}] Fuzzy matched ref: '{char_name}' → '{fuzzy_key}'")

            else:

                log.warning(f"[{label}] NO ref match for {repr(char_name)}")

        resolved_char_names[char_name] = matched_key

        if ref_url:

            primary_refs.append((char_name, matched_key, ref_url))

        if len(primary_refs) >= MAX_CHARACTER_REFS_PER_FRAME:

            break

    # ─────────────────────────────────────────────────────────────────────
 
    log.info(f"[{label}] primary_refs count    : {len(primary_refs)}")
 
    files_payload: List[Tuple[str, bytes]] = []

    invariant_lines: List[str] = []

    ref_index = 1
 
    for char_name, matched_key, ref_url in primary_refs:

        # Gender lock from bible

        char_data_inv = character_bible.get(matched_key) or character_bible.get(char_name) or {}

        if not char_data_inv:

            fk = _match_character_name(char_name, list(character_bible.keys()))

            if fk:

                char_data_inv = character_bible.get(fk) or {}

        gender_str = char_data_inv.get("gender", "")

        gender_lock = (

            f" This character is {gender_str}. Do NOT change the sex or gender of this character."

            if gender_str else ""

        )
 
        # ── NEW: high-signal FACE ANCHOR from the original uploaded photo ──

        face_url = user_uploaded_images.get(char_name) or user_uploaded_images.get(matched_key)

        if not face_url and user_uploaded_images:

            fk = _match_character_name(char_name, list(user_uploaded_images.keys()))

            if fk:

                face_url = user_uploaded_images.get(fk)
 
        if face_url:

            try:

                face_bytes = _download_blob_as_bytes(face_url)

                clean_face = _standardize_reference_image(face_bytes, target_size=1024)

                files_payload.append((f"face_{ref_index}.png", clean_face))

                invariant_lines.append(

                    f"- Reference image {ref_index} is a PHOTOGRAPH of {char_name} and is the "

                    f"DEFINITIVE, AUTHORITATIVE source of this character's facial identity. "

                    f"The face in the output MUST match this exact person: replicate the precise "

                    f"face shape, bone structure, jawline, eye shape and spacing, eyebrow shape, "

                    f"nose shape, lip shape, and hairline/hair. This is a real specific person — "

                    f"do NOT alter, idealize, average, beautify, or substitute the face. "

                    f"Discard ONLY the photo's background, colours, lighting, clothing, and "

                    f"photographic finish; redraw THIS exact face as a monochrome pencil sketch."

                    f"{gender_lock}"

                )

                

                ref_index += 1

                log.info(f"[{label}] Face anchor photo added for '{char_name}'.")

            except Exception as e:

                log.warning(f"[{label}] Could not load face anchor for '{char_name}': {e}")
 
        # ── Canonical storyboard sketch reference (style + body + clothing) ──

        try:

            raw_bytes = _download_blob_as_bytes(ref_url)

            clean_png_bytes = _standardize_reference_image(raw_bytes)

            files_payload.append((f"char_{ref_index}.png", clean_png_bytes))

            if face_url:

                # A real photo anchor exists → it owns the face. The sketch is

                # only body/clothing reference, so it can't drag the face back

                # to its (reframed, drifted) version.

                invariant_lines.append(

                    f"- Reference image {ref_index} is the canonical character sketch for {char_name}. "

                    f"Use it ONLY for body build, clothing, proportions, and overall silhouette.{gender_lock} "

                    f"Do NOT copy the face from this sketch — the facial identity comes EXCLUSIVELY "

                    f"from the PHOTOGRAPH reference above. Ignore this sketch's background, lighting, "

                    f"and any facial deviation from the photograph."

                )
                

            else:

                # No uploaded photo → the sketch is the only face source, keep old behaviour.

                invariant_lines.append(

                    f"- Reference image {ref_index} is the canonical character sketch for {char_name}. "

                    f"Preserve the exact face, hair, body build, and clothing shown.{gender_lock} "

                    f"Completely ignore the background, lighting, and art style of this reference."

                )

                


                

            ref_index += 1

        except Exception as e:

            log.warning(f"[{label}] Could not load sketch for '{char_name}': {e}")
 
    if scene_reference_url:

        invariant_lines.append(

            f"- Reference image {ref_index} is the location reference. "

            f"Preserve environment layout, prop placement, and lighting mood."

        )

        files_payload.append(("scene_ref.png", _download_blob_as_bytes(scene_reference_url)))

        ref_index += 1
 
    use_prev = should_use_prev_frame_anchor(prev_frame, frame)

    if use_prev and prev_frame and prev_frame.get("image_url"):

        invariant_lines.append(

            f"- Reference image {ref_index} is the previous frame. "

            f"Preserve continuity where appropriate without turning the output "

            f"into a multi-panel layout."

        )

        files_payload.append(("prev_frame.png", _download_blob_as_bytes(prev_frame["image_url"])))
 
    invariant_block = (

        "\n".join(invariant_lines) if invariant_lines

        else "- Maintain internal visual consistency."

    )
 
    # ── Subject block — 3-tier universal handler ──────────────────────────

    scene_context = (

        f"Scene: {scene.get('scene_heading', '')}. "

        f"Location: {scene.get('location', '')}. "

        f"Time: {scene.get('time_of_day', '')}. "

        f"Summary: {scene.get('summary', '')}"

    )
 
    subject_parts = []

    for char_name in characters_present:

        bible_key = resolved_char_names.get(char_name, char_name)

        char_data = character_bible.get(bible_key)

        if not char_data:

            fuzzy_key = _match_character_name(char_name, list(character_bible.keys()))

            if fuzzy_key:

                char_data = character_bible.get(fuzzy_key)

                log.info(f"[{label}] Subject fuzzy match: '{char_name}' → '{fuzzy_key}'")
 
        if char_data:

            gender_str = char_data.get("gender", "")

            gender_prefix = f"{gender_str} — " if gender_str else ""

            subject_parts.append(

                f"{char_name}: {gender_prefix}{char_data.get('visual_summary', 'As established')}"

            )

            log.info(f"[{label}] '{char_name}' → Tier 1 (bible match).")

            continue
 
        # ── Tier 2 + 3: no bible entry → LLM classifies ──────────────────

        log.info(f"[{label}] '{char_name}' not in bible → calling LLM classifier.")

        classification = _classify_unmatched_character(char_name, scene_context)

        char_type = classification["type"]

        description = classification["description"]

        if char_type == "background":

            subject_parts.append(

                f"{char_name}: {description} "

                f"Minor background figure — not visually prominent, no distinctive face."

            )

            log.info(f"[{label}] '{char_name}' → Tier 2 background. Desc: {description}")

        else:

            subject_parts.append(

                f"{char_name}: {description} "

                f"Supporting character — visually subordinate to main characters."

            )

            log.warning(

                f"[{label}] '{char_name}' → Tier 3 named missing from bible. "

                f"Desc: {description}. Consider rerunning character extraction."

            )
 
    subject_block = (

        " | ".join(subject_parts) if subject_parts

        else "No visible person; environment-focused image."

    )
 
    # ── Prop de-duplication ──────────────────────────────────────────────

    must_show_list = list(frame.get("must_show", []))

    props_list     = list(frame.get("props", []))

    env_list       = list(scene.get("environment_inventory", []))
 
    must_show_list, props_list = _dedupe_visual_terms(must_show_list, props_list)

    _key_terms = must_show_list + props_list

    _, env_list = _dedupe_visual_terms(_key_terms, env_list)
 
    must_show   = "; ".join(must_show_list) or "As established"

    props       = "; ".join(props_list) or "As established"

    env_inv     = "; ".join(env_list) or "As established"

    composition = frame.get("composition") or "As established"
 
    movement_notation = _extract_movement_notation(composition)
    # ── DEBUGGING COMPOSITION & ARROWS ───────────────────────────────────
    log.info(f"[{label}] 🔴 DEBUG COMPOSITION RAW TEXT: {repr(composition)}")
    log.info(f"[{label}] 🔵 DEBUG EXTRACTED MOVEMENT: {repr(movement_notation)}")
    clean_composition = re.sub(r"[→←↑↓↗↘↙↖⟳]", "", composition).strip()
    clean_movement = re.sub(r"[→←↑↓↗↘↙↖⟳]", "", movement_notation).strip()   
    if clean_movement:

        camera_block = (

            f"One single composition only. Shot type: {frame['shot_type']}. "

            f"Angle: {frame['angle']}. Composition: {clean_composition}. "

            f"Camera movement: {clean_movement}. Tone: {frame['tone']}."

        )

    else:

        camera_block = (

            f"One single composition only. Shot type: {frame['shot_type']}. "

            f"Angle: {frame['angle']}. Composition: {clean_composition}. Tone: {frame['tone']}."

        )
 
    raw_prompt = (

        f"SUBJECT: {subject_block}\n"

        f"ACTION: {frame['action']}\n"

        f"ENVIRONMENT: Location: {scene['location']}, {scene['time_of_day']}. "

        f"{frame['setting_detail']} "

        f"Environment inventory: {env_inv}.\n"

        f"CAMERA: {camera_block}\n"

        f"STYLE: Single standalone monochrome storyboard sketch, rough pencil "

        f"line art, black and white. "

        f"Visual style: {breakdown.get('visual_style', DEFAULT_VISUAL_STYLE)}. "

        f"Palette: {breakdown.get('color_palette', DEFAULT_COLOR_PALETTE)}.\n"

        f"TECHNICAL: Must show: {must_show}. Props visible: {props}. "

        f"Continuity: {frame.get('continuity_notes', 'Maintain continuity.')}. "

        f"PROP DISCIPLINE: This frame is one single instant. Draw each object exactly once. "

        f"Do NOT duplicate props or characters. The count of every object must match the action "

        f"exactly — if a character holds two cups, show exactly two cups, in that character's hands "

        f"only, with no extra copies elsewhere. A prop belongs to the character or surface named in "

        f"the action; never scatter duplicate copies onto other characters or into the background. "

        f"If the shot type or camera angle would not naturally reveal a small prop, omit or "

        f"de-emphasize it rather than forcing it into an unnatural position. "

        f"{single_image_rules_text()} "

        f"No drawn page layout. No multiple internal frames. No contact sheet. "

        f"No repeated figure. "

        f"INVARIANTS:\n{invariant_block}\n"

        f"Strictly monochrome, zero color, visible paper texture, "

        f"not photorealistic, not 3D."

    )
 
    if files_payload:

        def build_request(prompt: str):

            return post_edit_with_size_fallback(

                prompt=prompt,

                files_payload=files_payload,

                sizes=FRAME_IMAGE_SIZES,

                input_fidelity=_get_input_fidelity(frame),

            )

        method = "edit_with_references"

    else:

        def build_request(prompt: str):

            return post_generation_with_size_fallback(prompt, FRAME_IMAGE_SIZES)

        method = "generation_fallback"
 
    log.info(f"[{label}] files_payload count   : {len(files_payload)}")

    log.info(f"[{label}] generation method     : {method}")

    if method == "generation_fallback":

        log.warning(f"[{label}] ⚠️  NO REFERENCE IMAGES — generating from text prompt only!")
 
    metadata_to_burn = {

        "scene":             scene.get("scene_number"),

        "frame_id":          frame.get("frame_id"),

        "location":          scene.get("location"),

        "time_of_day":       scene.get("time_of_day"),

        "shot_type":         frame.get("shot_type"),

        "composition":       composition,

        "action":            frame.get("action"),

        "movement":          movement_notation,   # FIX: Updated key to match stamp.py

    }
 
    result_url = _call_with_retry_and_rewrite(

        build_request,

        raw_prompt,

        label,

        blob_name,

        frame_metadata=metadata_to_burn,

    )
 
    return result_url, method, raw_prompt
 

# ══════════════════════════════════════════════════════════════════════════════  
# SPLIT / NORMALIZE HELPERS  
# ══════════════════════════════════════════════════════════════════════════════  
  
def validate_scene_boundaries(scenes: List[Dict[str, Any]], total_lines: int) -> List[str]:  
    issues = []  
    if not scenes:  
        return ["No scenes were returned."]  
  
    sorted_scenes = sorted(scenes, key=lambda x: int(x.get("start_line", 1)))  
  
    expected_start = 1  
    for idx, scene in enumerate(sorted_scenes, start=1):  
        start_line = int(scene.get("start_line", 1))  
        end_line = int(scene.get("end_line", start_line))  
  
        if start_line < 1:  
            issues.append(f"Scene {idx} starts before line 1.")  
        if end_line < start_line:  
            issues.append(f"Scene {idx} has end_line before start_line.")  
        if end_line > total_lines:  
            issues.append(f"Scene {idx} ends after the screenplay ends.")  
  
        if start_line != expected_start:  
            issues.append(f"Scene {idx} should start at line {expected_start} but starts at {start_line}.")  
  
        expected_start = end_line + 1  
  
    if expected_start != total_lines + 1:  
        issues.append(f"Scene coverage ends at line {expected_start - 1} instead of {total_lines}.")  
  
    return issues  
  
  
def normalize_scene_boundaries(scene_list: List[Dict[str, Any]], total_lines: int) -> List[Dict[str, Any]]:  
    sorted_scenes = sorted(scene_list, key=lambda x: int(x.get("start_line", 1)))  
    normalized = []  
  
    for idx, scene in enumerate(sorted_scenes, start=1):  
        start_line = max(1, int(scene.get("start_line", 1)))  
        end_line = min(total_lines, int(scene.get("end_line", total_lines)))  
        if end_line < start_line:  
            end_line = start_line  
  
        normalized.append({  
            "scene_number": idx,  
            "scene_heading": scene.get("scene_heading", f"Scene {idx}") or f"Scene {idx}",  
            "start_line": start_line,  
            "end_line": end_line,  
            "location": scene.get("location", "As established") or "As established",  
            "time_of_day": scene.get("time_of_day", "As established") or "As established",  
            "summary": scene.get("summary", "As established") or "As established",  
        })  
  
    return normalized  
  
  
def get_relevant_character_bible(scene_text: str, character_bible: Dict[str, Any]) -> Dict[str, Any]:  
    if not character_bible:  
        return {}  
  
    relevant = {}  
    for canonical_name, data in character_bible.items():  
        candidates = [canonical_name] + data.get("aliases", [])  
        for candidate in candidates:  
            if contains_name(scene_text, candidate):  
                relevant[canonical_name] = data  
                break  
  
    return relevant if relevant else character_bible  
  
  
def normalize_frame(frame: Dict[str, Any], scene_number: int, frame_index: int, fallback_start: int, fallback_end: int) -> Dict[str, Any]:  
    local = dict(frame)  
  
    local["frame_id"] = f"S{scene_number:02d}F{frame_index:03d}"  
    local["characters_present"] = dedupe_preserve_order(local.get("characters_present", []))  
    local["must_show"] = dedupe_preserve_order(local.get("must_show", []))  
    local["props"] = dedupe_preserve_order(local.get("props", []))  
    local["uncertain_details"] = dedupe_preserve_order(local.get("uncertain_details", []))  
    local["lines"] = local.get("lines") or "None"  
    local["tone"] = local.get("tone") or "As established"  
    local["shot_type"] = local.get("shot_type") or "medium shot"  
    local["angle"] = local.get("angle") or "eye level"  
    local["composition"] = local.get("composition") or "As established"  
    local["continuity_notes"] = local.get("continuity_notes") or "Maintain continuity with previous frame and reference images."  
  
    source_spans = local.get("source_spans", [])  
    if not source_spans:  
        source_spans = [{  
            "start_line": fallback_start,  
            "end_line": fallback_end,  
            "quote": "As established from the scene."  
        }]  
  
    cleaned_spans = []  
    for span in source_spans:  
        start_line = max(fallback_start, int(span.get("start_line", fallback_start)))  
        end_line = min(fallback_end, int(span.get("end_line", fallback_end)))  
        if end_line < start_line:  
            end_line = start_line  
        cleaned_spans.append({  
            "start_line": start_line,  
            "end_line": end_line,  
            "quote": span.get("quote", "As established from the scene.")  
        })  
  
    local["source_spans"] = cleaned_spans  
    local["image_generated"] = bool(local.get("image_generated", False))  
  
    return Frame(**local).model_dump()  
  
  
def build_scene_object(raw_scene: Dict[str, Any], inventory: Dict[str, Any], scene_plan: Dict[str, Any], character_bible: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  
    bible_keys = list((character_bible or {}).keys())

    frames = []  
    for idx, frame in enumerate(scene_plan.get("frames", []), start=1):  
        # ── Snap every character name to its canonical bible key ──────────
        # The frame planner sometimes re-spells transliterated names
        # ("Tabby" → "Tabi"). If that drift reaches image generation, the
        # character loses her reference images and is regenerated from text
        # every frame — the #1 cause of face inconsistency. Normalize here,
        # once, so the rest of the pipeline only ever sees canonical names.
        if bible_keys:
            normalized_present = []
            for name in frame.get("characters_present", []):
                if name in character_bible:
                    normalized_present.append(name)
                    continue
                matched = _match_character_name(name, bible_keys)
                if matched:
                    if matched != name:
                        log.info(f"[NAME_FIX] Scene {raw_scene['scene_number']} frame {idx}: "
                                 f"'{name}' → canonical '{matched}'")
                    normalized_present.append(matched)
                else:
                    # Genuinely unknown (background figure) — leave as-is.
                    normalized_present.append(name)
            frame = dict(frame)
            frame["characters_present"] = dedupe_preserve_order(normalized_present)

        frames.append(  
            normalize_frame(  
                frame=frame,  
                scene_number=raw_scene["scene_number"],  
                frame_index=idx,  
                fallback_start=raw_scene["start_line"],  
                fallback_end=raw_scene["end_line"],  
            )  
        )  
  
    scene_obj = Scene(  
        scene_number=raw_scene["scene_number"],  
        scene_heading=raw_scene["scene_heading"],  
        start_line=raw_scene["start_line"],  
        end_line=raw_scene["end_line"],  
        summary=scene_plan.get("summary") or inventory.get("summary") or raw_scene.get("summary") or "As established",  
        location=raw_scene.get("location") or inventory.get("location") or "As established",  
        time_of_day=raw_scene.get("time_of_day") or inventory.get("time_of_day") or "As established",  
        lighting_mood=scene_plan.get("lighting_mood") or inventory.get("lighting_mood") or "As established",  
        environment_inventory=dedupe_preserve_order(  
            (scene_plan.get("environment_inventory") or []) + (inventory.get("environment_details") or [])  
        ),  
        prop_inventory=dedupe_preserve_order(  
            (scene_plan.get("prop_inventory") or []) + (inventory.get("props_explicit") or [])  
        ),  
        frames=frames,  
        scene_reference_url=None,  
    )  
    return scene_obj.model_dump()  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# STATE  
# ══════════════════════════════════════════════════════════════════════════════  
  
class PipelineState(TypedDict):
    screenplay_text: str
    screenplay_title: Optional[str]
    numbered_lines: Optional[List[str]]
    numbered_screenplay_text: Optional[str]
    scene_boundaries: Optional[List[Dict[str, Any]]]
    scenes_raw: Optional[List[Dict[str, Any]]]
    character_bible: Optional[Dict[str, Any]]
    scene_inventories: Optional[Dict[str, Any]]
    planned_scenes: Optional[List[Dict[str, Any]]]
    breakdown: Optional[Dict[str, Any]]
    reference_images: Optional[Dict[str, str]]
    scene_reference_images: Optional[Dict[str, str]]
    final_output: Optional[Dict[str, Any]]
    current_step: str
    user_uploaded_images: Optional[Dict[str, str]]  # character_name -> blob_url (from user uploads)
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# NODES  
# ══════════════════════════════════════════════════════════════════════════════  
  
def preprocess_script_node(state: PipelineState):  
    log.info("=== LANGGRAPH: Numbering screenplay lines...")  
  
    screenplay_text = state["screenplay_text"]  
    screenplay_title = guess_title(screenplay_text)  
    numbered_lines, numbered_screenplay_text = add_line_numbers(screenplay_text)  
  
    return {  
        "screenplay_title": screenplay_title,  
        "numbered_lines": numbered_lines,  
        "numbered_screenplay_text": numbered_screenplay_text,  
        "current_step": "line_numbered",  
    }  
  
  
def extract_characters_node(state: PipelineState):  
    log.info("=== LANGGRAPH: Extracting character bible...")  
  
    bible_raw = invoke_chain_with_retry(  
        character_chain,  
        {"screenplay": state["screenplay_text"]},  
        label="character_extraction",  
        retries=2,  
    )  
    character_bible = {c["name"]: c for c in bible_raw["characters"]}  
  
    log.info(f"Character count: {len(character_bible)}")  
    return {  
        "character_bible": character_bible,  
        "current_step": "characters_extracted",  
    }  
  
  
def split_scenes_node(state: PipelineState):  
    log.info("=== LANGGRAPH: Splitting screenplay into scenes using LLM only...")  
  
    numbered_screenplay_text = state["numbered_screenplay_text"]  
    numbered_lines = state["numbered_lines"] or []  
    total_lines = len(numbered_lines)  
  
    scene_split = invoke_chain_with_retry(  
        scene_split_chain,  
        {"numbered_screenplay": numbered_screenplay_text},  
        label="scene_split",  
        retries=2,  
    )  
  
    raw_scenes = scene_split.get("scenes", [])  
    issues = validate_scene_boundaries(raw_scenes, total_lines)  
  
    if issues:  
        log.warning("Scene split validation issues found. Running repair pass...")  
        repaired = invoke_chain_with_retry(  
            scene_split_repair_chain,  
            {  
                "numbered_screenplay": numbered_screenplay_text,  
                "scene_list": json.dumps(scene_split, indent=2, ensure_ascii=False),  
                "issues": "\n".join(issues),  
            },  
            label="scene_split_repair",  
            retries=2,  
        )  
        raw_scenes = repaired.get("scenes", [])
        
        # ---> NEW: Heal the repaired scenes too, just in case <---
        raw_scenes = heal_scene_gaps(raw_scenes, total_lines)
        issues = validate_scene_boundaries(raw_scenes, total_lines)  
  
    if issues:  
        log.warning(f"Scene split still invalid after repair. Falling back to single-scene mode. Issues: {issues}")  
        raw_scenes = [{  
            "scene_number": 1,  
            "scene_heading": state.get("screenplay_title") or "Scene 1",  
            "start_line": 1,  
            "end_line": total_lines,  
            "location": "As established",  
            "time_of_day": "As established",  
            "summary": "As established",  
        }]  
  
    scene_boundaries = normalize_scene_boundaries(raw_scenes, total_lines)  
  
    scenes_raw = []  
    for scene in scene_boundaries:  
        scene_text = slice_numbered_lines(numbered_lines, scene["start_line"], scene["end_line"])  
        scenes_raw.append({  
            "scene_number": scene["scene_number"],  
            "scene_heading": scene["scene_heading"],  
            "start_line": scene["start_line"],  
            "end_line": scene["end_line"],  
            "location": scene["location"],  
            "time_of_day": scene["time_of_day"],  
            "summary": scene["summary"],  
            "text": scene_text,  
        })  
  
    log.info(f"Scene count: {len(scenes_raw)}")  
  
    return {  
        "scene_boundaries": scene_boundaries,  
        "scenes_raw": scenes_raw,  
        "screenplay_title": scene_split.get("title") or state.get("screenplay_title") or "Untitled Screenplay",  
        "current_step": "scenes_split",  
    }  


def heal_scene_gaps(scenes: List[Dict[str, Any]], total_lines: int) -> List[Dict[str, Any]]:
    """Automatically closes gaps between scenes so validation never fails."""
    if not scenes:
        return scenes
        
    # Sort scenes by their start line
    sorted_scenes = sorted(scenes, key=lambda x: int(x.get("start_line", 1)))
    
    # 1. Force the first scene to start at line 1
    sorted_scenes[0]["start_line"] = 1
    
    # 2. Close any gaps in the middle
    for i in range(len(sorted_scenes) - 1):
        current_scene = sorted_scenes[i]
        next_scene = sorted_scenes[i + 1]
        
        actual_next_start = int(next_scene.get("start_line", current_scene.get("end_line", 1) + 1))
        
        # Extend the current scene's end_line to touch the next scene's start_line
        current_scene["end_line"] = actual_next_start - 1
            
    # 3. Force the final scene to end at the very last line
    sorted_scenes[-1]["end_line"] = total_lines
    
    return sorted_scenes



  
def extract_scene_inventories_node(state: PipelineState):  
    log.info("=== LANGGRAPH: Extracting per-scene inventories...")  
  
    scenes_raw = state.get("scenes_raw", []) or []  
    scene_inventories: Dict[str, Any] = {} 
    # Extract character bible from state to get canonical names   
    character_bible = state.get("character_bible", {}) or {}    
    canonical_names = list(character_bible.keys()) 

    # ── TEST LIMITER ──────────────────────────────────────────────────────────
    max_scenes = MAX_SCENES_DEBUG
    if max_scenes > 0:
        scenes_raw = scenes_raw[:max_scenes]
        log.info(f"[DEBUG] extract_scene_inventories_node: limiting to first {max_scenes} scenes.")
    # ─────────────────────────────────────────────────────────────────────────
  
    for raw_scene in scenes_raw:  
        scene_no = raw_scene["scene_number"]  
        log.info(f"  -> Inventory Scene {scene_no}: {raw_scene['scene_heading']}")  
  
        inventory = invoke_chain_with_retry(  
            inventory_chain,  
            {  
                "scene_heading": raw_scene["scene_heading"],  
                "scene_text": raw_scene["text"],  
                "canonical_character_names": json.dumps(canonical_names, ensure_ascii=False),
            },  
            label=f"inventory_scene_{scene_no:02d}",  
            retries=2,  
        )  
        scene_inventories[str(scene_no)] = inventory  
        time.sleep(RATE_LIMIT_SLEEP)  
  
    return {  
        "scene_inventories": scene_inventories,  
        "current_step": "scene_inventories_extracted",  
    }  
  
  
def plan_frames_node(state: PipelineState):
    log.info("=== LANGGRAPH: Planning frames scene by scene...")

    scenes_raw = state.get("scenes_raw", []) or []
    scene_inventories = state.get("scene_inventories", {}) or {}
    character_bible = state.get("character_bible", {}) or {}

    # Build once — reused for every scene
    canonical_names = list(character_bible.keys())
    log.info(f"Canonical character names: {canonical_names}")

    max_scenes = MAX_SCENES_DEBUG
    if max_scenes > 0:
        scenes_raw = scenes_raw[:max_scenes]

    planned_scenes: List[Dict[str, Any]] = []

    for raw_scene in scenes_raw:
        scene_no = raw_scene["scene_number"]
        inventory = scene_inventories.get(str(scene_no), {})
        relevant_chars = get_relevant_character_bible(raw_scene["text"], character_bible)

        log.info(f"  -> Plan Scene {scene_no}")
        scene_plan = invoke_chain_with_retry(
            frame_planner_chain,
            {
                "scene_heading": raw_scene["scene_heading"],
                "scene_inventory": json.dumps(inventory, indent=2, ensure_ascii=False),
                "character_bible": json.dumps(relevant_chars, indent=2, ensure_ascii=False),
                "canonical_character_names": json.dumps(canonical_names, ensure_ascii=False),
                "scene_text": raw_scene["text"],
            },
            label=f"plan_scene_{scene_no:02d}",
            retries=2,
        )

        scene_obj = build_scene_object(raw_scene, inventory, scene_plan, character_bible=character_bible)
        planned_scenes.append(scene_obj)
        log.info(f"     Planned frames: {len(scene_obj.get('frames', []))}")
        time.sleep(RATE_LIMIT_SLEEP)

    return {
        "planned_scenes": planned_scenes,
        "current_step": "frames_planned",
    }
  
  
def audit_and_repair_frames_node(state: PipelineState):  
    log.info("=== LANGGRAPH: Auditing and repairing frame plans...")  
  
    scenes_raw = state.get("scenes_raw", []) or []  
    scene_inventories = state.get("scene_inventories", {}) or {}  
    planned_scenes = state.get("planned_scenes", []) or [] 
    character_bible = state.get("character_bible", {}) or {}  # for canonical name normalization
    # Extract all canonical names once   
    canonical_names = list(character_bible.keys())
    # ── TEST LIMITER ──────────────────────────────────────────────────────────
    # planned_scenes is already sliced by plan_frames_node.
    # We slice scenes_raw here to keep the zip() aligned.
    max_scenes = MAX_SCENES_DEBUG
    if max_scenes > 0:
        scenes_raw = scenes_raw[:max_scenes]
        log.info(f"[DEBUG] audit_and_repair_frames_node: limiting scenes_raw to first {max_scenes} to match planned_scenes.")
    # ───────────────────────────────────────────────────────────────────────── 
  
    repaired_scenes: List[Dict[str, Any]] = []  
  
    for raw_scene, planned_scene in zip(scenes_raw, planned_scenes):  
        scene_no = raw_scene["scene_number"]  
        inventory = scene_inventories.get(str(scene_no), {}) 
        # Get only the characters relevant to this specific scene to save tokens 
        relevant_chars = get_relevant_character_bible(raw_scene["text"], character_bible)
        log.info(f"  -> Audit Scene {scene_no}")  
        audit = invoke_chain_with_retry(  
            frame_audit_chain,  
            {  
                "scene_heading": raw_scene["scene_heading"],  
                "canonical_character_names": json.dumps(canonical_names, ensure_ascii=False),
                "scene_inventory": json.dumps(inventory, indent=2, ensure_ascii=False),  
                "scene_plan": json.dumps(planned_scene, indent=2, ensure_ascii=False),  
                "scene_text": raw_scene["text"],  
            },  
            label=f"audit_scene_{scene_no:02d}",  
            retries=2,  
        )  
  
        current_scene = planned_scene  
        coverage_score = audit.get("coverage_score", 0)  
        needs_repair = bool(audit.get("needs_repair", False)) or bool(audit.get("missing_visual_facts"))  
  
        log.info(f"     Coverage score={coverage_score} | needs_repair={needs_repair}")  
  
        if needs_repair or coverage_score < 90:  
            log.info(f"     Repairing Scene {scene_no}")  
            repaired_plan = invoke_chain_with_retry(  
                frame_repair_chain,  
                {  
                    "scene_heading": raw_scene["scene_heading"],  
                    "canonical_character_names": json.dumps(canonical_names, ensure_ascii=False),
                    "character_bible": json.dumps(relevant_chars, indent=2, ensure_ascii=False),
                    "scene_inventory": json.dumps(inventory, indent=2, ensure_ascii=False),  
                    "scene_plan": json.dumps(planned_scene, indent=2, ensure_ascii=False),  
                    "audit_report": json.dumps(audit, indent=2, ensure_ascii=False),  
                    "scene_text": raw_scene["text"],  
                },  
                label=f"repair_scene_{scene_no:02d}",  
                retries=2,  
            )  
            current_scene = build_scene_object(raw_scene, inventory, repaired_plan)  
  
        repaired_scenes.append(current_scene)  
        time.sleep(RATE_LIMIT_SLEEP)  
  
    breakdown = ScreenplayBreakdown(  
        title=state.get("screenplay_title") or "Untitled Screenplay",  
        total_scenes=len(repaired_scenes),  
        visual_style=DEFAULT_VISUAL_STYLE,  
        color_palette=DEFAULT_COLOR_PALETTE,  
        aspect_ratio=DEFAULT_ASPECT_RATIO,  
        scenes=[Scene(**scene) for scene in repaired_scenes],  
    ).model_dump()  
  
    return {  
        "breakdown": breakdown,  
        "current_step": "frames_audited_and_repaired",  
    }  


def generate_references_node(state, config):
    """
    Phase A: Canonicalize user-uploaded photos → storyboard sketches.
    Phase B: Auto-generate refs for characters with no uploaded image.

    The result is that reference_images contains ONLY storyboard sketches,
    never raw photographs, before frame generation begins.
    """
    from new import (   # replace "new" with your actual module name
        generate_reference_image,
        RATE_LIMIT_SLEEP,
        DEFAULT_VISUAL_STYLE,
        DEFAULT_COLOR_PALETTE,
    )
    from langchain_core.runnables.config import RunnableConfig

    log.info("=== LANGGRAPH: Generating character reference images (with canonicalization)...")

    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")
    character_bible  = state.get("character_bible", {}) or {}
    breakdown        = state.get("breakdown", {}) or {}
    visual_style     = breakdown.get("visual_style", DEFAULT_VISUAL_STYLE)
    color_palette    = breakdown.get("color_palette", DEFAULT_COLOR_PALETTE)
    reference_images = state.get("reference_images", {}) or {}
    user_uploaded_images = state.get("user_uploaded_images", {}) or {}

    # ── PHASE A: Canonicalize every user-uploaded photo ───────────────────
    for uploaded_char_name, uploaded_url in user_uploaded_images.items():
        if not uploaded_url:
            continue

        # Exact match only — the frontend sends the exact bible key name
        # because /api/upload-character-reference returns the character list.
        if uploaded_char_name not in character_bible:
            log.warning(
                f"  -> Uploaded name '{uploaded_char_name}' not in character bible. "
                f"Skipping canonicalization."
            )
            continue

        char_data = character_bible[uploaded_char_name]
        log.info(
            f"  -> Canonicalizing user-uploaded photo for '{uploaded_char_name}': "
            f"{uploaded_url}"
        )

        canonical_url = canonicalize_uploaded_reference(
            char_name=uploaded_char_name,
            char_data=char_data,
            uploaded_blob_url=uploaded_url,
            visual_style=visual_style,
            color_palette=color_palette,
            thread_id=thread_id,
        )

        if canonical_url:
            reference_images[uploaded_char_name] = canonical_url
            log.info(
                f"  -> Canonical sketch stored for '{uploaded_char_name}': "
                f"{canonical_url}"
            )
        else:
            # Canonicalization failed — fall through to auto-generation below.
            log.warning(
                f"  -> Canonicalization FAILED for '{uploaded_char_name}'. "
                f"Will auto-generate a reference instead."
            )

        time.sleep(RATE_LIMIT_SLEEP)

    # ── PHASE B: Auto-generate refs for characters with no reference yet ───
    for char_name, char_data in character_bible.items():
        if reference_images.get(char_name):
            log.info(f"  -> Skipping '{char_name}' (reference already exists).")
            continue

        log.info(f"  -> Auto-generating storyboard reference for: {char_name}")
        ref_url = generate_reference_image(
            char_data, visual_style, color_palette, thread_id
        )
        reference_images[char_name] = ref_url if ref_url else None
        time.sleep(RATE_LIMIT_SLEEP)

    return {
        "reference_images": reference_images,
        "current_step": "character_references_generated",
    }
  
  
def generate_scene_references_node(state: PipelineState, config: RunnableConfig):  
    log.info("=== LANGGRAPH: Generating scene/location reference images...")  
  
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")  
    breakdown = state.get("breakdown", {}) or {}  
    scene_reference_images = state.get("scene_reference_images", {}) or {}  
  
    for scene in breakdown.get("scenes", []):  
        scene_no = scene["scene_number"]  
        key = str(scene_no)  
  
        if key not in scene_reference_images or not scene_reference_images[key]:  
            log.info(f"  -> Scene ref: Scene {scene_no}")  
            scene_ref_url = generate_scene_reference(scene, breakdown, thread_id)  
            scene_reference_images[key] = scene_ref_url if scene_ref_url else None  
            if scene_ref_url:  
                scene["scene_reference_url"] = scene_ref_url  
            time.sleep(RATE_LIMIT_SLEEP)  
        else:  
            scene["scene_reference_url"] = scene_reference_images[key]  
  
    return {  
        "breakdown": breakdown,  
        "scene_reference_images": scene_reference_images,  
        "current_step": "scene_references_generated",  
    }  
  
  
def generate_frames_node(state: PipelineState, config: RunnableConfig):  
    log.info("=== LANGGRAPH: Generating frame images...")  
  
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")  
    breakdown = state.get("breakdown", {}) or {}  
    character_bible = state.get("character_bible", {}) or {}  
    reference_images = state.get("reference_images", {}) or {}  
    user_uploaded_images = state.get("user_uploaded_images", {}) or {}   # NEW
  
    total_frames = sum(len(scene.get("frames", [])) for scene in breakdown.get("scenes", []))  
    done = 0  
  
    for scene in breakdown.get("scenes", []):  
        prev_frame = None  
        scene_reference_url = scene.get("scene_reference_url")  
  
        for frame in scene.get("frames", []):  
            if frame.get("image_generated") and frame.get("image_url"):  
                done += 1  
                prev_frame = frame  
                continue  
  
            done += 1  
            log.info(f"    [{done}/{total_frames}] {frame['frame_id']} - {frame['title']}")  
  
            img_url, method, final_prompt = generate_frame_image(  
                frame=frame,  
                scene=scene,  
                breakdown=breakdown,  
                character_bible=character_bible,  
                reference_images=reference_images,  
                scene_reference_url=scene_reference_url,  
                prev_frame=prev_frame,  
                thread_id=thread_id, 
                user_uploaded_images=user_uploaded_images,   # NEW
            )  
  
            frame["image_url"] = img_url  
            frame["generation_method"] = method  
            frame["final_prompt"] = final_prompt  
            frame["image_generated"] = img_url is not None  
  
            if img_url:  
                prev_frame = frame  
                log.info(f"       OK -> {img_url}")  
  
            time.sleep(RATE_LIMIT_SLEEP)  
  
    final_output = {  
        "screenplay_title": breakdown.get("title"),  
        "visual_style": breakdown.get("visual_style"),  
        "color_palette": breakdown.get("color_palette"),  
        "aspect_ratio": breakdown.get("aspect_ratio"),  
        "total_scenes": breakdown.get("total_scenes"),  
        "total_frames": total_frames,  
        "character_bible": character_bible,  
        "reference_images": reference_images,  
        "scene_reference_images": state.get("scene_reference_images", {}) or {},  
        "scenes": breakdown.get("scenes", []),  
    }  
  
    return {  
        "breakdown": breakdown,  
        "final_output": final_output,  
        "current_step": "frames_generated",  
    }  
  
  
def persist_output_node(state: PipelineState, config: RunnableConfig):  
    log.info("=== LANGGRAPH: Persisting final output...")  
  
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")  
    final_output = state.get("final_output", {}) or {}  
    breakdown = state.get("breakdown", {}) or {}  
  
    if projects_collection is not None:  
        try:  
            projects_collection.update_one(  
                {"thread_id": thread_id},  
                {  
                    "$set": {  
                        "thread_id": thread_id,  
                        "title": breakdown.get("title"),  
                        "status": "completed",  
                        "current_step": "completed",  
                        "final_breakdown": final_output,  
                        "updated_at": time.time(),  
                    }  
                },  
                upsert=True,  
            )  
            log.info(f"Saved final output to Cosmos/MongoDB for thread_id={thread_id}")  
        except Exception as e:  
            log.error(f"Failed saving final output to Cosmos/MongoDB: {e}")  
    else:  
        log.warning("Skipping DB persistence because Mongo/Cosmos is not configured.")  
  
    return {  
        "current_step": "completed",  
        "final_output": final_output,  
    }  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# GRAPH  
# ══════════════════════════════════════════════════════════════════════════════  
  
workflow = StateGraph(PipelineState)  
  
workflow.add_node("preprocess_script", preprocess_script_node)  
workflow.add_node("extract_characters", extract_characters_node)  
workflow.add_node("split_scenes", split_scenes_node)  
workflow.add_node("extract_scene_inventories", extract_scene_inventories_node)  
workflow.add_node("plan_frames", plan_frames_node)  
workflow.add_node("audit_and_repair_frames", audit_and_repair_frames_node)  
workflow.add_node("generate_references", generate_references_node)  
workflow.add_node("generate_scene_references", generate_scene_references_node)  
workflow.add_node("generate_frames", generate_frames_node)  
workflow.add_node("persist_output", persist_output_node)  
  
workflow.add_edge(START, "preprocess_script")  
workflow.add_edge("preprocess_script", "extract_characters")  
workflow.add_edge("extract_characters", "split_scenes")  
workflow.add_edge("split_scenes", "extract_scene_inventories")  
workflow.add_edge("extract_scene_inventories", "plan_frames")  
workflow.add_edge("plan_frames", "audit_and_repair_frames")  
workflow.add_edge("audit_and_repair_frames", "generate_references")  
workflow.add_edge("generate_references", "generate_scene_references")  
workflow.add_edge("generate_scene_references", "generate_frames")  
workflow.add_edge("generate_frames", "persist_output")  
workflow.add_edge("persist_output", END)  
  
if mongo_client is not None:  
    try:  
        memory = MongoDBSaver(mongo_client, db_name="script_duniya_db")  
        log.info("Using MongoDBSaver for LangGraph checkpoints.")  
    except Exception as e:  
        log.warning(f"MongoDBSaver failed, using MemorySaver instead: {e}")  
        memory = MemorySaver()  
else:  
    memory = MemorySaver()  
    log.info("Using MemorySaver for LangGraph checkpoints.")  
  

app_graph = workflow.compile(
    checkpointer=memory,
    interrupt_before=["generate_references"]  # pause here to wait for user uploads
) 
  
