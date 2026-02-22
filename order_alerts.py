from bse import BSE
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.exceptions import ReadTimeout
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from pypdf import PdfReader
from pdf2image import convert_from_path
import pytesseract
import shutil
import os
import json
from tabulate import tabulate
import textwrap
from util import send_discord, get_previous_ts, update_previous_ts
import time
import textwrap
from dotenv import load_dotenv
load_dotenv()


# ---- CONFIG ----

test_mode = False


# ---- Load env variables ---

if test_mode:
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_TMP')
else:
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_ORDER_WEBHOOK')

misc_str = os.getenv("ORDER_MISC_DATA")
misc_data = json.loads(misc_str)

BASE_URLS = misc_data.get('base_urls')
HEADERS = misc_data.get('headers')

KEYWORDS = misc_data.get('keywords')
OB_STOCKS = misc_data.get('ob_stocks')
if test_mode:
    OB_STOCKS = OB_STOCKS[:10]

allowed_cat = misc_data.get('allowed_cat')
forbidden_subcat = misc_data.get('forbidden_subcat')

allowed_cat_norm = {c.strip().lower() for c in allowed_cat}
forbidden_subcat_norm = {s.strip().lower() for s in forbidden_subcat}


# ---- Methods ----

def fetch_announcements(scripCode, from_dt, to_dt, max_anns=1000):
    anns: list[dict] = []
    page_count = 1
    bse = BSE(download_folder='data/orders_data/tmp')

    while True:
        # ---- minimal retry handling ----
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
                time.sleep(5)

        if page_count == 1:
            max_anns = res['Table1'][0]['ROWCNT']

        page_count += 1
        anns.extend(res['Table'])

        if len(anns) >= max_anns:
            break

        time.sleep(1)  # polite rate limit (important for BSE)

    return anns


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


# ---- Format Output & Communicate ----

def publish_orders(order_lines):
    orders_df = pd.DataFrame.from_dict(order_lines)

    industry_list = (orders_df["industry"]
                    .value_counts()        # counts rows per industry
                    .index                 # take the industry names
                    .tolist())             # convert to Python list

    # industries = set(orders_df['industry'].values.tolist())
    for industry in industry_list:
        ind_df = orders_df[orders_df['industry']==industry]
        ind_df = ind_df.sort_values(by=['sector', 'company', 'order date'], ascending=True)
        ind_df = ind_df[['company', 'order date', 'attachment']]

        message = "- - - - - Orders for industry : " + str(industry).upper() + '\n'
        table_str = message + tabulate(
            ind_df,
            headers="keys",
            tablefmt="grid",
            showindex=False
        ) + "\n```"

        send_discord(table_str, DISCORD_WEBHOOK_URL)

#PATH
log_path = 'data/log.json'
pdf_base = 'data/pdf/'

# session
session = requests.Session()
session.headers.update(HEADERS)


def main_task():

    #load last run time stamp
    prev_run_dt = get_previous_ts('order_alerts')

    from_dt = prev_run_dt
    to_dt = datetime.now(ZoneInfo("Asia/Kolkata"))

    send_discord(f"Morning! Processing anns from  {str(from_dt.date())} - {str(to_dt.date())}", DISCORD_WEBHOOK_URL)
    
    # load stocks list that announces order book
    stocks_df = pd.read_csv('data/stocks_list.csv', dtype={'bse_code': 'Int64'})
    stocks_df = stocks_df[stocks_df['bse_code'].isin(OB_STOCKS)]

    order_lines = []
    # iterate over stocks and load announcements for required duration
    for idx, row in stocks_df.iterrows():

        anns = fetch_announcements(row['bse_code'], from_dt, to_dt)
        # print(f'Total announcements for {row["company_name"]} : {len(anns)}')
        
        for idx, ann in enumerate(anns):
            if not ann.get('ATTACHMENTNAME'):
                #no attachment -> obvio not an order win ann
                continue
            # print(f'- - - - - - - - - Processing announcement : {idx} - - - - - - - - - -')
            category = str(ann.get('CATEGORYNAME', '')).strip().lower()
            subcat   = str(ann.get('SUBCATNAME', '')).strip().lower()

            if category not in allowed_cat_norm or subcat in forbidden_subcat_norm:
                continue
            #handle attachment
            downloaded, pdf_path, doc_url = download_pdf_fast(
                ann['ATTACHMENTNAME'],
                pdf_base + str(row['bse_code']),
                session
            )
            attachment_text = text_from_pdf(pdf_path, pg_limit=3)
            if contains_keywords(ann['NEWSSUB']+'. '+ann['HEADLINE']+'. '+ann['MORE']+attachment_text, KEYWORDS):
                order_lines.append({
                    'company': row['company_name'],
                    'order date': datetime.fromisoformat(ann['NEWS_DT']).date(),
                    'attachment': doc_url,
                    'industry': row['Industry'],
                    'sector': row['Sector'],
                })
            #delete current pdf folder
            if os.path.exists(pdf_base + str(row['bse_code'])):
                shutil.rmtree(pdf_base + str(row['bse_code']))
    
    if order_lines:
        publish_orders(order_lines)
    else:
        send_discord("No orders announcements for today. Enjoy your day! ☀️", DISCORD_WEBHOOK_URL)
    if not test_mode:
        update_previous_ts('order_alerts') 

if __name__ == "__main__":
    main_task()
