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






import os
import json
import logging
from azure.servicebus import ServiceBusClient, AutoLockRenewer
from new import app_graph, projects_collection
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SERVICE_BUS_CONN_STR = os.getenv("SERVICE_BUS_CONN_STR")
QUEUE_NAME = "pipeline-jobs"
MAX_LOCK_DURATION = 36000 # 10 hours

def process_job(payload: dict):
    thread_id = payload.get("thread_id")
    action = payload.get("action")
    config = {"configurable": {"thread_id": thread_id}}

    log.info(f"--- GRAPH STARTING: thread_id={thread_id}, action={action} ---")
    
    projects_collection.update_one(
        {"thread_id": thread_id},
        {"$set": {"status": "processing"}}
    )

    try:
        if action == "resume":
            app_graph.invoke(None, config)
        elif action == "start":
            initial_state = {
                "screenplay_text": payload.get("screenplay_text"), 
                "current_step": "init"
            }
            app_graph.invoke(initial_state, config)
            
        log.info(f"--- GRAPH FINISHED: thread_id={thread_id} ---")
    except Exception as e:
        log.error(f"CRITICAL FAILURE in thread {thread_id}: {str(e)}", exc_info=True)
        projects_collection.update_one(
            {"thread_id": thread_id},
            {"$set": {"status": "failed", "error_message": str(e)}}
        )
        raise e

def main():
    log.info("Worker started. Listening for Service Bus messages...")
    if not SERVICE_BUS_CONN_STR:
        log.error("SERVICE_BUS_CONN_STR environment variable is missing!")
        return

    renewer = AutoLockRenewer(max_lock_renewal_duration=MAX_LOCK_DURATION)
    
    with ServiceBusClient.from_connection_string(SERVICE_BUS_CONN_STR) as client:
        with client.get_queue_receiver(queue_name=QUEUE_NAME, prefetch_count=1) as receiver:
            for msg in receiver:
                renewer.register(receiver, msg, max_lock_renewal_duration=MAX_LOCK_DURATION)
                
                try:
                    payload = json.loads(str(msg))
                    process_job(payload)
                    
                    receiver.complete_message(msg)
                    log.info("Message fully processed and removed from queue.")
                    
                except Exception as e:
                    log.error(f"Failed to process message, abandoning: {e}")
                    receiver.abandon_message(msg)

if __name__ == "__main__":
    main()