import json

import requests
import pandas as pd
import time
import random
import os
from tabulate import tabulate
from datetime import datetime, timedelta
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from util import send_discord2, get_previous_ts, update_previous_ts

# ---- CONFIG ----

test_mode = False


# ---- Load env variables ---
load_dotenv()

misc_str = os.getenv("INSIDER_MISC_DATA")
misc_data = json.loads(misc_str)

BASE_URL = misc_data['base_url']
API_URL = misc_data['api_url']

BASE_HEADER = misc_data['base_header']
API_HEADER = misc_data['api_header']

if test_mode:
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_TMP')
else:
    DISCORD_WEBHOOK_URL = os.getenv('DISCORD_INSIDER_WEBHOOK')

# ---- Methods & Classes ----

class XInsiderScraper:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = BASE_URL
        self.api_url = API_URL
        
        # Modern headers to look like a standard Chrome browser
        self.session.headers.update(BASE_HEADER)

    def _init_cookies(self):
        """Initializes the session by visiting the homepage and setting the referer."""
        try:
            # Clear cookies to avoid 'stale' session errors
            self.session.cookies.clear()
            self.session.get(self.base_url, timeout=15)
            # Crucial: This pause mimics a user 'loading' the page
            time.sleep(random.uniform(3, 6))
        except Exception as e:
            print(f"Failed to initialize session: {e}")

    def get_data(self, symbol, from_date, to_date, max_retries=3):
        """ Fetches insider trading data for a symbol.Dates must be in DD-MM-YYYY format. """
        params = {
            "index": "equities",
            "symbol": symbol,
            "from": from_date.strftime('%d-%m-%Y'),
            "to": to_date.strftime('%d-%m-%Y')
        }
        for attempt in range(max_retries):
            try:
                self._init_cookies()
                api_headers = API_HEADER
                response = self.session.get(self.api_url, params=params, headers=api_headers, timeout=20)
                #Note: api returns top 5 rows even when there is no data in provided interval
                
                if response.status_code == 200:
                    json_data = response.json()
                    df = pd.DataFrame(json_data.get('data', []))
                    if df.empty:
                        return None
                    #handle date parsing & filtering here since api is weird 
                    df['date'] = pd.to_datetime(df['date'], errors='coerce')
                    from_dt = pd.to_datetime(from_date, dayfirst=True)
                    to_dt = pd.to_datetime(to_date, dayfirst=True)
                    df = df[(df['date'] >= from_dt) & (df['date'] <= to_dt)]
                    return df if not df.empty else None

                elif response.status_code == 403:
                    print(f"🚫 403 Forbidden for {symbol}. Likely rate limited.")
                else:
                    print(f"⚠️ Request failed for {symbol}. Status: {response.status_code}")

            except (requests.exceptions.RequestException, Exception) as e:
                print(f"❌ Attempt {attempt + 1} failed for {symbol}: {e}")

            # --- Exponential Backoff Logic ---
            if attempt < max_retries - 1:
                sleep_time = (2 ** attempt) * 5 + random.uniform(1, 3)
                time.sleep(sleep_time)
        print(f"🛑 Max retries reached for {symbol}. Returning None.")
        return None

def get_insider_summary(insider_lines, actual_from_dt=None, actual_to_dt=None):
    
    df1 = insider_lines.copy()
    df1['date'] = pd.to_datetime(df1['date'], format='%d-%b-%Y %H:%M')
    df1['secVal'] = pd.to_numeric(df1['secVal'], errors='coerce')

    #  Filter on mode of acquisition & type of secuirity & person category
    valid_moas = ['Market Purchase', 'Market Sale', 'Off Market', 'Others']
    df1 = df1[df1['acqMode'].isin(valid_moas)]

    #  Filter on type of secuirity 
    df2 = df1[df1['secType']=='Equity Shares']

    #  Filter on person category
    eligible_pcats = ['Promoters', 'Promoter Group']
    df3 = df2[df2['personCategory'].isin(eligible_pcats)]

    # In last 3 months if there is any sort of promoter selling 
    # (except inter-se-transfer/gift) then reject those companies
    # If any promoter sold in last X months => ABORT
    if len(df3[df3['tdpTransactionType']=='Sell']) > 0:
        return False, None

    #Summarise for date interval user asked for
    df4 = df2[(df2['date'] >= actual_from_dt) & (df2['date'] <= actual_to_dt)]

    valid_pcats = ['Promoters', 'Promoter Group', 'Immediate relative']
    valid_ttypes = ['Buy']
    df5 = df4[df4['personCategory'].isin(valid_pcats)]
    df6 = df5[df5['tdpTransactionType'].isin(valid_ttypes)]
    
    if df6.empty:
        return False, None
    results = df6.agg({'secVal': 'sum', 'date': 'max'})

    return True, {'Value(cr)': results['secVal']*1.0/10000000, 'XTimes': len(df6), 'LatestTxnOn': results['date']}


