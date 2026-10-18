import yfinance as yf
from google import genai
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import csv
import json
import os
from dotenv import load_dotenv

load_dotenv()

TICKERS = ["O", "STAG", "MO", "VZ", "PEP", "CVX", "EPR", "MAIN", "NVO", "CCJ"]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.5-flash-lite")
HA_WEBHOOK_URL = os.getenv("HA_WEBHOOK_URL")
LOG_FILE = os.getenv("LOG_FILE", "/opt/ai_bot/portfolio_log.csv")
DB_FILE = os.getenv("DB_FILE", "/opt/ai_bot/portfolio_db.json")

MONTH_NAMES = {
    1: "Január", 2: "Február", 3: "Március", 4: "Április",
    5: "Május", 6: "Június", 7: "Július", 8: "Augusztus",
    9: "Szeptember", 10: "Október", 11: "November", 12: "December"
}

print(f"=== 🚀 TBSZ AI BOT INDÍTÁSA (Modell: {MODEL_NAME}) ===")

client = genai.Client(api_key=GEMINI_API_KEY)

def format_qty(qty):
    qty = float(qty or 0)
    if qty.is_integer():
        return f"{int(qty)}"
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
        return yf.Ticker("HUF=X").history(period="1d")['Close'].iloc[0]

def load_portfolio_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "transactions" not in data: data["transactions"] = []
            if "dividends_log" not in data: data["dividends_log"] = []
            return data
    return {"cash_huf": 0.0, "transactions": [], "dividends_log": []}

def save_portfolio_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_current_holdings(db):
    holdings = {t: {"qty": 0.0, "total_cost_usd": 0.0, "avg_price_usd": 0.0} for t in TICKERS}
    for tx in db.get("transactions", []):
        t = tx["ticker"]
        if t not in holdings:
            holdings[t] = {"qty": 0.0, "total_cost_usd": 0.0, "avg_price_usd": 0.0}
            
        qty = float(tx["qty"])
        price = float(tx["price_usd"])
        tx_type = tx["type"]
        
        if tx_type == "BUY":
            holdings[t]["qty"] += qty
            holdings[t]["total_cost_usd"] += (qty * price)
        elif tx_type == "SELL":
            curr_qty = holdings[t]["qty"]
            if curr_qty > 0:
                avg_cost = holdings[t]["total_cost_usd"] / curr_qty
                holdings[t]["qty"] = max(0.0, curr_qty - qty)
                holdings[t]["total_cost_usd"] = holdings[t]["qty"] * avg_cost

    for t, info in holdings.items():
        q = info["qty"]
        info["avg_price_usd"] = (info["total_cost_usd"] / q) if q > 0 else 0.0
    return holdings

def auto_process_dividends(db, usd_huf):
    holdings = get_current_holdings(db)
    div_log = db.get("dividends_log", [])
    processed_ids = {d.get("event_id") for d in div_log if "event_id" in d}
    
    new_divs_total_huf = 0.0
    new_divs_summary = []
    
    for t in TICKERS:
        qty = float(holdings.get(t, {}).get("qty", 0.0))
        if qty <= 0: continue
        
        try:
            stock = yf.Ticker(t)
            divs = stock.dividends
            if divs.empty: continue
            
            cutoff_date = datetime.now() - timedelta(days=45)
            recent_divs = divs[divs.index >= cutoff_date.strftime("%Y-%m-%d")]
            
            for date_idx, div_per_share in recent_divs.items():
                event_date_str = date_idx.strftime("%Y-%m-%d")
                event_id = f"{t}_{event_date_str}"
                
                if event_id not in processed_ids:
                    payout_usd = float(div_per_share) * qty
                    payout_huf = payout_usd * usd_huf
                    
                    div_entry = {
                        "event_id": event_id,
                        "ticker": t,
                        "date": event_date_str,
                        "qty": qty,
                        "div_per_share": float(div_per_share),
                        "amount_usd": round(payout_usd, 2),
                        "amount_huf": round(payout_huf, 0)
                    }
                    div_log.append(div_entry)
                    processed_ids.add(event_id)
                    
                    db["cash_huf"] = float(db.get("cash_huf", 0.0)) + payout_huf
                    new_divs_total_huf += payout_huf
                    new_divs_summary.append(f"• **{t}**: +${payout_usd:.2f} (~{payout_huf:,.0f} Ft)")
        except Exception as e:
            print(f"    [!] {t} osztalék-ellenőrzési hiba: {e}")
            
    if new_divs_total_huf > 0:
        db["dividends_log"] = div_log
        save_portfolio_db(db)
        print(f"    [+] AUTOMATA OSZTALÉK JÓVÁÍRVA: +{new_divs_total_huf:,.0f} Ft")
        
    return new_divs_summary

