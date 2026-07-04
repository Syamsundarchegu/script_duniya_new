# backend.py
from fastapi import FastAPI, HTTPException, Body, File, UploadFile,BackgroundTasks, Depends, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uuid
import os
from pypdf import PdfReader
import docx
import io
import copy
from datetime import datetime, timedelta, timezone
from fastapi.concurrency import run_in_threadpool
import json
#Authentication required imports
import bcrypt
from jose import JWTError, jwt
from typing import Optional
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
# from test import script_db
from new import script_db, sanitize_for_filename
from azure.servicebus import ServiceBusClient, ServiceBusMessage

# Azure Storage Imports for SAS generation
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions, ContentSettings

# from test import app_graph, projects_collection
from new import app_graph, projects_collection

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Script Duniya Pipeline API")

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")



SERVICE_BUS_CONN_STR = os.getenv("SERVICE_BUS_CONN_STR")
QUEUE_NAME = "pipeline-jobs"



# --- SAS TOKEN CONFIGURATION ---
BLOB_CONN_STR = os.getenv("BLOB_CONN_STR")
BLOB_CONTAINER = "script-duniya-images"
blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONN_STR)

def append_sas_to_url(url: str) -> str:
    """Generates a 2-hour SAS token and appends it to a private blob URL."""
    if not url or BLOB_CONTAINER not in url:
        return url
        
    # FIX: Strip any existing SAS token to prevent double-stacking
    clean_url = url.split("?")[0]
        
    try:
        blob_name = clean_url.split(f"/{BLOB_CONTAINER}/")[1]
        
        sas_token = generate_blob_sas(
            account_name=blob_service_client.account_name,
            container_name=BLOB_CONTAINER,
            blob_name=blob_name,
            account_key=blob_service_client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=2)
        )
        return f"{clean_url}?{sas_token}"
    except Exception as e:
        print(f"SAS Generation Error: {e}")
        return clean_url

def inject_sas_tokens(data: dict) -> dict:
    """Recursively walks the dictionary to append SAS tokens to frontend image URLs."""
    # 1. Update reference images
    refs = data.get("reference_images", {})
    if isinstance(refs, dict):
        for char, url in refs.items():
            if url:
                refs[char] = append_sas_to_url(url)

    # 2. Update breakdown scenes -> frames (was wrongly looking for 'subscenes')
    breakdown = data.get("breakdown") or data.get("final_breakdown") or {}
    for scene in breakdown.get("scenes", []):
        for frame in scene.get("frames", []):          # ← was "subscenes"
            if frame.get("image_url"):
                frame["image_url"] = append_sas_to_url(frame["image_url"])

    # 3. Also walk final_output if present (it duplicates scene data)
    final_output = data.get("final_output", {})
    if final_output:
        for scene in final_output.get("scenes", []):
            for frame in scene.get("frames", []):
                if frame.get("image_url"):
                    frame["image_url"] = append_sas_to_url(frame["image_url"])
        ref2 = final_output.get("reference_images", {})
        if isinstance(ref2, dict):
            for char, url in ref2.items():
                if url:
                    ref2[char] = append_sas_to_url(url)

    return data

# --- API ENDPOINTS ---

class StartPipelineRequest(BaseModel):
    screenplay: str



# --- ADD THIS HELPER FUNCTION ---

# ---------------------------------------------------------
# 1. Sync helper for CPU-bound parsing
# ---------------------------------------------------------
def parse_document(content: bytes, filename: str) -> str:
    """Runs synchronous parsing logic without blocking the async event loop."""
    text = ""
    try:
        if filename.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(content))
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
                    
        elif filename.endswith(".docx"):
            doc = docx.Document(io.BytesIO(content))
            for para in doc.paragraphs:
                text += para.text + "\n"
                
        else:
            raise ValueError("Unsupported file type")
            
    except Exception as e:
        raise Exception(f"Error parsing document: {str(e)}")
        
    return text




