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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# =========================================================
# ⚙️ CONFIGURACIÓN "MODO TITÁN" + CRONÓMETRO (SIN OI)
# =========================================================
UMBRAL_ALERTA = 1000       # Sensibilidad +/- $500 USD
MIN_LIQUIDITY = 100       # Mínimo $100 USD de liquidez real
INTERVALO_ESC_SEG = 300   # Escaneo cada 5 minutos
DEPTH_PERCENT = 10.0      # Rango de profundidad (10%)

# --- 🚫 BLACKLIST ---
blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'stoppage', 'decision',
    'points', 'rebounds', 'assists', 'pts', 'reb', 'ast', 'win', 'spread', 'vs', 'game',
    'xrp', 'btc', 'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

app = Flask(__name__)
memoria_deltas = {}

@app.route('/')
def home():
    return f"🛰️ Radar 10k Activo. Mercados en memoria: {len(memoria_deltas)}", 200

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, data=payload, timeout=10)
    except: pass

def calcular_delta_mercado(m):
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5"]'))
        if len(tokens) < 2: return None
        
        token_yes, token_no = tokens[0], tokens[1]
        precio_mkt = float(prices[0])
        
        res_yes = requests.get(f"https://clob.polymarket.com/book?token_id={token_yes}", timeout=5).json()
        res_no = requests.get(f"https://clob.polymarket.com/book?token_id={token_no}", timeout=5).json()
        
        dist = precio_mkt * (DEPTH_PERCENT / 100.0)
        piso, techo = precio_mkt - dist, precio_mkt + dist
        
        b_usd = sum(float(b['price']) * float(b['size']) for b in res_yes.get('bids', []) if float(b['price']) >= piso)
        b_usd += sum((1.0 - float(a['price'])) * float(a['size']) for a in res_no.get('asks', []) if (1.0 - float(a['price'])) >= piso)
        
        a_usd = sum(float(a['price']) * float(a['size']) for a in res_yes.get('asks', []) if float(a['price']) <= techo)
        a_usd += sum((1.0 - float(b['price'])) * float(b['size']) for b in res_no.get('bids', []) if (1.0 - float(a['price'])) <= techo)
        
        return int(b_usd - a_usd)
    except: return None

def bucle_principal():
    global memoria_deltas
    print("🤖 Centinela 10k (Versión Limpia) Iniciado...")
    enviar_telegram("🚀 *Radar Titán Online:* Escaneando 10,000 mercados (Lógica optimizada).")

    while True:
        try:
            # 1. BARRIDO MASIVO (Hasta 10k)
            all_m, offset = [], 0
            while offset < 10000:
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                data = requests.get(url, timeout=10).json()
                if not data or len(data) == 0: break
                all_m.extend(data)
                offset += 100
                if float(data[-1].get('liquidity', 0)) < 20: break

            # 2. FILTRADO
            filtrados = []
            for m in all_m:
                txt = f"{m.get('question','')} {m.get('category','')} {m.get('groupItemTitle','')}".lower()
                if any(word in txt for word in blacklist): continue
                if float(m.get('liquidity', 0)) < MIN_LIQUIDITY: continue
                filtrados.append(m)

            # 3. ANÁLISIS MULTIHILO (Sin límite fijo de workers)
            with ThreadPoolExecutor() as executor:
                resultados = list(executor.map(calcular_delta_mercado, filtrados))

            # 4. DETECCIÓN Y TIEMPO
            ahora = time.time()
            for i, m in enumerate(filtrados):
                id_m = m['question']
                d_actual = resultados[i]
                if d_actual is None: continue
                
                if id_m in memoria_deltas:
                    info_vieja = memoria_deltas[id_m]
                    cambio = d_actual - info_vieja['delta']
                    minutos_seguimiento = int((ahora - info_vieja['first_seen']) / 60)
                    
                    if abs(cambio) >= UMBRAL_ALERTA:
                        tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                        emoji = "🐋" if abs(cambio) > 5000 else "🐟"
                        tiempo_txt = f"{minutos_seguimiento} min" if minutos_seguimiento < 60 else f"{minutos_seguimiento//60}h {minutos_seguimiento%60}min"

                        enviar_telegram(
                            f"{emoji} *MOVIMIENTO DETECTADO*\n\n"
                            f"📌 *{id_m}*\n\n"
                            f"💰 *Variación:* `${cambio:,} USD`\n"
                            f"⚖️ *Delta Total:* `${d_actual:,} USD`\n"
                            f"⚖️ *Acción:* {tipo}\n"
                            f"⏱️ *Tiempo Monitoreado:* `{tiempo_txt}`\n"
                            f"🔗 [Ver en Polymarket](https://polymarket.com/event/{m['slug']})"
                        )
                    memoria_deltas[id_m]['delta'] = d_actual
                else:
                    memoria_deltas[id_m] = {'delta': d_actual, 'first_seen': ahora}

            print(f"✅ Ciclo completado: {datetime.now().strftime('%H:%M:%S')} - Memoria: {len(memoria_deltas)}")
            time.sleep(INTERVALO_ESC_SEG)
            
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
