import json
import os
import requests
import xml.etree.ElementTree as ET
import yfinance as yf
from datetime import datetime

DB_FILE = "/opt/ai_bot/portfolio_db.json"
TICKERS = ["O", "STAG", "MO", "VZ", "PEP", "CVX", "EPR", "MAIN", "NVO", "CCJ"]

def parse_float(val_str):
    if not val_str:
        return None
    cleaned = val_str.strip().replace(',', '.')
    try:
        return float(cleaned)
    except ValueError:
        return None

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
        try:
            return yf.Ticker("HUF=X").history(period="1d")['Close'].iloc[0]
        except Exception:
            return 360.0

def get_live_prices():
    """Lekéri az élő piaci árakat a Yahoo Finance-ről"""
    prices = {}
    print("⏳ Élő piaci árfolyamok lekérése...", end="\r")
    for t in TICKERS:
        try:
            hist = yf.Ticker(t).history(period="1d")
            if not hist.empty:
                prices[t] = float(hist['Close'].iloc[-1])
            else:
                prices[t] = 0.0
        except Exception:
            prices[t] = 0.0
    print(" " * 40, end="\r")
    return prices

def calculate_ibkr_fee_usd(qty, price_usd):
    trade_val_usd = qty * price_usd
    if trade_val_usd <= 0:
        return 0.0
    base_fee = max(1.00, qty * 0.005)
    max_fee = trade_val_usd * 0.01
    return min(base_fee, max_fee)

def load_db():
    if not os.path.exists(DB_FILE):
        return {
            "cash_huf": 0.0,
            "holdings": {t: {"qty": 0.0, "avg_price_usd": 0.0} for t in TICKERS},
            "transactions": [],
            "dividends_log": []
        }
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
    """Tranzakciók és kézi felvitel összefésülése"""
    txs = db.get("transactions", [])
    raw_holdings = db.get("holdings", {})
    
    holdings = {t: {"qty": 0.0, "total_cost_usd": 0.0, "avg_price_usd": 0.0} for t in TICKERS}
    
    # 1. Kézi állományok betöltése alapként
    for t, val in raw_holdings.items():
        if isinstance(val, dict):
            q = float(val.get("qty", 0.0))
            a = float(val.get("avg_price_usd", 0.0))
        else:
            q = float(val or 0.0)
            a = 0.0
        holdings[t] = {"qty": q, "total_cost_usd": q * a, "avg_price_usd": a}
        
    # 2. Tranzakciók hozzáadása / levonása
    for tx in txs:
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

def show_transaction_history(db):
    txs = db.get("transactions", [])
    print("\n" + "="*70)
    print("               📜 TRANZAKCIÓS ELŐZMÉNYEK (VÉTELEK / ELADÁSOK)")
    print("="*70)
    if not txs:
        print("Még nincs rögzített tranzakció.")
    else:
        for i, tx in enumerate(txs, 1):
            ttype = "🛒 VÉTEL" if tx["type"] == "BUY" else "💰 ELADÁS"
            print(f"{i:>2}. [{tx['date']}] {ttype:<8} | {tx['ticker']:<5} | {format_qty(tx['qty'])} db @ ${tx['price_usd']:.2f} | MNB: {tx['usd_huf']:.2f} Ft")
    print("="*70)

