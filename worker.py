# import os
# import json
# import time
# import sys
# from azure.servicebus import ServiceBusClient
# # Importing your existing graph and database collections from app.py
# from app import app_graph, projects_collection 

# SERVICE_BUS_CONN_STR = os.getenv("SERVICE_BUS_CONN_STR")
# QUEUE_NAME = "pipeline-jobs"

# def process_job(message_body: dict):
#     """
#     This function handles the actual 5-6 hour LangGraph execution.
#     It runs COMPLETELY OFFLINE from the Azure Service Bus connection.
#     """
#     thread_id = message_body["thread_id"]
#     screenplay_text = message_body["screenplay_text"]
    
#     # 1. Update status to processing in Cosmos DB
#     try:
#         projects_collection.update_one(
#             {"thread_id": thread_id},
#             {"$set": {"status": "processing"}}
#         )
#         print(f"[{thread_id}] Status updated to 'processing' in Cosmos DB.")
#     except Exception as db_err:
#         print(f"[{thread_id}] Failed to update DB to processing: {db_err}")
    
#     # 2. Setup LangGraph state and config
#     initial_state = {"screenplay_text": screenplay_text, "current_step": "init"}
#     config = {"configurable": {"thread_id": thread_id}}
    
#     # 3. Execute the heavy pipeline
#     try:
#         print(f"[{thread_id}] Starting 6-hour LangGraph pipeline...")
#         # Note: Your app_graph's persist_output_node automatically sets status to 'completed' at the end
#         app_graph.invoke(initial_state, config)
#         print(f"[{thread_id}] Pipeline completed successfully.")
#     except Exception as e:
#         print(f"[{thread_id}] Pipeline failed: {e}")
#         # Mark as failed in DB so the user knows via the frontend UI
#         try:
#             projects_collection.update_one(
#                 {"thread_id": thread_id},
#                 {"$set": {"status": "failed", "error": str(e)}}
#             )
#         except Exception as db_err:
#             print(f"[{thread_id}] Failed to log error to DB: {db_err}")

# def main():
#     print("Worker container started. Connecting to Azure Service Bus...")
#     job_to_run = None
    
#     # =====================================================================
#     # STEP 1: THE QUICK FETCH
#     # Connect, grab exactly ONE message, tell Azure to delete it, and disconnect.
#     # =====================================================================
#     try:
#         with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as client:
#             with client.get_queue_receiver(queue_name=QUEUE_NAME, max_wait_time=5) as receiver:
#                 for msg in receiver:
#                     job_to_run = json.loads(str(msg))
                    
#                     # IMPORTANT: Tell Service Bus we have the message BEFORE starting the heavy work
#                     receiver.complete_message(msg)
#                     print(f"Successfully pulled job {job_to_run['thread_id']} off the queue.")
                    
#                     # Break the loop immediately so we only process one job and sever the connection
#                     break 
#     except Exception as sb_err:
#         print(f"Error communicating with Service Bus: {sb_err}")
#         # Exit the script with an error code so Azure knows it failed early
#         sys.exit(1)

#     # =====================================================================
#     # STEP 2: THE HEAVY LIFT
#     # Run the 6-hour process completely independent of the Service Bus connection.
#     # =====================================================================
#     if job_to_run:
#         print("Disconnected from Service Bus. Beginning offline processing phase.")
#         process_job(job_to_run)
#     else:
#         print("No messages found in the queue. Worker shutting down.")

#     # =====================================================================
#     # STEP 3: CLEAN EXIT
#     # Tell Azure the job finished perfectly so it marks it as "Succeeded"
#     # =====================================================================
#     print("Worker execution complete. Exiting gracefully.")
#     sys.exit(0)

# if __name__ == "__main__":
#     main()









# worker.py
import json
import logging
from azure.servicebus import ServiceBusClient
from new import app_graph
import os
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO)

SERVICE_BUS_CONN_STR = os.getenv("SERVICE_BUS_CONN_STR")
QUEUE_NAME = "pipeline-jobs"

def process_message(msg_body: dict):
    thread_id = msg_body["thread_id"]
    action = msg_body.get("action", "start")  # "start" (fresh) or "resume" (after character uploads)
    config = {"configurable": {"thread_id": thread_id}}

    if action == "resume":
        # Character upload phase already ran via /api/extract-characters and
        # /api/upload-character-reference. The graph state already has
        # user_uploaded_images populated — just continue from the interrupt point.
        log.info(f"Resuming pipeline for thread_id={thread_id}")
        app_graph.invoke(None, config)
        log.info(f"Resume completed for thread_id={thread_id}")
    else:
        # Fresh run — not used by the current frontend (which always goes
        # through extract-characters -> resume-pipeline), but kept for
        # any direct /api/start callers.
        screenplay_text = msg_body["screenplay_text"]
        initial_state = {"screenplay_text": screenplay_text, "current_step": "init"}
        log.info(f"Starting fresh pipeline for thread_id={thread_id}")
        app_graph.invoke(initial_state, config)
        log.info(f"Start completed for thread_id={thread_id}")

def run_worker():
    with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as client:
        with client.get_queue_receiver(queue_name=QUEUE_NAME, max_wait_time=30) as receiver:
            log.info("Worker started, listening for messages...")
            for msg in receiver:
                try:
                    body = json.loads(str(msg))
                    process_message(body)
                    receiver.complete_message(msg)
                except Exception as e:
                    log.error(f"Message processing failed: {e}")
                    receiver.abandon_message(msg)

if __name__ == "__main__":
    run_worker()