# ---- Format Output & Communicate ----

def publish_insiders(insider_lines):
    insiders_df = pd.DataFrame.from_dict(insider_lines)
    
    # Pre-processing
    insiders_df['Value(cr)'] = pd.to_numeric(insiders_df['Value(cr)']).round(2)
    insiders_df['LatestTxnOn'] = pd.to_datetime(insiders_df['LatestTxnOn']).dt.date
    
    industry_list = insiders_df['Sector'].unique()
    
    for industry in industry_list:
        sector_insiders_df = insiders_df[insiders_df['Sector'] == industry]
        sector_insiders_df.sort_values(by=['Value(cr)'], ascending=False, inplace=True)
        sector_insiders_df.reset_index(drop=True, inplace=True)

        header = f"**🏢   Industry   :   {industry}**\n"
        body = "```\n"
        
        for idx, row in sector_insiders_df.iterrows():
            # We use padding (e.g., :9) to keep the colons aligned
            body += f"{'NAME':<10}: {row['Name']}\n"
            body += f"{'VALUE':<10}: {row['Value(cr)']}\n"
            body += f"{'X-TIMES':<10}: {row['XTimes']}\n"
            body += f"{'DATE':<10}: {row['LatestTxnOn']}\n"
            if idx != len(sector_insiders_df) - 1:
                body += f"{'-' * 20}\n"
        body += "```"
        send_discord2(header + body, DISCORD_WEBHOOK_URL)


# ---- Main ----

def main_task():

    #load last run time stamp
    # prev_run_dt = get_previous_ts('insider_alerts')

    from_dt = datetime(2025, 9, 1, 0, 0)
    to_dt = datetime(2025, 12, 31, 0, 0) #datetime.now()

    send_discord2(f"Hello! Fetching insider trades from  {str(from_dt.date())} - {str(to_dt.date())}", DISCORD_WEBHOOK_URL)
    send_discord2(f"Disc: Only NSE based insider alerts", DISCORD_WEBHOOK_URL)

    scraper = XInsiderScraper()

    #Load base stocks list    
    stocks_df = pd.read_csv('data/stocks_list_sectorv1.csv') 
    stocks_df = stocks_df.dropna(subset=['nse_code'])
    
    if test_mode:
        stocks_df = stocks_df[stocks_df['nse_code']=='WCIL'] #.sample(50, random_state=42)

    insider_lines = []
    sectors_list = set(stocks_df['sector_v1'].values.tolist())
    
    for sector in sectors_list:
        print(f"Processing sector: {sector}")
        sector_stocks = stocks_df[stocks_df['sector_v1'] == sector]

        for idx, row in sector_stocks.iterrows():
            print('processgn stock: ' + row['nse_code'])
            from_3M_ago = to_dt - timedelta(days=90)
            deals_df = scraper.get_data(row['nse_code'], from_3M_ago, to_dt)
            if deals_df is None or len(deals_df) == 0:
                continue
            eligible, insider_item = get_insider_summary(deals_df, from_dt, to_dt)
            
            if eligible:
                insider_item['Sector'] = sector
                insider_item['Name'] = row['nse_code']
                insider_lines.append(insider_item)
            time.sleep(1)
        if insider_lines:
            publish_insiders(insider_lines)
            insider_lines = [] #reset for next sector
        time.sleep(15)


if __name__ == "__main__":
    main_task()