def main():
    rate = get_mnb_usd_rate()
    live_prices = get_live_prices()
    
    while True:
        db = load_db()
        show_status(db, rate, live_prices)
        print("\nMIT SZERETNÉL CSINÁLNI?")
        print("1. Részvény VÁSÁRLÁS rögzítése (Élő piaci árfolyam felajánlásával)")
        print("2. Részvény ELADÁS rögzítése (Élő piaci árfolyam felajánlásával)")
        print("3. Kezdő Portfólió / Állomány kézi beállítása (Tranzakciók nélkül)")
        print("4. KP Konténer egyenleg módosítása (+/- HUF befizetés/kivét)")
        print("5. Tranzakciós Napló megtekintése")
        print("6. Árfolyamok frissítése")
        print("7. Kilépés")
        
        choice = input("\nVálasztás (1-7): ").strip()
        
        if choice == "1":
            t = input("\nMelyik papírból vásároltál? (pl. O, STAG, MO): ").strip().upper()
            if t in TICKERS:
                live_p = live_prices.get(t, 0.0)
                qty_str = input(f"Hány darab {t}-t vettél? (pl: 1.5): ").strip()
                price_str = input(f"Mi volt a darabár ($)? [Élő piaci ár: ${live_p:.2f}, Enter = ezen az áron]: ").strip()
                
                q = parse_float(qty_str)
                p = parse_float(price_str) if price_str else live_p
                
                if q is not None and p is not None and p > 0:
                    trade_val_usd = q * p
                    comm_usd = calculate_ibkr_fee_usd(q, p)
                    total_usd = trade_val_usd + comm_usd
                    total_huf = total_usd * rate
                    cash_huf = float(db.get("cash_huf", 0.0))
                    
                    print("\n--- 🛒 VÁSÁRLÁS ÖSSZESÍTŐ ---")
                    print(f"Részvény:             {q} db {t} @ ${p:.2f}")
                    print(f"Részvény értéke:      ${trade_val_usd:,.2f}")
                    print(f"IBKR tranzakciós díj: ${comm_usd:,.2f}")
                    print(f"TELJES KÖLTSÉG:       ${total_usd:,.2f} -> {total_huf:,.0f} Ft")
                    
                    if cash_huf >= total_huf or input("\nNincs elég KP. Ennek ellenére rögzítjük? (i/n): ").lower() == 'i':
                        db["cash_huf"] = max(0.0, cash_huf - total_huf)
                        db["transactions"].append({
                            "id": f"TX_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                            "type": "BUY",
                            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "ticker": t,
                            "qty": q,
                            "price_usd": p,
                            "usd_huf": rate,
                            "fee_usd": comm_usd,
                            "total_huf": total_huf
                        })
                        save_db(db)
                        print("✅ Vásárlás rögzítve a Tranzakciós Naplóban!")
                else:
                    print("❌ Érvénytelen szám formátum!")
            else:
                print("❌ Ismeretlen ticker!")
                
        elif choice == "2":
            t = input("\nMelyik papírból adtál el? (pl. O): ").strip().upper()
            if t in TICKERS:
                holdings = get_current_holdings(db)
                curr_qty = holdings.get(t, {}).get("qty", 0.0)
                avg_buy_p = holdings.get(t, {}).get("avg_price_usd", 0.0)
                live_p = live_prices.get(t, 0.0)
                
                if curr_qty <= 0:
                    print("❌ Ebből a papírból jelenleg nincs eladható állományod!")
                    continue
                    
                qty_str = input(f"Hány darab {t}-t adtál el? (Max: {format_qty(curr_qty)} db): ").strip()
                price_str = input(f"Mi volt az eladási darabár ($)? [Élő piaci ár: ${live_p:.2f}, Enter = ezen az áron]: ").strip()
                
                q = parse_float(qty_str)
                p = parse_float(price_str) if price_str else live_p
                
                if q is not None and p is not None and q <= curr_qty and p > 0:
                    gross_sale_usd = q * p
                    comm_usd = calculate_ibkr_fee_usd(q, p)
                    net_sale_usd = gross_sale_usd - comm_usd
                    net_sale_huf = net_sale_usd * rate
                    
                    cost_usd = q * avg_buy_p
                    profit_usd = net_sale_usd - cost_usd
                    profit_huf = profit_usd * rate
                    profit_pct = (profit_usd / cost_usd * 100) if cost_usd > 0 else 0.0
                    
                    print("\n--- 💰 ELADÁS ÉS PROFIT ÖSSZESÍTŐ ---")
                    print(f"Bekerülési érték: ${cost_usd:,.2f}")
                    print(f"Nettó eladási ár: ${net_sale_usd:,.2f} (~{net_sale_huf:,.0f} Ft)")
                    print(f"REALIZÁLT NYERESÉG: ${profit_usd:+,.2f} ({profit_pct:+.2f}%) -> {profit_huf:+,.0f} Ft")
                    
                    db["cash_huf"] = float(db.get("cash_huf", 0.0)) + net_sale_huf
                    db["transactions"].append({
                        "id": f"TX_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                        "type": "SELL",
                        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "ticker": t,
                        "qty": q,
                        "price_usd": p,
                        "usd_huf": rate,
                        "fee_usd": comm_usd,
                        "net_sale_huf": net_sale_huf,
                        "realized_profit_usd": profit_usd,
                        "realized_profit_huf": profit_huf
                    })
                    save_db(db)
                    print("✅ Eladás rögzítve a Tranzakciós Naplóban!")
                else:
                    print("❌ Érvénytelen darabszám vagy ár!")
            else:
                print("❌ Ismeretlen ticker!")
                
        elif choice == "3":
            print("\n--- Kezdő Portfólió / Állomány kézi beállítása ---")
            raw_h = db.get("holdings", {})
            for t in TICKERS:
                c_info = raw_h.get(t, {})
                c_q = c_info.get("qty", 0.0) if isinstance(c_info, dict) else float(c_info or 0)
                c_a = c_info.get("avg_price_usd", 0.0) if isinstance(c_info, dict) else 0.0
                
                v_q = input(f"\n{t} darabszáma (Jelenlegi: {format_qty(c_q)} db, Enter = változatlan): ").strip()
                p_q = parse_float(v_q)
                
                v_a = input(f"{t} átlagos bekerülési ára ($) (Jelenlegi: ${c_a:.2f}, Enter = változatlan): ").strip()
                p_a = parse_float(v_a)
                
                if p_q is not None or p_a is not None:
                    raw_h[t] = {
                        "qty": p_q if p_q is not None else c_q,
                        "avg_price_usd": p_a if p_a is not None else c_a
                    }
            db["holdings"] = raw_h
            save_db(db)
            print("\n✅ Kezdő portfólió sikeresen frissítve!")
            
        elif choice == "4":
            val = input("\nÍrd be az összeget HUF-ban (+ befizetés, - kivét): ").strip()
            parsed = parse_float(val)
            if parsed is not None:
                db["cash_huf"] = max(0.0, float(db.get("cash_huf", 0.0)) + parsed)
                save_db(db)
                print(f"✅ Új KP egyenleg: {db['cash_huf']:,.0f} Ft")
            else:
                print("❌ Érvénytelen szám formátum!")
                
        elif choice == "5":
            show_transaction_history(db)
            input("\nNyomj Entert a visszatéréshez...")
            
        elif choice == "6":
            rate = get_mnb_usd_rate()
            live_prices = get_live_prices()
            print("✅ Árfolyamok frissítve!")
            
        elif choice == "7":
            print("\nViszlát!")
            break

if __name__ == "__main__":
    main()
