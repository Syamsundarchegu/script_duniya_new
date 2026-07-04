# import os
# import copy
# import uuid
# import json
# import io
# import logging
# from datetime import datetime, timedelta, timezone
# from typing import Optional

# import bcrypt
# import docx
# from jose import JWTError, jwt
# from pymongo import MongoClient
# from pypdf import PdfReader
# from pydantic import BaseModel

# from fastapi import FastAPI, HTTPException, File, UploadFile, Depends
# from fastapi.concurrency import run_in_threadpool
# from fastapi.responses import FileResponse
# from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
# from fastapi.staticfiles import StaticFiles
# from fastapi.middleware.cors import CORSMiddleware

# from azure.servicebus import ServiceBusClient, ServiceBusMessage
# from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

# from app import app_graph, script_db, projects_collection

# from dotenv import load_dotenv

# load_dotenv()


# # ══════════════════════════════════════════════════════════════════════════════
# # LOGGING
# # ══════════════════════════════════════════════════════════════════════════════

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s [%(levelname)s] %(message)s",
# )
# log = logging.getLogger(__name__)


# # ══════════════════════════════════════════════════════════════════════════════
# # FASTAPI APP
# # ══════════════════════════════════════════════════════════════════════════════

# app = FastAPI(title="Script Duniya Pipeline API")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[os.getenv("FRONTEND_URL", "*")],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# os.makedirs("static", exist_ok=True)
# app.mount("/static", StaticFiles(directory="static"), name="static")


# # ══════════════════════════════════════════════════════════════════════════════
# # ENVIRONMENT CONFIG
# # ══════════════════════════════════════════════════════════════════════════════

# BLOB_CONN_STR      = os.getenv("BLOB_CONN_STR")
# BLOB_CONTAINER     = os.getenv("AZURE_STORAGE_CONTAINER", "script-duniya-images")

# SERVICE_BUS_CONN_STR = os.getenv("SERVICE_BUS_CONN_STR")
# SERVICE_BUS_QUEUE    = os.getenv("SERVICE_BUS_QUEUE", "pipeline-jobs")

# SECRET_KEY                  = os.getenv("JWT_SECRET_KEY")
# if not SECRET_KEY:
#     raise ValueError("JWT_SECRET_KEY environment variable is not set")
# ALGORITHM                   = "HS256"
# ACCESS_TOKEN_EXPIRE_MINUTES = 1440  # 24 hours


# # ══════════════════════════════════════════════════════════════════════════════
# # AZURE BLOB STORAGE
# # ══════════════════════════════════════════════════════════════════════════════

# blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONN_STR)

# # Simple in-process SAS cache { clean_url -> {sas_url, expires} }
# _sas_cache: dict = {}


# def append_sas_to_url(url: str) -> str:
#     """Generates a 2-hour SAS token and appends it to a private blob URL.
#     Results are cached for ~115 minutes to avoid regenerating on every poll.
#     """
#     if not url or BLOB_CONTAINER not in url:
#         return url

#     clean_url = url.split("?")[0]

#     import time
#     cached = _sas_cache.get(clean_url)
#     if cached and cached["expires"] > time.time() + 300:  # 5-min buffer
#         return cached["sas_url"]

#     try:
#         blob_name = clean_url.split(f"/{BLOB_CONTAINER}/")[1]
#         sas_token = generate_blob_sas(
#             account_name=blob_service_client.account_name,
#             container_name=BLOB_CONTAINER,
#             blob_name=blob_name,
#             account_key=blob_service_client.credential.account_key,
#             permission=BlobSasPermissions(read=True),
#             expiry=datetime.now(timezone.utc) + timedelta(hours=2),
#         )
#         sas_url = f"{clean_url}?{sas_token}"
#         _sas_cache[clean_url] = {"sas_url": sas_url, "expires": time.time() + 6900}
#         return sas_url
#     except Exception as e:
#         log.error(f"SAS generation error: {e}")
#         return clean_url


# def inject_sas_tokens(data: dict) -> dict:
#     """Recursively walks the state dict and appends SAS tokens to all image URLs."""
#     # Reference images (character refs)
#     refs = data.get("reference_images", {})
#     if isinstance(refs, dict):
#         for char, url in refs.items():
#             if url:
#                 refs[char] = append_sas_to_url(url)

