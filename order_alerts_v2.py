import shutil

from PIL.ImagePath import Path
from google import genai
from google.api_core import exceptions
from models import AnnouncementType, AnnouncementModel
from util import GICSAutomator, clean_order_line, get_previous_ts, fetch_announcements, get_prompt, send_discord2, update_previous_ts, upload_json, send_grouped_discord
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
import pytesseract
import pandas as pd
import os
import json
import time
import random
import requests
from dotenv import load_dotenv
load_dotenv()

# ---- CONFIG ----

test_mode = True
local_run = False
alert_name = 'order_alerts_v2'

# ---- Load env variables ---

if test_mode:
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_TMP')
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_MISC') #for individual stock OB
else:
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_ORDER2_WEBHOOK')

misc_str = os.getenv("ORDER_MISC_DATA")
misc_data = json.loads(misc_str)

BASE_URLS = misc_data.get('base_urls')
HEADERS = misc_data.get('headers')

KEYWORDS = misc_data.get('keywords')
OB_STOCKS = misc_data.get('ob_stocks')
if test_mode:
    OB_STOCKS = [544223] #OB_STOCKS[:10]

allowed_cat = misc_data.get('allowed_cat')
forbidden_subcat = misc_data.get('forbidden_subcat')

allowed_cat_norm = {c.strip().lower() for c in allowed_cat}
forbidden_subcat_norm = {s.strip().lower() for s in forbidden_subcat}

""" Gemini configuration """

MAX_COUNT = 248 #Free gemini limit is 250

classifier_model = os.getenv('OCLASSIFIER_MODEL')
extractor_model = os.getenv('OEXTRACTOR_MODEL')
llm_key = os.getenv("GEMINI_PAID_KEY1")

""" GCP configuration """

bucket_name = os.getenv("GCP_BUCKET")
oa_v2_folder = f"alerts/{alert_name}"
local_folder = "data/orders_data"

if test_mode:
    oa_v2_folder = f"segregated_alerts/{alert_name}"

# ---- Load prompts ---

CLASSIFIER_PROMPT = get_prompt('prompts/oclassifier.txt')
EXTRACTION_PROMPT = get_prompt('prompts/oextractor.txt')

# ---- Base paths ---

pdf_base = 'data/pdf/'
industry_taxonomy_path = 'data/gics_map_2023.csv'
indexed_gics_path = 'data/gics_index.faiss'



# ---- Methods ----

# ---- Attachment handling ----
def is_valid_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except:
        return False
    
def download_pdf_fast(fname, out_dir, session):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, fname)

    for base in BASE_URLS:
        url = base + fname
        try:
            r = session.get(url, timeout=15, stream=True)
            if r.status_code != 200:
                continue

            with open(out_path, "wb") as f:
                for chunk in r.iter_content(16384):
                    if chunk:
                        f.write(chunk)

            if is_valid_pdf(out_path):
                return True, out_path, url
            else:
                os.remove(out_path)

        except requests.exceptions.RequestException:
            pass
    print('Download failed ⛔️ fname : ', fname)
    return False, None, None

# pdf handling
def extract_text_fast(pdf_path, pg_limit=4):
    try:
        reader = PdfReader(pdf_path, strict=False)
        text = ""
        for i, page in enumerate(reader.pages):
            if i >= pg_limit:
                break
            text += page.extract_text() or ""
        return text.strip()
    except Exception:
        return ""

def extract_text_ocr(pdf_path, pg_limit=4):
    try:
        images = convert_from_path(
            pdf_path,
            dpi=200,                 # lower DPI = faster
            first_page=1,
            last_page=pg_limit
        )
    except Exception:
        return ""

    text = ""
    for img in images:
        text += pytesseract.image_to_string(
            img,
            config="--psm 6"        # faster, good for documents
        )
        text += "\n--- PAGE BREAK ---\n"

    return text.strip()

def text_from_pdf(pdf_path, pg_limit=4):
    # Fast path
    text = extract_text_fast(pdf_path, pg_limit)

    if len(text) > 100:      # heuristic: enough content
        return text

    # OCR fallback
    return extract_text_ocr(pdf_path, pg_limit)