# ---------------------------------------------------------
# 2. Async file reader and validator
# ---------------------------------------------------------
async def extract_text_from_file(file: UploadFile) -> str:
    filename = file.filename.lower()
    
    # Fail fast before reading the file
    if not filename.endswith((".pdf", ".docx")):
        raise HTTPException(
            status_code=400, 
            detail="Unsupported file type. Please upload a .pdf or .docx document."
        )
    
    # Read file into memory
    content = await file.read()
    
    # Basic Security: Limit file size to 10MB to prevent RAM exhaustion
    MAX_FILE_SIZE = 10 * 1024 * 1024 # 10 MB
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10MB.")
        
    # Offload the heavy parsing to a separate thread
    try:
        text = await run_in_threadpool(parse_document, content, filename)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract any text from the document.")
        
    return text







# --- AUTHENTICATION SETUP ---
SECRET_KEY = "your-super-secret-key-change-this-in-production" # Replace with a secure random string
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440 # 24 hours

# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")

# Add a users collection to your database
users_collection = script_db["users"] # Assuming script_db is imported from test.py

def verify_password(plain_password, hashed_password):
    # bcrypt requires bytes, so encode the strings to utf-8
    return bcrypt.checkpw(
        plain_password.encode('utf-8'), 
        hashed_password.encode('utf-8')
    )

def get_password_hash(password):
    # Hash the password with a freshly generated salt
    salt = bcrypt.gensalt()
    hashed_bytes = bcrypt.hashpw(password.encode('utf-8'), salt)
    # Decode back to a string so it can be safely stored in MongoDB
    return hashed_bytes.decode('utf-8')

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    user = users_collection.find_one({"username": username})
    if user is None:
        raise credentials_exception
    return user



class UserCreate(BaseModel):
    username: str
    password: str

@app.post("/api/register")
async def register(user: UserCreate):
    if users_collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="Username already registered")
    
    hashed_password = get_password_hash(user.password)
    users_collection.insert_one({"username": user.username, "password": hashed_password})
    return {"message": "User created successfully"}

@app.post("/api/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = users_collection.find_one({"username": form_data.username})
    if not user or not verify_password(form_data.password, user["password"]):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    access_token = create_access_token(data={"sub": user["username"]})
    return {"access_token": access_token, "token_type": "bearer"}
















# ── STEP 1: Extract characters only (called before upload step) ──────────────
@app.post("/api/extract-characters")
async def extract_characters_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """
    Phase 1 of the new two-phase flow.
    Extracts screenplay text, runs the pipeline up to character extraction,
    then returns the character names so the frontend can show upload slots.
    The pipeline is paused (interrupted) before generate_references_node.
    """
    screenplay_text = await extract_text_from_file(file)
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    # Log to Cosmos DB
    projects_collection.insert_one({
        "thread_id": thread_id,
        "username": current_user["username"],
        "status": "extracting_characters",
        "original_screenplay": screenplay_text
    })

    # Run the pipeline synchronously up to the interrupt point
    # The graph must be compiled with interrupt_before=["generate_references"]
    # in pipeline_graph.py for this to pause correctly.
    # We run this in a threadpool since app_graph.invoke is blocking.
    initial_state = {
        "screenplay_text": screenplay_text,
        "screenplay_title": None,
        "numbered_lines": None,
        "numbered_screenplay_text": None,
        "scene_boundaries": None,
        "scenes_raw": None,
        "character_bible": None,
        "scene_inventories": None,
        "planned_scenes": None,
        "breakdown": None,
        "reference_images": None,
        "scene_reference_images": None,
        "final_output": None,
        "current_step": "init",
        "user_uploaded_images": {},
    }

    try:
        await run_in_threadpool(app_graph.invoke, initial_state, config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Character extraction failed: {str(e)}")

    # Read the paused state to get character names
    state = app_graph.get_state(config)
    if not state or not state.values:
        raise HTTPException(status_code=500, detail="Pipeline state not found after extraction.")

    character_bible = state.values.get("character_bible") or {}
    character_names = list(character_bible.keys())

    # Update Cosmos DB status
    projects_collection.update_one(
        {"thread_id": thread_id},
        {"$set": {"status": "awaiting_character_uploads", "characters": character_names}}
    )

    return {
        "thread_id": thread_id,
        "characters": character_names,
        "message": f"Found {len(character_names)} characters. Upload references and call /api/resume-pipeline."
    }


# ── STEP 2a: Upload a single character reference image ───────────────────────
@app.post("/api/upload-character-reference")
async def upload_character_reference(
    thread_id: str = Form(...),
    character_name: str = Form(...),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """
    Accepts a user-uploaded character reference image.
    Uploads it to Azure Blob Storage and stores the URL
    in the pipeline state under user_uploaded_images.
    Call this once per character the user wants to upload.
    """
    # Validate file type
    allowed_types = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.content_type}. Use JPEG, PNG, or WebP."
        )

    # Read and validate file size (max 10MB)
    file_bytes = await file.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10MB.")

    # Build blob name — sanitize character name for safe path
    safe_char = character_name.strip().lower()
    safe_char = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in safe_char)
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "jpg"
    blob_name = f"{thread_id}/characters/uploaded_{safe_char}.{ext}"

    # Upload to Azure Blob Storage
    try:
        container_client = blob_service_client.get_container_client(BLOB_CONTAINER)
        blob_client = container_client.get_blob_client(blob_name)
        blob_client.upload_blob(
            file_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type=file.content_type),
        )
        blob_url = blob_client.url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Blob upload failed: {str(e)}")

    # Update the pipeline state — merge this upload into user_uploaded_images
    config = {"configurable": {"thread_id": thread_id}}
    state = app_graph.get_state(config)
    if not state or not state.values:
        raise HTTPException(status_code=404, detail="Pipeline state not found. Run /api/extract-characters first.")

    current_uploads = state.values.get("user_uploaded_images") or {}
    current_uploads[character_name] = blob_url

    app_graph.update_state(config, {"user_uploaded_images": current_uploads})

    return {
        "character_name": character_name,
        "blob_url": blob_url,
        "thread_id": thread_id,
        "message": f"Reference uploaded for {character_name}."
    }