#     # Breakdown scenes → frames
#     breakdown = data.get("breakdown") or data.get("final_breakdown") or {}
#     for scene in breakdown.get("scenes", []):
#         if scene.get("scene_reference_url"):
#             scene["scene_reference_url"] = append_sas_to_url(scene["scene_reference_url"])
#         for frame in scene.get("frames", []):
#             if frame.get("image_url"):
#                 frame["image_url"] = append_sas_to_url(frame["image_url"])

#     # final_output scenes
#     final_output = data.get("final_output", {})
#     if final_output:
#         for scene in final_output.get("scenes", []):
#             if scene.get("scene_reference_url"):
#                 scene["scene_reference_url"] = append_sas_to_url(scene["scene_reference_url"])
#             for frame in scene.get("frames", []):
#                 if frame.get("image_url"):
#                     frame["image_url"] = append_sas_to_url(frame["image_url"])
#         ref2 = final_output.get("reference_images", {})
#         if isinstance(ref2, dict):
#             for char, url in ref2.items():
#                 if url:
#                     ref2[char] = append_sas_to_url(url)

#     return data


# # ══════════════════════════════════════════════════════════════════════════════
# # SERVICE BUS
# # ══════════════════════════════════════════════════════════════════════════════

# def push_job_to_queue(thread_id: str, screenplay_text: str) -> None:
#     """Push a pipeline job payload to the Azure Service Bus queue."""
#     if not SERVICE_BUS_CONN_STR:
#         raise RuntimeError("SERVICE_BUS_CONN_STR is not configured.")

#     payload = json.dumps({
#         "thread_id": thread_id,
#         "screenplay_text": screenplay_text,
#     })
#     with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as client:
#         with client.get_queue_sender(SERVICE_BUS_QUEUE) as sender:
#             sender.send_messages(ServiceBusMessage(payload))
#     log.info(f"[{thread_id}] Job pushed to Service Bus queue '{SERVICE_BUS_QUEUE}'.")


# # ══════════════════════════════════════════════════════════════════════════════
# # AUTHENTICATION
# # ══════════════════════════════════════════════════════════════════════════════

# oauth2_scheme   = OAuth2PasswordBearer(tokenUrl="/api/login")
# users_collection = script_db["users"]


# def verify_password(plain_password: str, hashed_password: str) -> bool:
#     return bcrypt.checkpw(
#         plain_password.encode("utf-8"),
#         hashed_password.encode("utf-8"),
#     )


# def get_password_hash(password: str) -> str:
#     salt = bcrypt.gensalt()
#     return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


# def create_access_token(data: dict) -> str:
#     to_encode = data.copy()
#     expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
#     to_encode.update({"exp": expire})
#     return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
#     credentials_exception = HTTPException(
#         status_code=401,
#         detail="Could not validate credentials",
#         headers={"WWW-Authenticate": "Bearer"},
#     )
#     try:
#         payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
#         username: str = payload.get("sub")
#         if username is None:
#             raise credentials_exception
#     except JWTError:
#         raise credentials_exception

#     user = users_collection.find_one({"username": username})
#     if user is None:
#         raise credentials_exception
#     return user


# # ══════════════════════════════════════════════════════════════════════════════
# # FILE PARSING HELPERS
# # ══════════════════════════════════════════════════════════════════════════════

# def parse_document(content: bytes, filename: str) -> str:
#     """Synchronous CPU-bound document parsing (runs in threadpool)."""
#     text = ""
#     try:
#         if filename.endswith(".pdf"):
#             reader = PdfReader(io.BytesIO(content))
#             for page in reader.pages:
#                 extracted = page.extract_text()
#                 if extracted:
#                     text += extracted + "\n"
#         elif filename.endswith(".docx"):
#             doc = docx.Document(io.BytesIO(content))
#             for para in doc.paragraphs:
#                 text += para.text + "\n"
#         else:
#             raise ValueError("Unsupported file type")
#     except Exception as e:
#         raise Exception(f"Error parsing document: {str(e)}")
#     return text


# async def extract_text_from_file(file: UploadFile) -> str:
#     filename = file.filename.lower()

#     if not filename.endswith((".pdf", ".docx")):
#         raise HTTPException(
#             status_code=400,
#             detail="Unsupported file type. Please upload a .pdf or .docx document.",
#         )

