from flask import Flask, render_template_string, request, jsonify
import json
import os
import subprocess
import requests
import xml.etree.ElementTree as ET
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DB_FILE = os.getenv("DB_FILE", "/opt/ai_bot/portfolio_db.json")
TICKERS = ["O", "STAG", "MO", "VZ", "PEP", "CVX", "EPR", "MAIN", "NVO", "CCJ"]

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

def calculate_ibkr_fee_usd(qty, price_usd):
    trade_val_usd = qty * price_usd
    if trade_val_usd <= 0: return 0.0
    base_fee = max(1.00, qty * 0.005)
    max_fee = trade_val_usd * 0.01
    return min(base_fee, max_fee)

def load_db():
    if not os.path.exists(DB_FILE):
        return {"cash_huf": 0.0, "holdings": {}, "transactions": [], "dividends_log": []}
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
    holdings = {t: {"qty": 0.0, "total_cost_usd": 0.0, "avg_price_usd": 0.0} for t in TICKERS}
    raw_h = db.get("holdings", {})
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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="hu">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TBSZ Portfólió Kezelő</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #111a2e; color: #e1e6ed; margin: 0; padding: 12px; }
        .card { background-color: #1c273c; border-radius: 10px; padding: 14px; margin-bottom: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { margin-top: 0; color: #4da6ff; font-size: 1.1rem; border-bottom: 1px solid #2d3b55; padding-bottom: 6px; }
        .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 10px; }
        .stat-box { background-color: #24324a; padding: 8px; border-radius: 6px; text-align: center; }
        .stat-title { font-size: 0.75rem; color: #9ab0d1; }
        .stat-val { font-size: 0.95rem; font-weight: bold; color: #4dff88; margin-top: 2px; }
        table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-top: 6px; }
        th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #2d3b55; }
        th { color: #829abf; font-size: 0.7rem; }
        input, select, button { width: 100%; padding: 8px; margin-top: 4px; margin-bottom: 8px; border-radius: 6px; border: 1px solid #3b4d6c; background-color: #0d1526; color: #fff; box-sizing: border-box; font-size: 0.85rem; }
        button { background-color: #0066cc; color: white; border: none; font-weight: bold; cursor: pointer; }
        button:hover { background-color: #0052a3; }
        .btn-refresh { background-color: #28a745; margin-top: 10px; }
        .flex-row { display: flex; gap: 8px; }
        .flex-row > div { flex: 1; }
        .positive { color: #4dff88; }
        .negative { color: #ff4d4d; }
    </style>
</head>
<body>
    <div class="card">
        <h2>📊 TBSZ Portfólió Műszerfal</h2>
        <div class="grid">
            <div class="stat-box">
                <div class="stat-title">USD/HUF Árfolyam</div>
                <div class="stat-val" style="color: #ffd11a;">{{ "%.2f"|format(rate) }} Ft</div>
            </div>
            <div class="stat-box">
                <div class="stat-title">KP Konténer</div>
                <div class="stat-val">{{ "{:,.0f}".format(db.cash_huf) }} Ft</div>
            </div>
            <div class="stat-box">
                <div class="stat-title">Teljes Portfólió</div>
                <div class="stat-val" style="color: #00d2ff;">${{ "{:,.2f}".format(total_val_usd) }}</div>
            </div>
            <div class="stat-box">
                <div class="stat-title">Nem Realizált Haszon</div>
                <div class="stat-val {{ 'positive' if unrealized_usd >= 0 else 'negative' }}">${{ "{:+,.2f}".format(unrealized_usd) }}</div>
            </div>
            <div class="stat-box">
                <div class="stat-title">Realizált Nyereség</div>
                <div class="stat-val {{ 'positive' if realized_huf >= 0 else 'negative' }}">{{ "{:+,.0f}".format(realized_huf) }} Ft</div>
            </div>
            <div class="stat-box">
                <div class="stat-title">Idei Osztalék</div>
                <div class="stat-val">+{{ "{:,.0f}".format(y_div_huf) }} Ft</div>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>Db</th>
                    <th>Átlagár</th>
                    <th>Élő Ár</th>
                    <th>Érték ($)</th>
                    <th>Vétel Óta</th>
                </tr>
            </thead>
            <tbody>
                {% for t, info in holdings.items() %}
                {% set live_p = live_prices.get(t, 0.0) %}
                {% set pos_val = info.qty * live_p %}
                {% set cost_val = info.qty * info.avg_price_usd %}
                {% set profit_pct = ((live_p - info.avg_price_usd) / info.avg_price_usd * 100) if info.avg_price_usd > 0 and info.qty > 0 else 0.0 %}
                <tr>
                    <td><b>{{ t }}</b></td>
                    <td>{{ "%.2f"|format(info.qty) if info.qty % 1 != 0 else info.qty|int }}</td>
                    <td>${{ "%.2f"|format(info.avg_price_usd) }}</td>
                    <td>${{ "%.2f"|format(live_p) }}</td>
                    <td>${{ "%.2f"|format(pos_val) }}</td>
                    <td class="{{ 'positive' if profit_pct >= 0 else 'negative' }}">{{ "%+.2f"|format(profit_pct) }}%</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

    <div class="card">
        <h2>🛒 Részvény Tranzakció (Vétel / Eladás)</h2>
        <form action="/api/transaction" method="POST">
            <div class="flex-row">
                <div>
                    <label>Típus</label>
                    <select name="type">
                        <option value="BUY">Vétel (BUY)</option>
                        <option value="SELL">Eladás (SELL)</option>
                    </select>
                </div>
                <div>
                    <label>Ticker</label>
                    <select name="ticker" id="tickerSelect" onchange="updatePrice()">
                        {% for t in tickers %}
                        <option value="{{ t }}">{{ t }}</option>
                        {% endfor %}
                    </select>
                </div>
            </div>
            <div class="flex-row">
                <div>
                    <label>Darabszám</label>
                    <input type="text" name="qty" placeholder="pl: 1.5" required>
                </div>
                <div>
                    <label>Darabár ($)</label>
                    <input type="text" name="price_usd" id="priceInput" placeholder="Élő ár" required>
                </div>
            </div>
            <button type="submit">Tranzakció Elkönyvelése</button>
        </form>
    </div>

    <div class="card">
        <h2>💵 KP Konténer Módosítás</h2>
        <form action="/api/cash" method="POST">
            <label>Összeg HUF (+ befizetés, - kivét)</label>
            <input type="text" name="amount_huf" placeholder="pl: 50000 vagy -10000" required>
            <button type="submit">KP Konténer Frissítése</button>
        </form>
    </div>

    <div class="card">
        <h2>📝 Kezdő Állomány Kézi Beállítása</h2>
        <form action="/api/holdings_manual" method="POST">
            <div class="flex-row">
                <div>
                    <label>Ticker</label>
                    <select name="ticker">
                        {% for t in tickers %}
                        <option value="{{ t }}">{{ t }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Összes Db</label>
                    <input type="text" name="qty" placeholder="pl: 10" required>
                </div>
                <div>
                    <label>Átlag vételár ($)</label>
                    <input type="text" name="avg_price_usd" placeholder="pl: 52.30" required>
                </div>
            </div>
            <button type="submit" style="background-color: #6c757d;">Kezdő Állomány MENTÉSE</button>
        </form>
    </div>

    {% if db.transactions %}
    <div class="card">
        <h2>📜 Utolsó Tranzakciók</h2>
        <table>
            <thead>
                <tr>
                    <th>Dátum</th>
                    <th>Típus</th>
                    <th>Ticker</th>
                    <th>Db</th>
                    <th>Ár ($)</th>
                </tr>
            </thead>
            <tbody>
                {% for tx in db.transactions[-10:]|reverse %}
                <tr>
                    <td>{{ tx.date }}</td>
                    <td><b style="color: {{ '#4dff88' if tx.type == 'BUY' else '#ff4d4d' }}">{{ tx.type }}</b></td>
                    <td>{{ tx.ticker }}</td>
                    <td>{{ tx.qty }}</td>
                    <td>${{ tx.price_usd }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% endif %}

    <div class="card">
        <form action="/api/trigger" method="POST">
            <button type="submit" class="btn-refresh">🔄 AI Elemzés és HA Frissítés Indítása</button>
        </form>
    </div>

    <script>
        const livePrices = {{ live_prices|tojson }};
        function updatePrice() {
            const ticker = document.getElementById('tickerSelect').value;
            if (livePrices[ticker]) {
                document.getElementById('priceInput').value = livePrices[ticker].toFixed(2);
            }
        }
        updatePrice();
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    rate = get_mnb_usd_rate()
    db = load_db()
    holdings = get_current_holdings(db)
    
    live_prices = {}
    total_stock_val_usd = 0.0
    total_cost_usd = 0.0
    
    for t in TICKERS:
        try:
            hist = yf.Ticker(t).history(period="1d")
            lp = float(hist['Close'].iloc[-1]) if not hist.empty else 0.0
            live_prices[t] = lp
        except:
            live_prices[t] = 0.0
            
        q = holdings[t]["qty"]
        a = holdings[t]["avg_price_usd"]
        total_stock_val_usd += (q * live_prices[t])
        total_cost_usd += (q * a)
        
    cash_huf = float(db.get("cash_huf", 0.0))
    cash_usd = cash_huf / rate if rate > 0 else 0
    total_val_usd = total_stock_val_usd + cash_usd
    unrealized_usd = total_stock_val_usd - total_cost_usd
    
    txs = db.get("transactions", [])
    realized_huf = sum(tx.get("realized_profit_huf", 0.0) for tx in txs if tx.get("type") == "SELL")
    
    curr_year_str = datetime.now().strftime("%Y")
    div_log = db.get("dividends_log", [])
    y_div_huf = sum(d.get("amount_huf", 0.0) for d in div_log if d.get("date", "").startswith(curr_year_str))
            
    return render_template_string(
        HTML_TEMPLATE, 
        db=db, holdings=holdings, rate=rate, tickers=TICKERS, live_prices=live_prices,
        total_val_usd=total_val_usd, unrealized_usd=unrealized_usd, realized_huf=realized_huf, y_div_huf=y_div_huf
    )

@app.route("/api/transaction", methods=["POST"])
def handle_transaction():
    tx_type = request.form.get("type")
    ticker = request.form.get("ticker")
    qty_str = request.form.get("qty", "").replace(',', '.')
    price_str = request.form.get("price_usd", "").replace(',', '.')
    
    try:
        q = float(qty_str)
        p = float(price_str)
        rate = get_mnb_usd_rate()
        db = load_db()
        holdings = get_current_holdings(db)
        avg_buy_p = holdings.get(ticker, {}).get("avg_price_usd", 0.0)
        
        trade_val_usd = q * p
        comm_usd = calculate_ibkr_fee_usd(q, p)
        total_usd = trade_val_usd + comm_usd
        total_huf = total_usd * rate
        cash_huf = float(db.get("cash_huf", 0.0))
        
        if tx_type == "BUY":
            db["cash_huf"] = max(0.0, cash_huf - total_huf)
            db["transactions"].append({
                "id": f"TX_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "type": "BUY",
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "ticker": ticker,
                "qty": q,
                "price_usd": p,
                "usd_huf": rate,
                "fee_usd": comm_usd,
                "total_huf": total_huf
            })
        elif tx_type == "SELL":
            net_sale_usd = trade_val_usd - comm_usd
            net_sale_huf = net_sale_usd * rate
            
            cost_usd = q * avg_buy_p
            profit_usd = net_sale_usd - cost_usd
            profit_huf = profit_usd * rate
            
            db["cash_huf"] = cash_huf + net_sale_huf
            db["transactions"].append({
                "id": f"TX_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "type": "SELL",
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "ticker": ticker,
                "qty": q,
                "price_usd": p,
                "usd_huf": rate,
                "fee_usd": comm_usd,
                "net_sale_huf": net_sale_huf,
                "realized_profit_usd": profit_usd,
                "realized_profit_huf": profit_huf
            })
            
        save_db(db)
        subprocess.Popen(["/opt/ai_bot/venv/bin/python3", "/opt/ai_bot/tbsz_ai.py"])
        return '<script>alert("Sikeres tranzakció!"); window.location.href="/";</script>'
    except Exception as e:
        return f'<script>alert("Hiba: {e}"); window.location.href="/";</script>'

@app.route("/api/cash", methods=["POST"])
def handle_cash():
    amount_str = request.form.get("amount_huf", "").replace(',', '.')
    try:
        amt = float(amount_str)
        db = load_db()
        db["cash_huf"] = max(0.0, float(db.get("cash_huf", 0.0)) + amt)
        save_db(db)
        subprocess.Popen(["/opt/ai_bot/venv/bin/python3", "/opt/ai_bot/tbsz_ai.py"])
        return '<script>alert("KP Konténer frissítve!"); window.location.href="/";</script>'
    except Exception as e:
        return f'<script>alert("Hiba: {e}"); window.location.href="/";</script>'

@app.route("/api/holdings_manual", methods=["POST"])
def handle_holdings_manual():
    ticker = request.form.get("ticker")
    qty_str = request.form.get("qty", "").replace(',', '.')
    price_str = request.form.get("avg_price_usd", "").replace(',', '.')
    try:
        q = float(qty_str)
        p = float(price_str)
        db = load_db()
        if "holdings" not in db: db["holdings"] = {}
        db["holdings"][ticker] = {"qty": q, "avg_price_usd": p}
        save_db(db)
        subprocess.Popen(["/opt/ai_bot/venv/bin/python3", "/opt/ai_bot/tbsz_ai.py"])
        return '<script>alert("Kezdő állomány elmentve!"); window.location.href="/";</script>'
    except Exception as e:
        return f'<script>alert("Hiba: {e}"); window.location.href="/";</script>'

@app.route("/api/trigger", methods=["POST"])
def handle_trigger():
    subprocess.Popen(["/opt/ai_bot/venv/bin/python3", "/opt/ai_bot/tbsz_ai.py"])
    return '<script>alert("Frissítés elindítva!"); window.location.href="/";</script>'

if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", 5005))
    app.run(host="0.0.0.0", port=port)
