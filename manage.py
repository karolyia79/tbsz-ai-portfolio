import json
import os
import requests
import xml.etree.ElementTree as ET
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DB_FILE = os.getenv("DB_FILE", "/opt/ai_bot/portfolio_db.json")
TICKERS = ["O", "STAG", "MO", "VZ", "PEP", "CVX", "EPR", "MAIN", "NVO", "CCJ"]

def parse_float(val_str):
    if not val_str: return None
    cleaned = val_str.strip().replace(',', '.')
    try: return float(cleaned)
    except ValueError: return None

def format_qty(qty):
    qty = float(qty or 0)
    if qty.is_integer(): return f"{int(qty)}"
    return f"{qty:.4f}".rstrip('0').rstrip('.')

def get_mnb_usd_rate():
    url = "http://www.mnb.hu/arfolyamok.asmx"
    headers = {'Content-Type': 'text/xml; charset=utf-8'}
    body = """<?xml version="1.0" encoding="utf-8"?>
    <soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body><GetCurrentExchangeRates xmlns="http://www.mnb.hu/webservices/" /></soap:Body>
    </soap:Envelope>"""
    try:
        response = requests.post(url, data=body, headers=headers, timeout=10)
        root = ET.fromstring(response.content)
        result_str = root.find('.//{http://www.mnb.hu/webservices/}GetCurrentExchangeRatesResult').text
        inner_root = ET.fromstring(result_str)
        rate_str = inner_root.find('.//Day/Rate[@curr="USD"]').text
        return float(rate_str.replace(',', '.'))
    except Exception:
        try: return yf.Ticker("HUF=X").history(period="1d")['Close'].iloc[0]
        except Exception: return 360.0

def get_live_prices():
    prices = {}
    print("⏳ Élő piaci árfolyamok lekérése...", end="\r")
    for t in TICKERS:
        try:
            hist = yf.Ticker(t).history(period="1d")
            prices[t] = float(hist['Close'].iloc[-1]) if not hist.empty else 0.0
        except Exception:
            prices[t] = 0.0
    print(" " * 40, end="\r")
    return prices

def calculate_ibkr_fee_usd(qty, price_usd):
    trade_val_usd = qty * price_usd
    if trade_val_usd <= 0: return 0.0
    base_fee = max(1.00, qty * 0.005)
    max_fee = trade_val_usd * 0.01
    return min(base_fee, max_fee)

def load_db():
    if not os.path.exists(DB_FILE):
        return {"cash_huf": 0.0, "holdings": {t: {"qty": 0.0, "avg_price_usd": 0.0} for t in TICKERS}, "transactions": [], "dividends_log": []}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        if "transactions" not in data: data["transactions"] = []
        if "dividends_log" not in data: data["dividends_log"] = []
        if "holdings" not in data: data["holdings"] = {}
        return data

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_current_holdings(db):
    raw_h = db.get("holdings", {})
    holdings = {t: {"qty": 0.0, "total_cost_usd": 0.0, "avg_price_usd": 0.0} for t in TICKERS}
    for t, val in raw_h.items():
        if isinstance(val, dict):
            q = float(val.get("qty", 0.0))
            a = float(val.get("avg_price_usd", 0.0))
        else:
            q = float(val or 0.0)
            a = 0.0
        holdings[t] = {"qty": q, "total_cost_usd": q * a, "avg_price_usd": a}
        
    for tx in db.get("transactions", []):
        t = tx["ticker"]
        if t not in holdings:
            holdings[t] = {"qty": 0.0, "total_cost_usd": 0.0, "avg_price_usd": 0.0}
        qty = float(tx["qty"])
        price = float(tx["price_usd"])
        if tx["type"] == "BUY":
            holdings[t]["qty"] += qty
            holdings[t]["total_cost_usd"] += (qty * price)
        elif tx["type"] == "SELL":
            curr_qty = holdings[t]["qty"]
            if curr_qty > 0:
                avg_cost = holdings[t]["total_cost_usd"] / curr_qty
                holdings[t]["qty"] = max(0.0, curr_qty - qty)
                holdings[t]["total_cost_usd"] = holdings[t]["qty"] * avg_cost

    for t, info in holdings.items():
        q = info["qty"]
        info["avg_price_usd"] = (info["total_cost_usd"] / q) if q > 0 else 0.0
    return holdings