#     content = await file.read()

#     MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
#     if len(content) > MAX_FILE_SIZE:
#         raise HTTPException(status_code=413, detail="File too large. Maximum size is 10MB.")

#     try:
#         text = await run_in_threadpool(parse_document, content, filename)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))

#     if not text.strip():
#         raise HTTPException(status_code=400, detail="Could not extract any text from the document.")

#     return text


# # ══════════════════════════════════════════════════════════════════════════════
# # PYDANTIC MODELS
# # ══════════════════════════════════════════════════════════════════════════════

# class UserCreate(BaseModel):
#     username: str
#     password: str


# # ══════════════════════════════════════════════════════════════════════════════
# # ROUTES — FRONTEND
# # ══════════════════════════════════════════════════════════════════════════════

# @app.get("/")
# async def serve_frontend():
#     return FileResponse("static/templates.html")


# # ══════════════════════════════════════════════════════════════════════════════
# # ROUTES — HEALTH
# # ══════════════════════════════════════════════════════════════════════════════

# @app.get("/api/health")
# async def health_check():
#     """Azure App Service health probe endpoint."""
#     checks = {"status": "ok", "mongo": False, "blob": False, "service_bus": False}

#     # Check MongoDB / Cosmos DB
#     try:
#         from app import mongo_client as _mc
#         if _mc:
#             _mc.admin.command("ping")
#             checks["mongo"] = True
#     except Exception:
#         pass

#     # Check Blob Storage
#     try:
#         blob_service_client.get_account_information()
#         checks["blob"] = True
#     except Exception:
#         pass

#     # Check Service Bus
#     try:
#         if SERVICE_BUS_CONN_STR:
#             with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as _sb:
#                 checks["service_bus"] = True
#     except Exception:
#         pass

#     return checks


# # ══════════════════════════════════════════════════════════════════════════════
# # ROUTES — AUTH
# # ══════════════════════════════════════════════════════════════════════════════

# @app.post("/api/register")
# async def register(user: UserCreate):
#     if users_collection.find_one({"username": user.username}):
#         raise HTTPException(status_code=400, detail="Username already registered")

#     hashed_password = get_password_hash(user.password)
#     users_collection.insert_one({"username": user.username, "password": hashed_password})
#     return {"message": "User created successfully"}


# @app.post("/api/login")
# async def login(form_data: OAuth2PasswordRequestForm = Depends()):
#     user = users_collection.find_one({"username": form_data.username})
#     if not user or not verify_password(form_data.password, user["password"]):
#         raise HTTPException(status_code=400, detail="Incorrect username or password")

#     access_token = create_access_token(data={"sub": user["username"]})
#     return {"access_token": access_token, "token_type": "bearer"}


# # ══════════════════════════════════════════════════════════════════════════════
# # ROUTES — PIPELINE
# # ══════════════════════════════════════════════════════════════════════════════

# @app.post("/api/start")
# async def start_pipeline(
#     file: UploadFile = File(...),
#     current_user: dict = Depends(get_current_user),
# ):
#     """
#     Accepts a PDF or DOCX screenplay, saves a job record to Cosmos DB,
#     and pushes the job to Azure Service Bus for the Container App worker to process.
#     Returns immediately with a thread_id for polling.
#     """
#     screenplay_text = await extract_text_from_file(file)
#     thread_id = str(uuid.uuid4())

#     # Save initial job record
#     projects_collection.insert_one({
#         "thread_id": thread_id,
#         "username": current_user["username"],
#         "status": "queued",
#         "original_screenplay": screenplay_text,
#         "created_at": datetime.now(timezone.utc).isoformat(),
#     })
#     log.info(f"[{thread_id}] Job record created for user '{current_user['username']}'.")

#     # Push to Service Bus — worker picks this up and runs the full pipeline
#     try:
#         await run_in_threadpool(push_job_to_queue, thread_id, screenplay_text)
#     except Exception as e:
#         # Roll back the DB record so the user can retry
#         projects_collection.delete_one({"thread_id": thread_id})
#         log.error(f"[{thread_id}] Failed to push to Service Bus: {e}")
#         raise HTTPException(status_code=500, detail="Failed to queue the pipeline job. Please try again.")

