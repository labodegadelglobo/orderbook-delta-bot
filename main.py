import requests
import json
import time
import os
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# --- CONFIGURACIÓN DE TELEGRAM ---
TELEGRAM_TOKEN = "TU_TOKEN_AQUÍ"
TELEGRAM_CHAT_ID = "TU_CHAT_ID_AQUÍ"

# --- CONFIGURACIÓN DEL RADAR ---
UMBRAL_ALERTA = 5000     # Avisar si el Delta cambia más de $5,000
MIN_LIQUIDITY = 1200     
INTERVALO_SEGUNDOS = 300 # 5 Minutos
DEPTH_PERCENT = 10.0

# --- BLACKLIST ---
blacklist = ['rounds', 'fight', 'ko', 'tko', 'stoppage', 'decision', 'points', 'pts', 'xrp', 'btc', 'eth', 'sol', 'nba', 'nfl', 'soccer', 'ufc', 'mlb']

app = Flask(__name__)
memoria_deltas = {}

@app.route('/')
def home():
    return "Bot Centinela está VIVO 🛰️", 200

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, data=payload, timeout=10)
    except: print("❌ Error Telegram")

def obtener_delta(m):
    try:
        tokens = json.loads(m['clobTokenIds'])
        precio_mkt = float(json.loads(m['outcomePrices'])[0])
        res_yes = requests.get(f"https://clob.polymarket.com/book?token_id={tokens[0]}", timeout=5).json()
        res_no = requests.get(f"https://clob.polymarket.com/book?token_id={tokens[1]}", timeout=5).json()
        dist = precio_mkt * (DEPTH_PERCENT / 100.0)
        piso, techo = precio_mkt - dist, precio_mkt + dist
        bids = sum(float(b['price']) * float(b['size']) for b in res_yes.get('bids', []) if float(b['price']) >= piso)
        bids += sum((1.0 - float(a['price'])) * float(a['size']) for a in res_no.get('asks', []) if (1.0 - float(a['price'])) >= piso)
        asks = sum(float(a['price']) * float(a['size']) for a in res_yes.get('asks', []) if float(a['price']) <= techo)
        asks += sum((1.0 - float(b['price'])) * float(b['size']) for b in res_no.get('bids', []) if (1.0 - float(b['price'])) <= techo)
        return int(bids - asks)
    except: return None

def bucle_scanner():
    global memoria_deltas
    while True:
        try:
            print(f"🔄 [{datetime.now().strftime('%H:%M:%S')}] Escaneando...")
            all_m, offset = [], 0
            while offset < 1500:
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                d = requests.get(url, timeout=10).json()
                if not d: break
                all_m.extend(d)
                offset += 100
            
            filtrados = [m for m in all_m if not any(w in m['question'].lower() for w in blacklist) and float(m.get('liquidity', 0)) >= MIN_LIQUIDITY]
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                resultados = list(executor.map(obtener_delta, filtrados))

            for i, m in enumerate(filtrados):
                id_m = m['question']
                d_actual = resultados[i]
                if d_actual is None: continue
                if id_m in memoria_deltas:
                    cambio = d_actual - memoria_deltas[id_m]
                    if abs(cambio) >= UMBRAL_ALERTA:
                        emoji = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                        enviar_telegram(f"🚨 *MOVIMIENTO*\n{id_m}\n\nCambio: `${cambio:,}`\nActual: `${d_actual:,}`\n{emoji}")
                memoria_deltas[id_m] = d_actual
                
            time.sleep(INTERVALO_SEGUNDOS)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    # Arrancar el scanner en un hilo separado
    threading.Thread(target=bucle_scanner, daemon=True).start()
    # Arrancar el servidor web para Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)