def process_market_data(db, usd_huf):
    holdings = get_current_holdings(db)
    cash_huf = float(db.get("cash_huf", 0.0))
    cash_usd = cash_huf / usd_huf if usd_huf > 0 else 0
    
    napi_mozgas_text = f"**MNB USD/HUF:** {usd_huf:.2f} Ft | **KP Konténer:** {cash_huf:,.0f} Ft (~${cash_usd:,.2f} USD)\n\n"
    
    total_stock_val_usd = 0.0
    prev_stock_val_usd = 0.0
    total_cost_basis_usd = 0.0
    
    for t in TICKERS:
        info = holdings.get(t, {})
        qty = float(info.get("qty", 0.0))
        avg_price = float(info.get("avg_price_usd", 0.0))
        
        stock = yf.Ticker(t)
        hist = stock.history(period="2d")
        
        if len(hist) >= 2:
            prev_close = hist['Close'].iloc[0]
            current_close = hist['Close'].iloc[1]
            change_pct = ((current_close - prev_close) / prev_close) * 100
            
            pos_val = current_close * qty
            pos_prev_val = prev_close * qty
            cost_val = avg_price * qty
            
            total_stock_val_usd += pos_val
            prev_stock_val_usd += pos_prev_val
            total_cost_basis_usd += cost_val
            
            icon = "🟩" if change_pct >= 0 else "🟥"
            if qty > 0:
                qty_str = format_qty(qty)
                unrealized_profit_usd = pos_val - cost_val
                unrealized_profit_pct = (unrealized_profit_usd / cost_val * 100) if cost_val > 0 else 0.0
                u_icon = "📈" if unrealized_profit_usd >= 0 else "📉"
                
                napi_mozgas_text += f"{icon} **{t}** ({qty_str} db): ${current_close:.2f} ({change_pct:+.2f}% 24h)\n"
                napi_mozgas_text += f"   └ {u_icon} **Vétel óta:** ${unrealized_profit_usd:+,.2f} ({unrealized_profit_pct:+.2f}%) | Átlagár: ${avg_price:.2f}\n"
            else:
                napi_mozgas_text += f"⚪ **{t}** (0 db): ${current_close:.2f} ({change_pct:+.2f}%)\n"

    total_portfolio_usd = total_stock_val_usd + cash_usd
    total_portfolio_huf = total_portfolio_usd * usd_huf
    
    unrealized_total_usd = total_stock_val_usd - total_cost_basis_usd
    unrealized_total_huf = unrealized_total_usd * usd_huf
    unrealized_total_pct = (unrealized_total_usd / total_cost_basis_usd * 100) if total_cost_basis_usd > 0 else 0.0
    
    if prev_stock_val_usd > 0:
        stock_change_pct = ((total_stock_val_usd - prev_stock_val_usd) / prev_stock_val_usd) * 100
        tot_icon = "🟩" if stock_change_pct >= 0 else "🟥"
        napi_mozgas_text += f"\n{tot_icon} **NAPI 24H MOZGÁS: {stock_change_pct:+.2f}%**\n"
        
    napi_mozgas_text += f"📊 **TELJES NEM REALIZÁLT HASZON (VÉTEL ÓTA):** ${unrealized_total_usd:+,.2f} ({unrealized_total_pct:+.2f}%) ~ {unrealized_total_huf:+,.0f} Ft\n"
    napi_mozgas_text += f"💼 **TELJES PORTFÓLIÓ ÉRTÉK:** ${total_portfolio_usd:,.2f} (~{total_portfolio_huf:,.0f} Ft)\n"

    return napi_mozgas_text, total_portfolio_usd