#     return {"thread_id": thread_id, "message": "Job queued successfully. Use thread_id to poll status."}


# @app.get("/api/state/{thread_id}")
# async def get_state(
#     thread_id: str,
#     current_user: dict = Depends(get_current_user),
# ):
#     """
#     Returns the current LangGraph checkpoint state for a given thread_id.
#     Injects fresh SAS tokens into all image URLs before returning.
#     """
#     # Ownership check — users can only see their own jobs
#     project = projects_collection.find_one({"thread_id": thread_id})
#     if project and project.get("username") != current_user["username"]:
#         raise HTTPException(status_code=403, detail="Access denied.")

#     config = {"configurable": {"thread_id": thread_id}}
#     state  = app_graph.get_state(config)

#     if not state or not state.values:
#         # Job may be queued but not yet started by the worker
#         db_status = project.get("status", "unknown") if project else "not_found"
#         return {
#             "values":      {"current_step": db_status},
#             "next_nodes":  [],
#             "is_finished": False,
#         }

#     # Deep copy so we never mutate LangGraph's internal state
#     safe_values = copy.deepcopy(state.values)

#     # Inject SAS tokens into all image URLs
#     values_with_sas = inject_sas_tokens(safe_values)

#     # Normalise: if final_output has no scenes yet, pull them from breakdown
#     final_output = values_with_sas.get("final_output") or {}
#     breakdown    = values_with_sas.get("breakdown") or {}

#     if not final_output.get("scenes") and breakdown.get("scenes"):
#         final_output = {
#             "screenplay_title": breakdown.get("title") or values_with_sas.get("screenplay_title"),
#             "title":            breakdown.get("title") or values_with_sas.get("screenplay_title"),
#             "visual_style":     breakdown.get("visual_style"),
#             "color_palette":    breakdown.get("color_palette"),
#             "aspect_ratio":     breakdown.get("aspect_ratio"),
#             "total_scenes":     breakdown.get("total_scenes"),
#             "scenes":           breakdown.get("scenes", []),
#             "reference_images": values_with_sas.get("reference_images", {}),
#         }
#         values_with_sas["final_output"] = final_output

#     return {
#         "values":      values_with_sas,
#         "next_nodes":  state.next,
#         "is_finished": len(state.next) == 0,
#     }


# @app.get("/api/project/{thread_id}")
# async def get_project(
#     thread_id: str,
#     current_user: dict = Depends(get_current_user),
# ):
#     """
#     Returns the raw Cosmos DB project record (status, metadata) for a thread.
#     Useful for checking 'queued' / 'processing' / 'completed' / 'failed' status
#     before the LangGraph checkpoint is created by the worker.
#     """
#     project = projects_collection.find_one({"thread_id": thread_id}, {"_id": 0})

#     if not project:
#         raise HTTPException(status_code=404, detail="Project not found.")

#     if project.get("username") != current_user["username"]:
#         raise HTTPException(status_code=403, detail="Access denied.")

#     return inject_sas_tokens(project)


# @app.get("/api/projects")
# async def list_projects(current_user: dict = Depends(get_current_user)):
#     """
#     Returns all projects belonging to the current user.
#     Useful for showing a dashboard / history page.
#     """
#     cursor = projects_collection.find(
#         {"username": current_user["username"]},
#         {"_id": 0, "original_screenplay": 0},  # exclude large fields
#     ).sort("created_at", -1).limit(50)

#     return {"projects": list(cursor)}


from PIL import Image, ImageFilter, ImageEnhance
import io
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


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


# ==================================================
# TEST CODE
# ==================================================

IMAGE_PATH = "image.png"   # Change this

# Read image bytes
with open(IMAGE_PATH, "rb") as f:
    raw_bytes = f.read()

# Process image
processed_bytes = _standardize_reference_image(raw_bytes)

# Verify output
processed_img = Image.open(io.BytesIO(processed_bytes))

print("\nProcessed Image")
print("----------------")
print("Format :", processed_img.format)
print("Size   :", processed_img.size)
print("Mode   :", processed_img.mode)

# Save output
OUTPUT_PATH = "processed.png"

with open(OUTPUT_PATH, "wb") as f:
    f.write(processed_bytes)

print(f"\nSaved standardized image as: {OUTPUT_PATH}")