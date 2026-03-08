import requests
import json
import time
import os
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# =========================================================
# 🔑 CREDENCIALES (Configuradas en Render)
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_IDS_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# ⚙️ CONFIGURACIÓN "BARREDOR TOTAL"
# =========================================================
UMBRAL_ALERTA = 500       
MIN_LIQUIDITY = 150       # Filtro de entrada (Liquidez real en API)
INTERVALO_ESC_SEG = 300   # Escaneo cada 5 minutos
DEPTH_PERCENT = 10.0      

# --- 🚫 TU BLACKLIST PERSONALIZADA ---
blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'stoppage',
    'points', 'rebounds', 'assists', 'pts', 'reb', 'ast', 'spread', 'game',
    'xrp', 'btc', 'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

app = Flask(__name__)
memoria_deltas = {}

@app.route('/')
def home():
    # Muestra cuántos mercados pasaron tus filtros y están siendo vigilados
    return f"🛰️ Radar 10k Activo. Mercados en vigilancia: {len(memoria_deltas)}", 200

def enviar_telegram(mensaje):
    """ Reparte el mensaje a cada ID de la lista por separado """
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": cid, "text": mensaje, "parse_mode": "Markdown"}
        try:
            requests.post(url, data=payload, timeout=10)
        except:
            print(f"❌ Error enviando a {cid}")

def calcular_delta_mercado(m):
    """ Análisis profundo de los libros de órdenes """
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
    except:
        return None

def bucle_principal():
    global memoria_deltas
    print("🤖 Iniciando Barredor de 10,000 Mercados con Blacklist personalizada...")
    enviar_telegram("🚜 *Modo Barredor 10k:* Filtros de deporte/crypto aplicados. Iniciando...")

    while True:
        try:
            all_m = []
            offset = 0
            # Paginación para no cansar la API
            while offset < 10000:
                print(f"📦 Escaneando bloque: {offset}...")
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                data = requests.get(url, timeout=15).json()
                
                if not data: break
                all_m.extend(data)
                offset += 100
                
                # Si bajamos de 10 de liquidez, ya no vale la pena seguir
                if float(data[-1].get('liquidity', 0)) < 10: break
                time.sleep(0.1)

            # APLICANDO TU NUEVA BLACKLIST
            filtrados = [m for m in all_m if not any(w in m.get('question','').lower() for w in blacklist) and float(m.get('liquidity', 0)) >= MIN_LIQUIDITY]
            
            print(f"🔎 Filtrado listo: {len(filtrados)} mercados pasaron el filtro.")

            with ThreadPoolExecutor(max_workers=8) as executor:
                resultados = list(executor.map(calcular_delta_mercado, filtrados))

            ahora = time.time()
            for i, m in enumerate(filtrados):
                id_m = m['question']
                d_actual = resultados[i]
                if d_actual is None: continue
                
                if id_m in memoria_deltas:
                    cambio = d_actual - memoria_deltas[id_m]['delta']
                    if abs(cambio) >= UMBRAL_ALERTA:
                        tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                        emoji = "🐋" if abs(cambio) > 3000 else "🐟"
                        enviar_telegram(f"{emoji} *MOVIMIENTO DETECTADO*\n\n📌 *{id_m}*\n💰 *Var:* `${cambio:,} USD` (Delta: `${d_actual:,}`)\n⚖️ *Acción:* {tipo}\n🔗 [Ver mercado](https://polymarket.com/event/{m['slug']})")
                    memoria_deltas[id_m]['delta'] = d_actual
                else:
                    memoria_deltas[id_m] = {'delta': d_actual, 'first_seen': ahora}

            print(f"✅ Ciclo completado. Vigilando {len(memoria_deltas)} mercados.")
            time.sleep(INTERVALO_ESC_SEG)
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
