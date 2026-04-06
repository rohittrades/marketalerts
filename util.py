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


def get_prompt(file_path):
    """
    Reads a prompt from a text file and returns it as a string.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            # .strip() removes leading/trailing whitespace/newlines
            prompt = file.read().strip()
            return prompt
    except FileNotFoundError:
        return "Error: The file was not found."
    except Exception as e:
        return f"An unexpected error occurred: {e}"

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

def send_grouped_discord(industry, data_list, webhookurl):
    """
    Sends all dictionaries in data_list to Discord, 
    automatically splitting them into multiple messages if they exceed 10.
    """
    chunks = [data_list[i:i + 10] for i in range(0, len(data_list), 10)]
    responses = []

    for index, chunk in enumerate(chunks):
        embeds = []
        
        for item in chunk:
            keys = list(item.keys())
            _, *main_keys, url_key = keys
            url_value = item[url_key]

            description_lines = ["```yaml"] 
            for k in main_keys:
                description_lines.append(f"{k:<15}: {item[k]}")
            description_lines.append("```") # End code block
            description_lines.append(f"🔗 [View attachment]({url_value})")

            embed = {
                "title": f"{item.get(keys[0], 'Details')}",
                "description": "\n".join(description_lines),
                "color": 5814783 
            }
            embeds.append(embed)

        # Only add the Industry header to the very first message
        content = f"### {industry}" if index == 0 else ""
        payload = {
            "content": content,
            "embeds": embeds
        }
        res = requests.post(webhookurl, json=payload)
        responses.append(res.status_code)
        if len(chunks) > 1:
            time.sleep(0.5)
    return responses

def clean_order_line(order_line):

    # order value formatting
    order_value = order_line.get('order_value_for_current_company')
    if order_value is not None:
        raw_number = order_value.get('raw_number')
        scale = order_value.get('scale').value if order_value.get('scale').value != 'none' else ''
        currency = order_value.get('currency', '')
        is_per_year = order_value.get('is_per_year', False)

        value_str = f"{currency} {raw_number} {scale} ".strip()
        if is_per_year:
            value_str += " per year"
    else:
        value_str = "not specified"
    
    award_status = order_line.get('award_status')
    if award_status and getattr(award_status, 'value', award_status) == 'first_lower':
        value_str += ' (First Lower)'

    # duration formatting
    duration = order_line.get('project_duration')
    if duration is not None:
        duration_value = duration.get('value')
        duration_unit = duration.get('unit').value if duration.get('unit') else None
        if duration_value is not None and duration_unit is not None:
            duration_str = f"{duration_value} {duration_unit}"
        else:
            duration_str = "not specified"
    else:
        duration_str = "not specified"

    display_line = {
        'company': order_line.get('company'),
        'value': value_str,
        'awarder': order_line.get('awarding_entity'),
    }

    partnership = order_line.get('partnership_details') or {}
    if partnership.get('type') and partnership['type'].value != 'solo':
        display_line['partnership'] = ", ".join(partnership.get('partners') or ['not specified'])
    
    display_line['duration'] = duration_str
    display_line['target industry'] = order_line.get('sub_industry_name')
    display_line['description'] = (order_line.get('work_description') or '')[:400]
    display_line['attachment'] = order_line.get('attachment')

    return display_line


# BSE Announcements related utilities
from bse import BSE
from requests.exceptions import ReadTimeout
import random

def fetch_announcements(scripCode, from_dt, to_dt, max_anns=1000):
    anns: list[dict] = []
    page_count = 1
    bse = BSE(download_folder='data/orders_data/tmp')

    while True:
        for attempt in range(3):
            try:
                res = bse.announcements(
                    page_no=page_count,
                    scripcode=scripCode,
                    from_date=from_dt,
                    to_date=to_dt
                )
                break
            except (TimeoutError, ReadTimeout):
                if attempt == 2:
                    print(
                        f"⚠️ BSE timeout on page {page_count} "
                        f"for {scripCode}, stopping fetch anns"
                    )
                    return anns
                wait_time = (2 ** attempt) * 2 + random.uniform(0, 1)
                time.sleep(wait_time)

        if page_count == 1:
            max_anns = res['Table1'][0]['ROWCNT']

        page_count += 1
        anns.extend(res['Table'])

        if len(anns) >= max_anns:
            break
        time.sleep(1)  # polite rate limit (important for BSE)
    return anns

# TARGET Industry Classifier
import pandas as pd
import numpy as np
from typing import List, Optional, Literal
import faiss
from google import genai
from sentence_transformers import SentenceTransformer
from pydantic import ValidationError, create_model, BaseModel, Field
from models import GICSResponse
from dotenv import load_dotenv
load_dotenv()

api_key = os.getenv("GEMINI_PAID_KEY1")
use_model = os.getenv("LLM_MODEL_NAME")

class GICSAutomator:
    def __init__(self, csv_path='data/gics_map_2023.csv', index_path='data/gics_index.faiss'):
        self.csv_path = csv_path
        self.index_path = index_path
        self.model_name = 'all-MiniLM-L6-v2'
        
        # 1. Lazy-load the embedder only when needed
        self._embedder = None 
        
        # 2. Load Taxonomy Metadata
        self.df = pd.read_csv(csv_path)
        
        # 3. Load or Build Index
        self.index = self._load_or_build_index()
        
        # 4. Initialize Gemini
        self.client = genai.Client(api_key=api_key)

    @property
    def embedder(self):
        """Getter that loads the model into memory only on first use."""
        if self._embedder is None:
            print(f"Loading {self.model_name} into memory...")
            self._embedder = SentenceTransformer(self.model_name)
        return self._embedder

    def _load_or_build_index(self):
        """Checks if a pre-computed FAISS index exists; otherwise builds it."""
        if os.path.exists(self.index_path):
            print(f"Loading existing index from {self.index_path}")
            return faiss.read_index(self.index_path)
        
        print("Index not found. Building new vector store...")
        # Create the rich search string
        search_texts = (
            self.df['Sector'] + " > " + 
            self.df['IndustryGroup'] + " > " + 
            self.df['Industry'] + " > " + 
            self.df['SubIndustry'] + ": " + 
            self.df['SubIndustryDescription']
        ).tolist()
        
        embeddings = self.embedder.encode(search_texts, convert_to_numpy=True).astype('float32')
        faiss.normalize_L2(embeddings)
        dimension = embeddings.shape[1]
        index = faiss.IndexFlatIP(dimension)
        index.add(embeddings)
        
        # Save for next time
        faiss.write_index(index, self.index_path)
        return index

    def llm(self, prompt, dynamic_schema):
        try:
            # We pass the Pydantic model directly to Gemini's response_schema
            response = self.client.models.generate_content(
                model="gemini-2.5-flash", # Updated to current stable/fast model
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": dynamic_schema, 
                }
            )
            # Validate the response string against our Pydantic model
            return dynamic_schema.model_validate_json(response.text)
        except ValidationError as e:
            return {"error": "Validation failed", "details": e.errors()}
        except Exception as e:
            return {"error": str(e)}

    def categorize_project(self, company_name, project_description):
        query = f"Work description : {project_description}"
        query_vec = self.embedder.encode([query]).astype('float32')
        faiss.normalize_L2(query_vec)

        scores, indices = self.index.search(query_vec, k=10)
        candidates = self.df.iloc[indices[0]].copy()
        candidates['similarity'] = scores[0]

        # Extract unique Sub-Industry names from your search results
        valid_names = candidates['SubIndustry'].unique().tolist()
        valid_names.append("Other / Unclassified") # The escape hatch        

        # Create a Dynamic Pydantic Model on the fly
        # This forces the LLM to choose ONLY 1/N names found in semantic search
        DynamicResponse = create_model(
            'DynamicGICSResponse',
            sub_industry_name=(Literal[tuple(valid_names)], ...),
            confidence_score=(float, ...),
            reasoning=(str, ...), # Let Gemini explain its choice
            __base__=GICSResponse
        )
        # Format context for prompt
        candidate_context = ""
        for _, row in candidates.iterrows():
            candidate_context += (
                f"- {row['SubIndustry']} (Similarity: {row['similarity']:.2f}): "
                f"{row['SubIndustryDescription']}\n"
            )

        prompt = get_prompt('prompts/industryclassifier.txt').format(
            company_name=company_name,
            project_description=project_description,
            candidate_context=candidate_context
        )
        return self.llm(prompt, DynamicResponse)