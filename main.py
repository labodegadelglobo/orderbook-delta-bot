import requests
import json
import time
import os
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# =========================================================
# 🔑 CREDENCIALES
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_IDS_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# ⚙️ CONFIGURACIÓN "TURBO BARREDOR 2.0"
# =========================================================
UMBRAL_ALERTA = 5000       
MIN_LIQUIDITY = 1000       # Subimos a 150 para filtrar mercados con más "carne"
INTERVALO_ESC_SEG = 300   
DEPTH_PERCENT = 10.0      

blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'vs', 'stoppage', 'points', 'rebounds', 
    'assists', 'pts', 'reb', 'ast', 'spread', 'game', 'xrp', 'btc', 
    'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

app = Flask(__name__)
memoria_deltas = {}

@app.route('/')
def home():
    return f"🛰️ Radar 10k Online | Mercados en vigilancia: {len(memoria_deltas)}", 200

def enviar_telegram(mensaje):
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": cid, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": False}
        try: requests.post(url, data=payload, timeout=10)
        except: pass

def calcular_delta_mercado(m):
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5"]'))
        if len(tokens) < 2: return None
        token_yes = tokens[0]
        precio_mkt = float(prices[0])
        res_yes = requests.get(f"https://clob.polymarket.com/book?token_id={token_yes}", timeout=5).json()
        dist = precio_mkt * (DEPTH_PERCENT / 100.0)
        piso, techo = precio_mkt - dist, precio_mkt + dist
        b_usd = sum(float(b['price']) * float(b['size']) for b in res_yes.get('bids', []) if float(b['price']) >= piso)
        a_usd = sum(float(a['price']) * float(a['size']) for a in res_yes.get('asks', []) if float(a['price']) <= techo)
        return int(b_usd - a_usd)
    except: return None

def bucle_principal():
    global memoria_deltas
    print("🤖 Modo Perfeccionado Online.")
    enviar_telegram("🚀 *Radar Actualizado:* Formato de alertas perfeccionado y monitoreo de 10k activo.")

    while True:
        try:
            all_m = []
            offset = 0
            while offset < 5000:
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                data = requests.get(url, timeout=15).json()
                if not data: break
                all_m.extend(data)
                offset += 100
                if float(data[-1].get('liquidity', 0)) < 10: break
                time.sleep(0.05)

            filtrados = [m for m in all_m if not any(w in m.get('question','').lower() for w in blacklist) and float(m.get('liquidity', 0)) >= MIN_LIQUIDITY]
            
            with ThreadPoolExecutor(max_workers=12) as executor:
                resultados = list(executor.map(calcular_delta_mercado, filtrados))

            for i, m in enumerate(filtrados):
                id_m = m['question']
                d_actual = resultados[i]
                liquidez_m = int(float(m.get('liquidity', 0)))
                
                if d_actual is None: continue
                
                if id_m in memoria_deltas:
                    cambio = d_actual - memoria_deltas[id_m]['delta']
                    if abs(cambio) >= UMBRAL_ALERTA:
                        tipo = "🟢 COMPRA MASIVA" if cambio > 0 else "🔴 VENTA MASIVA"
                        
                        # NUEVO FORMATO SOLICITADO
                        mensaje_alert = (
                            f"🚨 *MOVIMIENTO DETECTADO EN MERCADO*\n\n"
                            f"📌 *{id_m}*\n\n"
                            f"💰 *Cambio de Delta:* `${cambio:,} USD`\n"
                            f"📊 *Delta Actual:* `${d_actual:,} USD`\n"
                            f"💧 *Liquidez:* `${liquidez_m:,} USD`\n"
                            f"⚖️ *Acción:* {tipo}\n\n"
                            f"🔗 [Ver en Polymarket](https://polymarket.com/event/{m['slug']})"
                        )
                        enviar_telegram(mensaje_alert)
                    memoria_deltas[id_m]['delta'] = d_actual
                else:
                    memoria_deltas[id_m] = {'delta': d_actual}

            print(f"✅ Ciclo terminado. Memoria: {len(memoria_deltas)}")
            time.sleep(INTERVALO_ESC_SEG)
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))