def contains_keywords(text, keywords):
    text = text.lower()
    return any(keyword.lower() in text for keyword in keywords)

def extract_first_two_pages(input_path, output_path=None):
    """
    Reads a PDF from input_path and writes a new PDF containing 
    only the first 2 pages.
    """
    if not output_path:
        # Default to appending '_truncated' to the filename
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_truncated{ext}"

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()

        # Determine how many pages to grab (max 2)
        num_pages = min(2, len(reader.pages))
        
        for i in range(num_pages):
            writer.add_page(reader.pages[i])

        with open(output_path, "wb") as out_file:
            writer.write(out_file)
            
        return True, output_path

    except Exception as e:
        print(f"Error processing PDF: {e}")
        return False, None

def wait_for_files_active(client, files):
    """
    Waits for all uploaded files to reach the 'ACTIVE' state.
    """
    print("⏳ Waiting for files to process...")
    for f in files:
        while True:
            # Refresh file metadata
            current_file = client.files.get(name=f.name)
            
            if current_file.state.name == "ACTIVE":
                break
            elif current_file.state.name == "FAILED":
                raise Exception(f"File {f.display_name} failed to process.")
            
            # Progress indicator
            print(f"  - {f.display_name} is {current_file.state.name}...")
            time.sleep(2)
    print("✅ All files ready!")

