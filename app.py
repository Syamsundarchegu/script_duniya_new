import os  
import json  
import time  
import base64  
import logging  
from typing import Optional, List, Dict, Any, TypedDict, Tuple  
# from test import stamp_metadata_on_image
import requests  
from pydantic import BaseModel, Field  
from pymongo import MongoClient  
from azure.storage.blob import BlobServiceClient, ContentSettings  
  
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
  
  
def guess_title(screenplay: str) -> str:  
    for line in screenplay.splitlines():  
        if line.strip():  
            return line.strip()[:120]  
    return "Untitled Screenplay"  
  
  
# def add_line_numbers(screenplay: str) -> Tuple[List[str], str]:  
#     numbered_lines = []  
#     for idx, line in enumerate(screenplay.splitlines(), start=1):  
#         numbered_lines.append(f"{idx:04d}: {line}")  
#     return numbered_lines, "\n".join(numbered_lines)  


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
  

AZURE_OPENAI_ENDPOINT= os.getenv("AZURE_OPENAI_ENDPOINT")


AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")



AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT ="gpt-4.1"
# AZURE_OPENAI_DEPLOYMENT ="gpt-5.4"

  

IMAGE_ENDPOINT       = os.getenv("IMAGE_ENDPOINT")
IMAGE_API_KEY        = os.getenv("IMAGE_API_KEY")

IMAGE_DEPLOYMENT     = "gpt-image-1.5"
IMAGE_API_VERSION    = os.getenv("IMAGE_API_VERSION")

  
DEFAULT_VISUAL_STYLE = env("DEFAULT_VISUAL_STYLE", "Single standalone monochrome storyboard sketch")  
DEFAULT_COLOR_PALETTE = env("DEFAULT_COLOR_PALETTE", "Black and White")  
DEFAULT_ASPECT_RATIO = env("DEFAULT_ASPECT_RATIO", "16:9")  
STORYBOARD_DENSITY = env("STORYBOARD_DENSITY", "dense")  
  
CHARACTER_IMAGE_SIZES = env_csv("CHARACTER_IMAGE_SIZES", "1024x1536,1024x1024")  
SCENE_IMAGE_SIZES = env_csv("SCENE_IMAGE_SIZES", "1536x1024,1024x1024")  
FRAME_IMAGE_SIZES = env_csv("FRAME_IMAGE_SIZES", "1536x1024,1024x1024")  
  
RATE_LIMIT_SLEEP = int(env("RATE_LIMIT_SLEEP", "2"))  
MAX_RETRIES = int(env("MAX_RETRIES", "4"))  
MAX_REWRITE_ATTEMPTS = int(env("MAX_REWRITE_ATTEMPTS", "4"))  
MAX_CHARACTER_REFS_PER_FRAME = int(env("MAX_CHARACTER_REFS_PER_FRAME", "2"))  
  
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
  
# llm = AzureChatOpenAI(  
#     azure_deployment=AZURE_OPENAI_DEPLOYMENT,  
#     api_version=AZURE_OPENAI_API_VERSION,  
#     temperature=0,  
#     api_key=AZURE_OPENAI_API_KEY,  
#     azure_endpoint=AZURE_OPENAI_ENDPOINT,  
#     top_p=1,  
#     timeout=120,  
#     max_retries=2,  
# )  


llm = AzureChatOpenAI(  
    azure_deployment=AZURE_OPENAI_DEPLOYMENT,  
    api_version=AZURE_OPENAI_API_VERSION,  
    # temperature=0,  
    api_key=AZURE_OPENAI_API_KEY,  
    azure_endpoint=AZURE_OPENAI_ENDPOINT,  
    top_p=1,  
    timeout=120,  
    max_retries=2,  
)  
  
# rewriter_llm = AzureChatOpenAI(  
#     azure_deployment=AZURE_OPENAI_DEPLOYMENT,  
#     api_version=AZURE_OPENAI_API_VERSION,  
#     temperature=0,  
#     api_key=AZURE_OPENAI_API_KEY,  
#     azure_endpoint=AZURE_OPENAI_ENDPOINT,  
#     top_p=1,  
#     timeout=120,  
#     max_retries=2,  
# )  