def get_dividend_calendar(db, usd_huf, new_divs):
    holdings = get_current_holdings(db)
    cash_huf = float(db.get("cash_huf", 0.0))
    cash_usd = cash_huf / usd_huf if usd_huf > 0 else 0
    
    div_log = db.get("dividends_log", [])
    txs = db.get("transactions", [])
    
    now = datetime.now()
    curr_month_str = now.strftime("%Y-%m")
    curr_year_str = now.strftime("%Y")
    
    m_div_huf = sum(d.get("amount_huf", 0.0) for d in div_log if d.get("date", "").startswith(curr_month_str))
    y_div_huf = sum(d.get("amount_huf", 0.0) for d in div_log if d.get("date", "").startswith(curr_year_str))
    total_realized_huf = sum(tx.get("realized_profit_huf", 0.0) for tx in txs if tx.get("type") == "SELL")
    
    div_text = f"💵 **Szabad KP keret:** {cash_huf:,.0f} Ft (~${cash_usd:,.2f} USD)\n"
    div_text += f"💰 **Ebben a hónapban beérkezett osztalék:** +{m_div_huf:,.0f} Ft\n"
    div_text += f"📅 **Idei ({curr_year_str}) összes osztalékbevétel:** +{y_div_huf:,.0f} Ft\n"
    div_text += f"📈 **Realizált Árfolyamnyereség:** {total_realized_huf:+,.0f} Ft\n\n"
    
    if new_divs:
        div_text += "🎉 **ÚJONNAN JÓVÁÍRT OSZTALÉKOK:**\n" + "\n".join(new_divs) + "\n\n"

    total_annual_div_usd = 0.0
    for t in TICKERS:
        qty = float(holdings.get(t, {}).get("qty", 0.0))
        if qty > 0:
            try:
                div_rate = yf.Ticker(t).info.get('dividendRate', 0) or 0
                total_annual_div_usd += (div_rate * qty)
            except: pass
            
    div_text += f"📊 **VÁRHATÓ ÉVES OSZTALÉK ÖSSZESEN:** ${total_annual_div_usd:,.2f} (~{total_annual_div_usd * usd_huf:,.0f} Ft/év)\n"
    return div_text

def generate_smart_report(market_data, db, usd_huf):
    cash_huf = float(db.get("cash_huf", 0.0))
    cash_usd = cash_huf / usd_huf if usd_huf > 0 else 0
    
    prompt = f"""
    Te egy profi osztalék-befektetési tanácsadó vagy.
    
    A befektető rendelkezésére álló szabad készpénz (KP Konténer): {cash_huf:,.0f} Ft (~${cash_usd:.2f} USD).
    A bróker az Interactive Brokers (IBKR), ahol a minimális tranzakciós díj $1.00 USD.
    
    SZIGORÚ SZABÁLY:
    Ha a portfólió papírjaiban NINCS +-3%-nál nagyobb elmozdulás, és nincs kiemelkedő piaci lehetőség, válaszolj EGYETLEN SZÓT: SKIP
    
    Ha van érdemi infó VAGY ha a KP konténerben van érdemi összeghatár (>${15000:.0f} Ft felett), írj egy Markdown jelentést:
    1. Kiemelt saját mozgások.
    2. Osztalék-reinvestációs tanács: Miből érdemes vásárolni a meglévő {cash_huf:,.0f} Ft (~${cash_usd:.2f} USD) keretből az ideális portfólió-súlyozáshoz?
    
    Adatok:
    {market_data}
    """
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return response.text.strip()

def send_to_home_assistant(elemzes, mozgas, osztalek):
    payload = {"elemzes": elemzes, "napi_mozgas": mozgas, "osztalek": osztalek}
    requests.post(HA_WEBHOOK_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=10)

if __name__ == "__main__":
    usd_huf = get_mnb_usd_rate()
    db = load_portfolio_db()
    
    new_divs = auto_process_dividends(db, usd_huf)
    napi_mozgas, total_val = process_market_data(db, usd_huf)
    osztalek_naptar = get_dividend_calendar(db, usd_huf, new_divs)
    ai_riport = generate_smart_report(napi_mozgas, db, usd_huf)
    
    if "SKIP" in ai_riport:
        cash_huf = float(db.get("cash_huf", 0.0))
        ai_riport = f"🟢 **Piacok csendesek.** A portfólió értéke stabil. A KP konténeredben lévő **{cash_huf:,.0f} Ft** keretet gyűjtsd tovább a következő jó vételi lehetőségig."
        
    send_to_home_assistant(ai_riport, napi_mozgas, osztalek_naptar)
    print("=== ✅ SIKERES AUTOMATA FRISSÍTÉS ===")