# ── STEP 2b: Resume pipeline after uploads ───────────────────────────────────
class ResumePipelineRequest(BaseModel):
    thread_id: str


@app.post("/api/resume-pipeline")
async def resume_pipeline(
    req: ResumePipelineRequest,
    current_user: dict = Depends(get_current_user)
):
    config = {"configurable": {"thread_id": req.thread_id}}
    state = app_graph.get_state(config)
    if not state or not state.values:
        raise HTTPException(status_code=404, detail="Pipeline state not found.")

    projects_collection.update_one(
        {"thread_id": req.thread_id},
        {"$set": {"status": "queued_for_processing"}}
    )

    try:
        with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as client:
            with client.get_queue_sender(queue_name=QUEUE_NAME) as sender:
                message_payload = {
                    "thread_id": req.thread_id,
                    "action": "resume"
                }
                message = ServiceBusMessage(json.dumps(message_payload))
                sender.send_messages(message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to queue job: {str(e)}")

    return {
        "thread_id": req.thread_id,
        "message": "Pipeline queued. Poll /api/state/{thread_id} for progress."
    }


#################### commented on july 2026 ##################33
# @app.post("/api/resume-pipeline")
# async def resume_pipeline(
#     req: ResumePipelineRequest,
#     background_tasks: BackgroundTasks,
#     current_user: dict = Depends(get_current_user)
# ):
#     """
#     Phase 2 of the two-phase flow.
#     Called after the user has finished uploading character reference photos.
#     Resumes the paused pipeline from generate_references_node onwards.
#     """
#     config = {"configurable": {"thread_id": req.thread_id}}

#     # Verify state exists
#     state = app_graph.get_state(config)
#     if not state or not state.values:
#         raise HTTPException(status_code=404, detail="Pipeline state not found.")

#     # Update Cosmos DB status
#     projects_collection.update_one(
#         {"thread_id": req.thread_id},
#         {"$set": {"status": "processing"}}
#     )

#     # Resume pipeline in background — passes None as input so it
#     # continues from the interrupted node with the existing state
#     background_tasks.add_task(run_pipeline_background, None, config)

#     return {
#         "thread_id": req.thread_id,
#         "message": "Pipeline resumed. Poll /api/state/{thread_id} for progress."
#     }








@app.get("/")
async def serve_frontend():
    return FileResponse("static/open.html")


def run_pipeline_background(initial_state: dict, config: dict):
    """Runs the LangGraph pipeline in the background."""
    try:
        app_graph.invoke(initial_state, config)
    except Exception as e:
        print(f"Pipeline background task failed: {e}")








# @app.post("/api/start")
# async def start_pipeline(
#     file: UploadFile = File(...),
#     current_user: dict = Depends(get_current_user)
# ):
#     screenplay_text = await extract_text_from_file(file)
#     thread_id = str(uuid.uuid4())
    
#     # 1. Log the job in Cosmos DB
#     projects_collection.insert_one({
#         "thread_id": thread_id,
#         "username": current_user["username"],
#         "status": "queued", # Changed status to queued
#         "original_screenplay": screenplay_text
#     })
    
#     # 2. Push the job to Azure Service Bus
#     try:
#         with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as client:
#             with client.get_queue_sender(queue_name=QUEUE_NAME) as sender:
#                 message_payload = {
#                     "thread_id": thread_id,
#                     "screenplay_text": screenplay_text
#                 }
#                 message = ServiceBusMessage(json.dumps(message_payload))
#                 sender.send_messages(message)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to queue job: {str(e)}")
    
#     return {"thread_id": thread_id, "message": "Pipeline queued successfully"}




####################### commented on july 4 2026 #######################33
# @app.post("/api/start")
# async def start_pipeline(
#     background_tasks: BackgroundTasks,
#     file: UploadFile = File(...),
#     current_user: dict = Depends(get_current_user)
# ):
#     # Extract text from uploaded PDF or DOCX
#     screenplay_text = await extract_text_from_file(file)
    
#     thread_id = str(uuid.uuid4())
#     config = {"configurable": {"thread_id": thread_id}}
    
#     projects_collection.insert_one({
#         "thread_id": thread_id,
#         "username": current_user["username"],
#         "status": "started",
#         "original_screenplay": screenplay_text
#     })
    
#     initial_state = {"screenplay_text": screenplay_text, "current_step": "init"}
#     background_tasks.add_task(run_pipeline_background, initial_state, config)
    
#     return {"thread_id": thread_id, "message": "Pipeline started in background"}




@app.post("/api/start")
async def start_pipeline(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    screenplay_text = await extract_text_from_file(file)
    thread_id = str(uuid.uuid4())

    projects_collection.insert_one({
        "thread_id": thread_id,
        "username": current_user["username"],
        "status": "queued",
        "original_screenplay": screenplay_text
    })

    try:
        with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as client:
            with client.get_queue_sender(queue_name=QUEUE_NAME) as sender:
                message_payload = {
                    "thread_id": thread_id,
                    "action": "start",
                    "screenplay_text": screenplay_text
                }
                message = ServiceBusMessage(json.dumps(message_payload))
                sender.send_messages(message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to queue job: {str(e)}")

    return {"thread_id": thread_id, "message": "Pipeline queued successfully"}




# @app.post("/api/start")
# async def start_pipeline(
#     req: StartPipelineRequest,          # <-- Expect JSON body
#     background_tasks: BackgroundTasks,  # <-- Keep background tasks
#     current_user: dict = Depends(get_current_user) # <-- Keep authentication
# ):
#     # 1. Grab the raw text straight from the request
#     screenplay_text = req.screenplay
    
#     # 2. Proceed with the background pipeline logic
#     thread_id = str(uuid.uuid4())
#     config = {"configurable": {"thread_id": thread_id}}
    
#     projects_collection.insert_one({
#         "thread_id": thread_id, 
#         "username": current_user["username"], # Tie project to user
#         "status": "started",
#         "original_screenplay": screenplay_text
#     })
    
#     initial_state = {"screenplay_text": screenplay_text, "current_step": "init"}
    
#     # Hand the graph invocation off to a background task
#     background_tasks.add_task(run_pipeline_background, initial_state, config)
    
#     return {"thread_id": thread_id, "message": "Pipeline started in background"}







### upload version
# @app.post("/api/start")
# async def start_pipeline(
#     background_tasks: BackgroundTasks, 
#     file: UploadFile = File(...),
#     current_user: dict = Depends(get_current_user) # PROTECTED
# ):
#     screenplay_text = await extract_text_from_file(file)
#     thread_id = str(uuid.uuid4())
#     config = {"configurable": {"thread_id": thread_id}}
    
#     projects_collection.insert_one({
#         "thread_id": thread_id, 
#         "username": current_user["username"], # Tie project to user
#         "status": "started",
#         "original_screenplay": screenplay_text
#     })
    
#     initial_state = {"screenplay_text": screenplay_text, "current_step": "init"}
#     background_tasks.add_task(run_pipeline_background, initial_state, config)
    
#     return {"thread_id": thread_id, "message": "Pipeline started in background"}






#### original code

# @app.post("/api/start")
# async def start_pipeline(req: StartPipelineRequest):
#     thread_id = str(uuid.uuid4())
#     config = {"configurable": {"thread_id": thread_id}}
    
#     projects_collection.insert_one({
#         "thread_id": thread_id, 
#         "status": "started",
#         "original_screenplay": req.screenplay
#     })
    
#     initial_state = {"screenplay_text": req.screenplay, "current_step": "init"}
#     app_graph.invoke(initial_state, config)
    
#     return {"thread_id": thread_id, "message": "Pipeline started"}

@app.get("/api/state/{thread_id}")
async def get_state(thread_id: str, current_user: dict = Depends(get_current_user)):
    config = {"configurable": {"thread_id": thread_id}}
    state = app_graph.get_state(config)

    if not state or not state.values:
        raise HTTPException(status_code=404, detail="Thread not found in Checkpointer")

    # Deep copy to avoid mutating LangGraph's internal state
    safe_values = copy.deepcopy(state.values)

    # Inject SAS tokens into all image URLs
    values_with_sas = inject_sas_tokens(safe_values)

    # ── KEY FIX ──
    # Normalise: if final_output exists but has no 'scenes', pull them from breakdown
    final_output = values_with_sas.get("final_output") or {}
    breakdown    = values_with_sas.get("breakdown") or {}

    if not final_output.get("scenes") and breakdown.get("scenes"):
        # Build a merged final_output so the frontend always gets one consistent object
        final_output = {
            "screenplay_title": breakdown.get("title") or values_with_sas.get("screenplay_title"),
            "title":            breakdown.get("title") or values_with_sas.get("screenplay_title"),
            "visual_style":     breakdown.get("visual_style"),
            "color_palette":    breakdown.get("color_palette"),
            "aspect_ratio":     breakdown.get("aspect_ratio"),
            "total_scenes":     breakdown.get("total_scenes"),
            "scenes":           breakdown.get("scenes", []),
            "reference_images": values_with_sas.get("reference_images", {}),
        }
        values_with_sas["final_output"] = final_output

    return {
        "values":      values_with_sas,
        "next_nodes":  state.next,
        "is_finished": len(state.next) == 0
    }

# @app.post("/api/resume/{thread_id}")
# async def resume_pipeline(thread_id: str, updated_state: dict = Body(...)):
#     config = {"configurable": {"thread_id": thread_id}}
    
#     # Strip SAS tokens back off before updating state, just to be safe
#     # (Though your frontend JSON editor currently doesn't edit URLs anyway)
    
#     app_graph.update_state(config, updated_state)
#     app_graph.invoke(None, config)
    
#     projects_collection.update_one(
#         {"thread_id": thread_id},
#         {"$set": {"status": "processing"}}
#     )
    
#     return {"message": "Pipeline resumed"}

@app.get("/api/project/{thread_id}")
async def get_project(thread_id: str):
    project = projects_collection.find_one({"thread_id": thread_id}, {"_id": 0})
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found in Cosmos DB")
        
    # Inject SAS tokens into the final payload
    project_with_sas = inject_sas_tokens(project)
        
    return project_with_sas







