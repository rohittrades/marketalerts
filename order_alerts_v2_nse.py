import shutil

from google import genai
from google.api_core import exceptions
from models import AnnouncementType, AnnouncementModel
from util import GICSAutomator, clean_order_line, get_prompt, send_discord2, update_previous_ts, upload_json, send_grouped_discord
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
local_run = True
alert_name = 'order_alerts_v2_nse'

# ---- Load env variables ---

DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_TMP')

# NSE config (hardcoded — no env var needed)
NSE_BASE_URL = "https://www.nseindia.com"
NSE_API_URL = "https://www.nseindia.com/api/corporate-announcements"

NSE_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NSE_API_HEADERS = {
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
}

KEYWORDS = [
    "order", "contract", "awarded", "received", "bagged", "won", "secured",
    "LOA", "letter of award", "work order", "EPC", "purchase order",
    "supply order", "tender", "bid", "mandate", "engagement",
    "appointed", "selected", "empanelled", "commissioned", "subcontract"
]

# Manually fill before running
OB_STOCKS = ["VINYAS"]  # e.g., ["LT", "BEL", "HAL", "RELIANCE", ...]

if test_mode:
    OB_STOCKS = ["VINYAS"]

""" Gemini configuration """

MAX_COUNT = 248 #Free gemini limit is 250

classifier_model = os.getenv('OCLASSIFIER_MODEL')
extractor_model = os.getenv('OEXTRACTOR_MODEL')
llm_key = os.getenv("GEMINI_PAID_KEY1")

""" GCP configuration """

bucket_name = os.getenv("GCP_BUCKET")
oa_v2_folder = f"alerts/{alert_name}"
local_folder = "data/orders_data_nse"

# ---- Load prompts ---

CLASSIFIER_PROMPT = get_prompt('prompts/oclassifier.txt')
EXTRACTION_PROMPT = get_prompt('prompts/oextractor.txt')

# ---- Base paths ---

pdf_base = 'data/pdf_nse/'
industry_taxonomy_path = 'data/gics_map_2023.csv'
indexed_gics_path = 'data/gics_index.faiss'


# ---- NSE Announcement Fetcher ----