rewriter_llm = AzureChatOpenAI(  
    azure_deployment=AZURE_OPENAI_DEPLOYMENT,  
    api_version=AZURE_OPENAI_API_VERSION,  
    # temperature=0,  
    api_key=AZURE_OPENAI_API_KEY,  
    azure_endpoint=AZURE_OPENAI_ENDPOINT,  
    top_p=1,  
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
    composition: str  
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
You are a professional casting director and visual development artist.  
  
Task:  
Extract a reliable visual character bible from the screenplay.  
  
Rules:  
1. Include every named recurring or meaningful character.  
2. Merge aliases and nicknames into one canonical character.  
3. visual_summary must be one dense visual sentence for image prompting.  
4. Use only safe language in visual_summary.  
5. Do not invent unsupported details.  
6. If a detail is unclear, use "As established".  
7. Output valid JSON only.  
  
{format_instructions}  
"""  
  
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
5. No gaps. No overlaps. start_line and end_line are inclusive.  
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
  
{format_instructions}  
"""  
  
inventory_prompt = ChatPromptTemplate.from_messages([  
    ("system", INVENTORY_SYSTEM),  
    ("human", "Scene heading: {scene_heading}\n\nNumbered scene text:\n{scene_text}")  
]).partial(format_instructions=inventory_parser.get_format_instructions())  
  
inventory_chain = inventory_prompt | llm | inventory_parser  
  
  
FRAME_PLANNER_SYSTEM = """  
You are an exhaustive storyboard frame planner.  
  
You will receive:  
1. one numbered screenplay scene  
2. a scene inventory  
3. a relevant character bible subset  
  
Goal:  
Convert the scene into storyboard frames, not summaries.  
  
Primary rule:  
Collectively, the frames must cover every explicit visual fact in the source scene.  
  
Rules:  
1. Work only from the provided scene and inventory.  
2. Understand non-English source text if needed, but output all JSON in English.  
3. Do not invent props, blocking, reactions, or camera moves.  
4. Create a new frame whenever:  
   - a character enters or exits  
   - a new object becomes important  
   - a new physical action begins  
   - visual focus shifts to reaction, face, hand, object, insert, or reveal  
   - blocking changes  
   - shot size or angle logically changes  
   - an emotionally distinct beat should be shown  
5. Preserve all specific visual nouns.  
6. Put exact dialogue only in lines. If none, use "None".  
7. Every frame must include source_spans with line numbers.  
8. If something is ambiguous, put it in uncertain_details. Never invent.  
9. Every explicit visual fact must appear in action, setting_detail, must_show, or props.  
10. Storyboard density mode is {storyboard_density}. In dense mode, do not compress meaningful beats.  
11. Output valid JSON only.  
  
{format_instructions}  
"""  
  
frame_planner_prompt = ChatPromptTemplate.from_messages([  
    ("system", FRAME_PLANNER_SYSTEM),  
    (  
        "human",  
        "Scene heading: {scene_heading}\n\n"  
        "Scene inventory:\n{scene_inventory}\n\n"  
        "Relevant character bible:\n{character_bible}\n\n"  
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
  
Your job:  
find omissions, merged beats, invented details, and continuity risks.  
  
Rules:  
1. If a specific visual fact in the source is not represented, mark it missing.  
2. If distinct beats are compressed together in dense storyboard mode, mark them.  
3. If any planned detail is unsupported by source text or inventory, mark it invented.  
4. Be exhaustive.  
5. Output valid JSON only.  
  
{format_instructions}  
"""  
  
frame_audit_prompt = ChatPromptTemplate.from_messages([  
    ("system", FRAME_AUDIT_SYSTEM),  
    (  
        "human",  
        "Scene heading: {scene_heading}\n\n"  
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
  
Repair the scene plan so that:  
- missing visual facts are covered  
- merged beats are split into separate frames  
- invented details are removed  
- continuity notes are improved  
- all frames remain grounded in source lines  
  
Rules:  
1. Preserve good frames where possible.  
2. Add frames when needed.  
3. Keep exact dialogue in lines.  
4. Do not invent unsupported actions or props.  
5. Output valid JSON only.  
  
{format_instructions}  
"""  
  
frame_repair_prompt = ChatPromptTemplate.from_messages([  
    ("system", FRAME_REPAIR_SYSTEM),  
    (  
        "human",  
        "Scene heading: {scene_heading}\n\n"  
        "Scene inventory:\n{scene_inventory}\n\n"  
        "Current scene plan:\n{scene_plan}\n\n"  
        "Audit report:\n{audit_report}\n\n"  
        "Numbered scene text:\n{scene_text}"  
    )  
]).partial(format_instructions=scene_plan_parser.get_format_instructions())  
  
frame_repair_chain = frame_repair_prompt | llm | scene_plan_parser  
  
  
REWRITER_SYSTEM = """  
You are a professional storyboard prompt rewriter.  
  
Your job:  
Rewrite a raw image prompt into a SAFE prompt while preserving the visual staging as much as possible.  
  
CRITICAL RULES:  
1. Keep it as one single standalone image.  
2. Never suggest a collage, page layout, storyboard sheet, multiple panels, diptych, triptych, or split-screen.  
3. Use exactly these labeled blocks:  
  
SUBJECT:  
ACTION:  
ENVIRONMENT:  
CAMERA:  
STYLE:  
TECHNICAL:  
  
Output only the structured brief.  
"""  
  
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
  
    try:  
        rewritten = rewriter_chain.invoke({"raw_prompt": raw_prompt + extra})  
        return rewritten.strip()  
    except Exception as e:  
        log.error(f"Prompt rewrite failed: {e}")  
        return (  
            "SUBJECT: A safe single standalone scene composition.\n"  
            "ACTION: Minimal safe implied action only.\n"  
            "ENVIRONMENT: Atmospheric location details and clear readable staging.\n"  
            "CAMERA: One single composition only, not a collage or multi-panel layout.\n"  
            "STYLE: Single standalone monochrome storyboard sketch, rough pencil drawing, black and white.\n"  
            "TECHNICAL: Generate exactly one image only. No split-screen, no multiple panels, no page layout, no collage."  
        )  
  
  
def _upload_b64_to_blob(b64_data: str, blob_name: str) -> str:  
    if container_client is None:  
        raise RuntimeError("Azure Blob Storage is not configured.")  
  
    img_bytes = base64.b64decode(b64_data)  
    blob_client = container_client.get_blob_client(blob_name)  
    blob_client.upload_blob(  
        img_bytes,  
        overwrite=True,  
        content_settings=ContentSettings(content_type="image/png"),  
    )  
    return blob_client.url  
  
  
def _download_blob_as_bytes(blob_url: str) -> bytes:  
    if container_client is None:  
        raise RuntimeError("Azure Blob Storage is not configured.")  
  
    clean_url = blob_url.split("?")[0]  
    marker = f"/{BLOB_CONTAINER}/"  
    if marker not in clean_url:  
        raise ValueError("Blob URL does not match configured container.")  
  
    blob_name = clean_url.split(marker, 1)[1]  
    blob_client = container_client.get_blob_client(blob_name)  
    stream = blob_client.download_blob()  
    return stream.readall()  
  
  
def _is_content_filter_error(response: requests.Response) -> Tuple[bool, str]:  
    try:  
        body = response.json()  
        error = body.get("error", {})  
        code = error.get("code", "")  
        msg = str(error.get("message", "")).lower()  
        if code == "contentFilter" or "content filter" in msg or "safety system" in msg:  
            if "generated image" in msg:  
                return True, "output_blocked"  
            return True, "prompt_blocked"  
        if response.status_code == 400 and "filter" in msg:  
            return True, "prompt_blocked"  
    except Exception:  
        pass  
    return False, ""  
  
  
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
# , frame_metadata: Optional[dict] = None 
def _call_with_retry_and_rewrite(build_request_fn, raw_prompt: str, label: str, blob_name: str ) -> Optional[str]:  
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
                is_filter, filter_type = _is_content_filter_error(response)  
                if is_filter:  
                    rewrite_attempt += 1  
                    log.warning(f"[{label}] Content filter ({filter_type}) -> rewrite attempt {rewrite_attempt}")  
                    if rewrite_attempt > MAX_REWRITE_ATTEMPTS:  
                        log.error(f"[{label}] Exhausted rewrite attempts.")  
                        return None  
                    current_prompt = safe_rewrite_prompt(raw_prompt, attempt=rewrite_attempt)  
                    time.sleep(1)  
                    continue  
  
            response.raise_for_status()  
            data = response.json()  
            b64 = data["data"][0]["b64_json"] 

        #    # --- NEW INTERCEPTION BLOCK ---
        #     if frame_metadata:
        #         try:
        #             b64 = stamp_metadata_on_image(b64, frame_metadata)
        #             log.info(f"[{label}] Successfully burned metadata onto image.")
        #         except Exception as stamp_err:
        #             log.error(f"[{label}] Failed to stamp metadata: {stamp_err}")
        #     # ------------------------------




            blob_url = _upload_b64_to_blob(b64, blob_name)  
            log.info(f"[{label}] Uploaded -> {blob_url}")  
            return blob_url  
  
        except requests.exceptions.HTTPError as e:  
            status = e.response.status_code if e.response else 0  
            is_filter, filter_type = _is_content_filter_error(e.response) if e.response else (False, "")  
            if is_filter:  
                rewrite_attempt += 1  
                log.warning(f"[{label}] Filter error HTTP {status} ({filter_type}), rewrite attempt {rewrite_attempt}")  
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
    prop_block = "; ".join(scene.get("prop_inventory", [])) or "As established"  
  
    raw_prompt = (  
        f"SUBJECT: Empty location environment only. No people.\n"  
        f"ACTION: Static establishing environment.\n"  
        f"ENVIRONMENT: {scene['location']}, {scene['time_of_day']}. Lighting mood: {scene['lighting_mood']}. "  
        f"Visible environment details: {env_block}. Key props present: {prop_block}.\n"  
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
  
  
def generate_frame_image(  
    frame: Dict[str, Any],  
    scene: Dict[str, Any],  
    breakdown: Dict[str, Any],  
    character_bible: Dict[str, Any],  
    reference_images: Dict[str, str],  
    scene_reference_url: Optional[str] = None,  
    prev_frame: Optional[Dict[str, Any]] = None,  
    thread_id: str = "default_thread",  
) -> Tuple[Optional[str], str, str]:  
    frame_id = frame["frame_id"]  
    label = f"FRAME:{frame_id}"  
    blob_name = f"{thread_id}/frames/{sanitize_for_filename(frame_id)}.png"  
  
    characters_present = frame.get("characters_present", [])  
    primary_refs: List[Tuple[str, str]] = []  
  
    for char_name in characters_present:  
        ref_url = reference_images.get(char_name)  
        if ref_url:  
            primary_refs.append((char_name, ref_url))  
        if len(primary_refs) >= MAX_CHARACTER_REFS_PER_FRAME:  
            break  
  
    files_payload: List[Tuple[str, bytes]] = []  
    invariant_lines: List[str] = []  
  
    ref_index = 1  
    for char_name, ref_url in primary_refs:  
        invariant_lines.append(  
            f"- Reference image {ref_index} is the canonical character reference for {char_name}. Preserve exact face, hair, build, clothing, and age impression."  
        )  
        files_payload.append((f"char_{ref_index}.png", _download_blob_as_bytes(ref_url)))  
        ref_index += 1  
  
    if scene_reference_url:  
        invariant_lines.append(  
            f"- Reference image {ref_index} is the location reference. Preserve environment layout, prop placement, and lighting mood."  
        )  
        files_payload.append(("scene_ref.png", _download_blob_as_bytes(scene_reference_url)))  
        ref_index += 1  
  
    use_prev = should_use_prev_frame_anchor(prev_frame, frame)  
    if use_prev and prev_frame and prev_frame.get("image_url"):  
        invariant_lines.append(  
            f"- Reference image {ref_index} is the previous frame. Preserve continuity where appropriate without turning the output into a multi-panel layout."  
        )  
        files_payload.append(("prev_frame.png", _download_blob_as_bytes(prev_frame["image_url"])))  
  
    invariant_block = "\n".join(invariant_lines) if invariant_lines else "- Maintain internal visual consistency."  
  
    subject_parts = []  
    for char_name in characters_present:  
        char_data = character_bible.get(char_name)  
        if char_data:  
            subject_parts.append(f"{char_name}: {char_data.get('visual_summary', 'As established')}")  
        else:  
            subject_parts.append(f"{char_name}: As established")  
    subject_block = " | ".join(subject_parts) if subject_parts else "No visible person; environment-focused image."  
  
    must_show = "; ".join(frame.get("must_show", [])) or "As established"  
    props = "; ".join(frame.get("props", [])) or "As established"  
    env_inv = "; ".join(scene.get("environment_inventory", [])) or "As established"  
  
    raw_prompt = (  
        f"SUBJECT: {subject_block}\n"  
        f"ACTION: {frame['action']}\n"  
        f"ENVIRONMENT: Location: {scene['location']}, {scene['time_of_day']}. {frame['setting_detail']} "  
        f"Environment inventory: {env_inv}.\n"  
        f"CAMERA: One single composition only. Shot type: {frame['shot_type']}. Angle: {frame['angle']}. "  
        f"Composition: {frame['composition']}. Tone: {frame['tone']}.\n"  
        f"STYLE: Single standalone monochrome storyboard sketch, rough pencil line art, black and white. "  
        f"Visual style: {breakdown.get('visual_style', DEFAULT_VISUAL_STYLE)}. "  
        f"Palette: {breakdown.get('color_palette', DEFAULT_COLOR_PALETTE)}.\n"  
        f"TECHNICAL: Must show: {must_show}. Props visible: {props}. Continuity: {frame.get('continuity_notes', 'Maintain continuity.')}. "  
        f"{single_image_rules_text()} "  
        f"No drawn page layout. No multiple internal frames. No contact sheet. No repeated figure. "  
        f"INVARIANTS:\n{invariant_block}\n"  
        f"Strictly monochrome, zero color, visible paper texture, not photorealistic, not 3D."  
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

    # # --- NEW: Construct the metadata dictionary for the burn-in ---
    # metadata_to_burn = {
    #     "scene": scene.get("scene_number"),
    #     "frame_id": frame.get("frame_id"),
    #     "location": scene.get("location"),
    #     "time_of_day": scene.get("time_of_day"),
    #     "shot_type": frame.get("shot_type"),
    #     "composition": frame.get("composition"),
    #     "action": frame.get("action")
    # }




    # <-- Added here

    # result_url = _call_with_retry_and_rewrite(build_request, raw_prompt, label, blob_name,frame_metadata=metadata_to_burn )
    result_url = _call_with_retry_and_rewrite(build_request, raw_prompt, label, blob_name)

    #   
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
  
  
def build_scene_object(raw_scene: Dict[str, Any], inventory: Dict[str, Any], scene_plan: Dict[str, Any]) -> Dict[str, Any]:  
    frames = []  
    for idx, frame in enumerate(scene_plan.get("frames", []), start=1):  
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
  
  
def extract_scene_inventories_node(state: PipelineState):  
    log.info("=== LANGGRAPH: Extracting per-scene inventories...")  
  
    scenes_raw = state.get("scenes_raw", []) or []  
    scene_inventories: Dict[str, Any] = {}  
  
    for raw_scene in scenes_raw:  
        scene_no = raw_scene["scene_number"]  
        log.info(f"  -> Inventory Scene {scene_no}: {raw_scene['scene_heading']}")  
  
        inventory = invoke_chain_with_retry(  
            inventory_chain,  
            {  
                "scene_heading": raw_scene["scene_heading"],  
                "scene_text": raw_scene["text"],  
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
                "scene_text": raw_scene["text"],  
            },  
            label=f"plan_scene_{scene_no:02d}",  
            retries=2,  
        )  
  
        scene_obj = build_scene_object(raw_scene, inventory, scene_plan)  
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
  
    repaired_scenes: List[Dict[str, Any]] = []  
  
    for raw_scene, planned_scene in zip(scenes_raw, planned_scenes):  
        scene_no = raw_scene["scene_number"]  
        inventory = scene_inventories.get(str(scene_no), {})  
  
        log.info(f"  -> Audit Scene {scene_no}")  
        audit = invoke_chain_with_retry(  
            frame_audit_chain,  
            {  
                "scene_heading": raw_scene["scene_heading"],  
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
  
  
def generate_references_node(state: PipelineState, config: RunnableConfig):  
    log.info("=== LANGGRAPH: Generating full-body character reference images...")  
  
    thread_id = config.get("configurable", {}).get("thread_id", "default_thread")  
    character_bible = state.get("character_bible", {}) or {}  
    breakdown = state.get("breakdown", {}) or {}  
  
    visual_style = breakdown.get("visual_style", DEFAULT_VISUAL_STYLE)  
    color_palette = breakdown.get("color_palette", DEFAULT_COLOR_PALETTE)  
  
    reference_images = state.get("reference_images", {}) or {}  
  
    for char_name, char_data in character_bible.items():  
        if char_name not in reference_images or not reference_images[char_name]:  
            log.info(f"  -> Character ref: {char_name}")  
            ref_url = generate_reference_image(char_data, visual_style, color_palette, thread_id)  
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
  
app_graph = workflow.compile(checkpointer=memory)  
  
  
# ══════════════════════════════════════════════════════════════════════════════  
# OPTIONAL LOCAL TEST  
# ══════════════════════════════════════════════════════════════════════════════  
  
# if __name__ == "__main__":  
#     sample_screenplay = env("SAMPLE_SCREENPLAY_TEXT")  
#     if not sample_screenplay:  
#         print("Set SAMPLE_SCREENPLAY_TEXT env var to run a local test.")  
#     else:  
#         result = app_graph.invoke(  
#             {  
#                 "screenplay_text": sample_screenplay,  
#                 "screenplay_title": None,  
#                 "numbered_lines": None,  
#                 "numbered_screenplay_text": None,  
#                 "scene_boundaries": None,  
#                 "scenes_raw": None,  
#                 "character_bible": None,  
#                 "scene_inventories": None,  
#                 "planned_scenes": None,  
#                 "breakdown": None,  
#                 "reference_images": None,  
#                 "scene_reference_images": None,  
#                 "final_output": None,  
#                 "current_step": "start",  
#             },  
#             config={"configurable": {"thread_id": "local_test_thread"}},  
#         )  
#         print(json.dumps(result.get("final_output", {}), indent=2, ensure_ascii=False))  