def llm(client, model_name, contents, config):
    """Wrapper to handle 429 Resource Exhausted errors."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config
            )
        except exceptions.ResourceExhausted as e:
            # Exponential backoff: 2, 4, 8, 16... seconds + jitter
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f"⚠️ Rate limit hit. Waiting {wait:.2f}s...")
            time.sleep(wait)
    raise Exception("Max retries exceeded.")


def main_task():

    prev_run_dt = get_previous_ts(alert_name)     # fetch last run time stamp

    from_dt = prev_run_dt
    to_dt   = datetime.now(ZoneInfo("Asia/Kolkata"))

    if test_mode:
        from_dt = datetime(2025, 4, 1, 0, 0)
        to_dt = datetime(2026, 3, 27, 0, 0) #datetime.now()
    welcome_message = f"Morning! reading announcements from BSE.. \nRunning Order Alerts v2 from {str(from_dt.date())} - {str(to_dt.date())}"
    send_discord2(welcome_message, DISCORD_WEBHOOK_URL)

    # load stocks list that announces order book
    stocks_df = pd.read_csv('data/stocks_list.csv', dtype={'bse_code': 'Int64'})
    ob_stocks = stocks_df[stocks_df['bse_code'].isin(OB_STOCKS)]

    # preparation
    client = genai.Client(api_key=llm_key)
    session = requests.Session()
    session.headers.update(HEADERS)

    automator = GICSAutomator(industry_taxonomy_path) 
    order_flag = False

    for industry in ob_stocks['Industry'].unique():
        print(f"** Processing industry: {industry} **")
        industry_stocks = ob_stocks[ob_stocks['Industry'] == industry]

        industry_order_lines = []
        for idx, row in industry_stocks.iterrows():
            anns = fetch_announcements(row['bse_code'], from_dt, to_dt)
            if not anns:
                # print(f"No announcements found for company in the given date range.")
                continue
            print(f"*stock: {row['company_name']} - total anns: {len(anns)}*")
            for idx, ann in enumerate(anns):
                if not ann.get('ATTACHMENTNAME'):
                    #print("no attachment -> obvio not an order win ann")
                    continue                
                category = str(ann.get('CATEGORYNAME', '')).strip().lower()
                subcat   = str(ann.get('SUBCATNAME', '')).strip().lower()
                if category not in allowed_cat_norm or subcat in forbidden_subcat_norm:
                    #print("Skipping! Category or Subcategory not relevant.")
                    continue
                
                #download attachment
                downloaded, pdf_full_path, doc_url = download_pdf_fast(
                    ann['ATTACHMENTNAME'],
                    pdf_base + str(row['bse_code']),
                    session
                )
                success2pager, anns_2pager_path = extract_first_two_pages(pdf_full_path, output_path=None)
                if not (downloaded and success2pager):
                    print("Attachment download or processing failed, skipping announcement.")
                    print(doc_url)
                    print(f"pdf download status : {downloaded}, 2 pager success: {success2pager}")
                    continue
                
                # keyword matching TODO optimize list
                attachment_text = text_from_pdf(pdf_full_path, pg_limit=3)
                combined_text = '  '.join([attachment_text, ann.get('NEWSSUB', ''), ann.get('HEADLINE', ''), ann.get('MORE', '')])
                if not contains_keywords(combined_text, KEYWORDS):
                    continue
                                                                                                                                                                                         
                #upload required documents to gcp and get public urls for llm access
                file1_reference = client.files.upload(file=anns_2pager_path, config={'display_name': 'Company Announcement 2-Pager'})
                file2_reference = client.files.upload(file=pdf_full_path, config={'display_name': 'Company Announcement'})
                wait_for_files_active(client, [file1_reference, file2_reference])

                #clear local pdfs to save space
                if os.path.exists(pdf_base + str(row['bse_code'])):
                    shutil.rmtree(pdf_base + str(row['bse_code']))
                # llm powered processing 
                # llm1 - classifier
                sys_classifier_prompt = CLASSIFIER_PROMPT.format(target_company=row['company_name'])
                classifier_response = llm(
                    client, 
                    classifier_model, 
                    [file1_reference, sys_classifier_prompt],
                    {
                        "response_mime_type": "application/json",
                        "response_schema": AnnouncementType,
                    }
                )

                #llm2 - extractor (only if classified as order book ann)
                sys_extraction_prompt = EXTRACTION_PROMPT.format(target_company=row['company_name'])
                if classifier_response.parsed.category.lower() == 'order':
                    extractor_response = llm(
                        client, 
                        extractor_model, 
                        [file2_reference, sys_extraction_prompt],
                        {
                            "response_mime_type": "application/json",
                            "response_schema": AnnouncementModel,
                        }
                    )
                    extracted_info = extractor_response.parsed
                    print(extracted_info)
                else:
                    continue #non-order > skip
                
                #Industry classification of won-order (semantic search + llm) 
                target_industry_resp = automator.categorize_project(extracted_info.awarding_entity, extracted_info.work_description)
                target_industry_dict = target_industry_resp.model_dump() # Convert to dict here!
                order_date = datetime.fromisoformat(ann['NEWS_DT']).date().isoformat()
                misc_info = {
                    'company': row['company_name'],
                    'bse_code': row['bse_code'],
                    'industry': industry,
                    'order_date': order_date,
                    'attachment': doc_url
                    }
                order_line = {**misc_info, **extracted_info.model_dump(), **target_industry_dict}

                #save response as json locally and/or upload to gcp
                filename = f"{row['bse_code']}_{ann['ATTACHMENTNAME']}.json"
                if not local_run:
                    #upload line level json with unique_name
                    raw_json_path = f"{oa_v2_folder}/raw_jsons/{order_date}/{filename}"
                    if test_mode:
                        raw_json_path = f"{oa_v2_folder}/raw_jsons/{row['bse_code']}/{filename}"
                    upload_json(bucket_name, raw_json_path, order_line)
                else:
                    raw_json_folder = os.path.join(local_folder, "raw_jsons", str(order_date))
                    os.makedirs(raw_json_folder, exist_ok=True)
                    raw_json_path = os.path.join(raw_json_folder, filename)
                    with open(raw_json_path, "w", encoding="utf-8") as f:
                        json.dump(order_line, f, indent=4, ensure_ascii=False)

                #format line for discord
                industry_order_lines.append(clean_order_line(order_line))
        #send industry wise summary to discord
        if industry_order_lines:
            send_grouped_discord(industry, industry_order_lines, DISCORD_WEBHOOK_URL)
            order_flag = True

    if not order_flag:
        send_discord2("No orders announcements for today. Enjoy your day! ☀️", DISCORD_WEBHOOK_URL)
    if not test_mode:
        update_previous_ts(alert_name) 

if __name__ == "__main__":
    main_task()