class NseAnnouncementFetcher:
    """Fetches corporate announcements from NSE with cookie-based session management."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(NSE_BASE_HEADERS)

    def _init_cookies(self):
        """Visit NSE homepage to initialize session cookies."""
        try:
            self.session.cookies.clear()
            self.session.get(NSE_BASE_URL, timeout=15)
            time.sleep(random.uniform(3, 6))
        except Exception as e:
            print(f"Failed to initialize NSE session: {e}")

    def fetch_announcements(self, symbol, from_dt, to_dt, max_retries=3):
        """Fetch corporate announcements for a symbol from NSE. Tries both equities and sme indices."""
        all_anns = []
        for index in ["equities", "sme"]:
            params = {
                "index": index,
                "symbol": symbol,
                "from_date": from_dt.strftime('%d-%m-%Y'),
                "to_date": to_dt.strftime('%d-%m-%Y'),
            }
            for attempt in range(max_retries):
                try:
                    self._init_cookies()
                    response = self.session.get(
                        NSE_API_URL, params=params,
                        headers=NSE_API_HEADERS, timeout=20
                    )
                    if response.status_code == 200:
                        anns = response.json()
                        if anns:
                            all_anns.extend(anns)
                        break  # success, move to next index
                    elif response.status_code == 403:
                        print(f"🚫 403 for {symbol} ({index}), likely rate limited.")
                    else:
                        print(f"⚠️ Request failed for {symbol} ({index}). Status: {response.status_code}")
                except (requests.exceptions.RequestException, Exception) as e:
                    print(f"❌ Attempt {attempt + 1} failed for {symbol} ({index}): {e}")

                if attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) * 5 + random.uniform(1, 3)
                    time.sleep(sleep_time)
            else:
                print(f"🛑 Max retries reached for {symbol} ({index}).")

        return all_anns


# ---- Methods ----

# ---- Attachment handling ----
def is_valid_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except:
        return False

def download_pdf_nse(url, out_dir, session):
    """Download PDF from full NSE attachment URL."""
    os.makedirs(out_dir, exist_ok=True)
    fname = url.split('/')[-1]
    out_path = os.path.join(out_dir, fname)

    try:
        r = session.get(url, timeout=15, stream=True)
        if r.status_code != 200:
            print(f'Download failed, status {r.status_code}: {url}')
            return False, None, None

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

    print('Download failed ⛔️ url : ', url)
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

    # Hardcoded dates for now (skip GCS log)
    from_dt = datetime(2025, 9, 1, 0, 0)
    to_dt = datetime(2026, 3, 24, 0, 0)
    welcome_message = f"Morning! reading announcements from NSE.. \nRunning Order Alerts v2 (NSE) from {str(from_dt.date())} - {str(to_dt.date())}"
    send_discord2(welcome_message, DISCORD_WEBHOOK_URL)

    # load stocks list (using nse_code)
    stocks_df = pd.read_csv('data/stocks_list_sectorv1.csv')
    stocks_df = stocks_df.dropna(subset=['nse_code'])
    ob_stocks = stocks_df[stocks_df['nse_code'].isin(OB_STOCKS)]

    # preparation
    client = genai.Client(api_key=llm_key)
    fetcher = NseAnnouncementFetcher()

    automator = GICSAutomator(industry_taxonomy_path)
    order_flag = False

    for industry in ob_stocks['industry'].unique():
        print(f"** Processing industry: {industry} **")
        industry_stocks = ob_stocks[ob_stocks['industry'] == industry]

        industry_order_lines = []
        for idx, row in industry_stocks.iterrows():
            anns = fetcher.fetch_announcements(row['nse_code'], from_dt, to_dt)
            if not anns:
                continue
            print(f"*stock: {row['company_name']} - total anns: {len(anns)}*")
            for idx, ann in enumerate(anns):
                if not ann.get('attchmntFile'):
                    continue

                #download attachment (NSE provides full URL)
                downloaded, pdf_full_path, doc_url = download_pdf_nse(
                    ann['attchmntFile'],
                    pdf_base + str(row['nse_code']),
                    fetcher.session
                )
                success2pager, anns_2pager_path = extract_first_two_pages(pdf_full_path, output_path=None)
                if not (downloaded and success2pager):
                    print("Attachment download or processing failed, skipping announcement.")
                    print(doc_url)
                    print(f"pdf download status : {downloaded}, 2 pager success: {success2pager}")
                    continue

                # keyword matching
                attachment_text = text_from_pdf(pdf_full_path, pg_limit=3)
                combined_text = '  '.join([attachment_text, ann.get('attchmntText', '')])
                if not contains_keywords(combined_text, KEYWORDS):
                    continue

                #upload required documents to gcp and get public urls for llm access
                file1_reference = client.files.upload(file=anns_2pager_path, config={'display_name': 'Company Announcement 2-Pager'})
                file2_reference = client.files.upload(file=pdf_full_path, config={'display_name': 'Company Announcement'})
                wait_for_files_active(client, [file1_reference, file2_reference])

                #clear local pdfs to save space
                if os.path.exists(pdf_base + str(row['nse_code'])):
                    shutil.rmtree(pdf_base + str(row['nse_code']))
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
                order_date = datetime.strptime(ann['sort_date'], '%Y-%m-%d %H:%M:%S').date().isoformat()
                misc_info = {
                    'company': row['company_name'],
                    'nse_code': row['nse_code'],
                    'industry': industry,
                    'order_date': order_date,
                    'attachment': doc_url
                    }
                order_line = {**misc_info, **extracted_info.model_dump(), **target_industry_dict}

                #save response as json locally and/or upload to gcp
                filename = f"{row['nse_code']}_{ann['seq_id']}.json"
                if not local_run:
                    #upload line level json with unique_name
                    raw_json_path = f"{oa_v2_folder}/raw_jsons/{order_date}/{filename}"
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
            df = pd.DataFrame.from_dict(industry_order_lines)
            df.to_csv('check_gpt.csv')
            order_flag = True

    if not order_flag:
        send_discord2("No orders announcements for today. Enjoy your day! ☀️", DISCORD_WEBHOOK_URL)
    if not test_mode:
        update_previous_ts(alert_name)

if __name__ == "__main__":
    main_task()