def show_status(db, rate, live_prices):
    cash_huf = float(db.get("cash_huf", 0.0))
    cash_usd = cash_huf / rate if rate > 0 else 0
    holdings = get_current_holdings(db)
    
    realized_profit_huf = sum(tx.get("realized_profit_huf", 0.0) for tx in db.get("transactions", []) if tx.get("type") == "SELL")
    div_total_huf = sum(d.get("amount_huf", 0.0) for d in db.get("dividends_log", []))

    print("\n" + "="*78)
    print("                📊 AKTUÁLIS PORTFÓLIÓ ÉS ÉLŐ PIACI ÁLLAPOT")
    print("="*78)
    print(f"🔱 MNB USD/HUF árfolyam:        {rate:.2f} Ft")
    print(f"💵 KP Konténer (Szabad HUF):     {cash_huf:,.0f} Ft (~${cash_usd:,.2f})")
    print(f"💰 Összes Kapott Osztalék:     +{div_total_huf:,.0f} Ft")
    print(f"📈 Realizált Árfolyam Nyereség:  {realized_profit_huf:+,.0f} Ft")
    print("-" * 78)
    print(f"{'Ticker':<6} | {'Állomány':<8} | {'Vétel Átlag':<11} | {'Élő Árfolyam':<12} | {'Érték ($)':<10} | {'Vétel Óta':<10}")
    print("-" * 78)
    
    total_val_usd = 0.0
    total_cost_usd = 0.0
    for t in TICKERS:
        info = holdings.get(t, {"qty": 0.0, "avg_price_usd": 0.0})
        q = info["qty"]
        avg_p = info["avg_price_usd"]
        live_p = live_prices.get(t, 0.0)
        
        pos_val = q * live_p
        cost_val = q * avg_p
        total_val_usd += pos_val
        total_cost_usd += cost_val
        profit_pct = ((live_p - avg_p) / avg_p * 100) if avg_p > 0 and q > 0 else 0.0
        
        if q > 0:
            print(f"{t:<6} | {format_qty(q):>6} db | ${avg_p:>9.2f} | ${live_p:>10.2f} | ${pos_val:>9.2f} | {profit_pct:>+8.2f}%")
        else:
            print(f"{t:<6} | {format_qty(q):>6} db | ${'0.00':>9} | ${live_p:>10.2f} | ${'0.00':>9} | {'---':>9}")
            
    unrealized_profit_usd = total_val_usd - total_cost_usd
    unrealized_profit_huf = unrealized_profit_usd * rate
    print("-" * 78)
    print(f"💼 Részvény Vagyon: ${total_val_usd:,.2f} (~{total_val_usd * rate:,.0f} Ft) | Nem Realizált Haszon: ${unrealized_profit_usd:+,.2f} (~{unrealized_profit_huf:+,.0f} Ft)")
    print("="*78)

def main():
    rate = get_mnb_usd_rate()
    live_prices = get_live_prices()
    while True:
        db = load_db()
        show_status(db, rate, live_prices)
        print("\nMIT SZERETNÉL CSINÁLNI?")
        print("1. Részvény VÁSÁRLÁS rögzítése")
        print("2. Részvény ELADÁS rögzítése")
        print("3. Kezdő Portfólió kézi beállítása")
        print("4. KP Konténer módosítása")
        print("5. Kilépés")
        c = input("\nVálasztás (1-5): ").strip()
        if c == "5": break

if __name__ == "__main__":
    main()
