# marketalerts
Sends alerts to comm channel on frequent basis

## Order Alerts

### BSE (`order_alerts_v2.py`)
- Sources corporate announcements from **BSE India API**
- Filters for order/contract win announcements
- Extracts structured order data via LLM (GPT)
- Saves raw JSONs to `data/orders_data/raw_jsons/`
- Sends formatted alerts to Telegram

### NSE (`order_alerts_v2_nse.py`) — WIP
- Same pipeline as BSE but sources announcements from **NSE India API**
- Saves raw JSONs to `data/orders_data_nse/raw_jsons/`
- Dates are hardcoded for now (no GCS log file yet)
- `desc` filter skipped for now (accepts false positives)
- Known fix applied: `clean_order_line` handles `None` `award_status`

### Utilities
- `util.py` — shared helpers (`clean_order_line`, formatting, etc.)
- `vinyas_orders.csv` — consolidated order history for VINYAS from NSE raw JSONs
