import json
from pathlib import Path
import requests
import textwrap
import time
from datetime import datetime
import json
import os
from google.cloud import storage
from google.oauth2 import service_account
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# Load .env file for local development
load_dotenv()


# GCP Operations

def get_gcs_client():
    """Creates a GCS client using the environment variable JSON string."""
    creds_json_str = os.getenv("GCP_CREDENTIALS")
    
    if not creds_json_str:
        raise ValueError("GCP_CREDENTIALS environment variable is not set.")

    # Clean the string (removes potential hidden newlines from .env)
    clean_creds_str = creds_json_str.replace('\n', '').replace('\r', '')
    
    try:
        creds_dict = json.loads(clean_creds_str)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        return storage.Client(credentials=credentials, project=creds_dict.get('project_id'))
    except json.JSONDecodeError as e:
        print(f"Failed to parse GCP_CREDENTIALS: {e}")
        raise

# 1. Point to your credentials

def upload_json(bucket_name, destination_blob_name, data):

    client = get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)

    payload = data.model_dump_json(indent=4) if hasattr(data, 'model_dump_json') else json.dumps(data, indent=4)

    blob.upload_from_string(payload, content_type='application/json')
    # print(f"File {destination_blob_name} uploaded to {bucket_name}.")

def download_json(bucket_name, source_blob_name):

    """Downloads a JSON blob from GCS and returns it as a dict."""
    client = get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(source_blob_name)

    raw_text = blob.download_as_text()
    return json.loads(raw_text)

def get_previous_ts(alert_type = 'order_alerts'):
    
    bucket_name = os.getenv('GCP_BUCKET')
    source_blob_name = f"alerts/{alert_type}/log.json"
    log_file = download_json(bucket_name, source_blob_name)
    return datetime.fromisoformat(log_file.get('latest_run_at'))
     
def update_previous_ts(alert_type = 'order_alerts'):
    
    bucket_name = os.getenv('GCP_BUCKET')
    destination_blob_name = f"alerts/{alert_type}/log.json"

    log_file = download_json(bucket_name, destination_blob_name)
    now_in_kolkata = datetime.now(ZoneInfo("Asia/Kolkata"))
    midnight_kolkata = now_in_kolkata.replace(hour=0, minute=0, second=0, microsecond=0)
    log_file['latest_run_at'] = midnight_kolkata.isoformat()

    upload_json(bucket_name, destination_blob_name, log_file)


""" DISCORD as communication chanell """

# 2000 discord limit. leave room for formatting.
MAX_LEN = 1900  

def send_discord(message, webhookurl):
    
    chunks = textwrap.wrap(message, MAX_LEN)
    for i, chunk in enumerate(chunks, 1):
        while True:
            response = requests.post(
                webhookurl,
                json={"content": chunk}
            )
            print(f"Chunk {i}/{len(chunks)} → Status:", response.status_code)

            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 1)
                print(f"Rate limited. Sleeping {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            break

def send_discord2(message, webhookurl):
    
    response = requests.post(
        webhookurl,
        json={"content": message}
    )
    print(f"Status:", response.status_code)