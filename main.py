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
# ⚙️ CONFIGURACIÓN "FRANCOTIRADOR 5.4" (Filtros Estrictos)
# =========================================================
UMBRAL_ALERTA = 1000       
UMBRAL_BALLENA_TOP = 11000  
MIN_LIQUIDITY = 400        
INTERVALO_ESC_SEG = 300   
DEPTH_PERCENT = 10.0      

# 🧠 MICROESTRUCTURA
MIN_SPREAD_PCT = 2.0       
MAX_SPREAD_PCT = 30.0      # Filtro estricto: Mayor a esto es un mercado roto
MIN_TOXICITY_PCT = 3.0     

# 🎯 ZONA DE ORO
MIN_PRICE = 0.04           
MAX_PRICE = 0.96           

blacklist = [

    'rounds', 'fight', 'ko', 'tko', 'vs', 'stoppage', 'points', 'rebounds', 

    'assists', 'pts', 'reb', 'ast', 'spread', 'game', 'xrp', 'btc', 

    'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',

    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'

]

app = Flask(__name__)
memoria_deltas = {}
session = requests.Session()

@app.route('/')
def home():
    return f"🛰️ Radar 5.4 Online | Vigilando: {len(memoria_deltas)}", 200

def enviar_telegram(mensaje):
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": cid, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": False}
        try: session.post(url, data=payload, timeout=10)
        except: pass

def calcular_datos_mercado(m):
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5"]'))
        if len(tokens) < 2: return None
        
        precio_mkt = float(prices[0])
        if precio_mkt < MIN_PRICE or precio_mkt > MAX_PRICE: return None
            
        token_yes = tokens[0]
        res_yes = session.get(f"https://clob.polymarket.com/book?token_id={token_yes}", timeout=5).json()
        
        bids, asks = res_yes.get('bids', []), res_yes.get('asks', [])
        best_bid = float(bids[0]['price']) if bids else 0.0
        best_ask = float(asks[0]['price']) if asks else 1.0
        
        dist = precio_mkt * (DEPTH_PERCENT / 100.0)
        piso, techo = precio_mkt - dist, precio_mkt + dist
        b_usd = sum(float(b['price']) * float(b['size']) for b in bids if float(b['price']) >= piso)
        a_usd = sum(float(a['price']) * float(a['size']) for a in asks if float(a['price']) <= techo)
        
        return {'delta': int(b_usd - a_usd), 'best_bid': best_bid, 'best_ask': best_ask, 'precio': precio_mkt}
    except: return None

def bucle_principal():
    global memoria_deltas
    print("🤖 Radar 5.4: Iniciando sistema...")
    enviar_telegram("⚡ *Radar 5.4 Online:* Filtro de Spread Estricto (Máx 25%) activado para TODOS los movimientos.")

    while True:
        try:
            all_m = []
            offset = 0
            while offset < 5000:
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                data = session.get(url, timeout=15).json()
                if not data: break
                all_m.extend(data)
                offset += 100
                time.sleep(0.1) 

            filtrados = [m for m in all_m if not any(w in m.get('question','').lower() for w in blacklist) and float(m.get('liquidity', 0)) >= MIN_LIQUIDITY]
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                resultados = list(executor.map(calcular_datos_mercado, filtrados))

            for i, m in enumerate(filtrados):
                id_m = m['question']
                datos = resultados[i]
                liquidez_m = int(float(m.get('liquidity', 0)))
                
                if datos is None or liquidez_m == 0: continue
                
                d_actual = datos['delta']
                
                if id_m in memoria_deltas:
                    delta_pasado = memoria_deltas[id_m]['delta']
                    cambio = d_actual - delta_pasado
                    
                    mid = (datos['best_ask'] + datos['best_bid']) / 2.0
                    spread = round(((datos['best_ask'] - datos['best_bid']) / mid) * 100, 2) if mid > 0 else 0
                    tox = round((abs(cambio) / liquidez_m) * 100, 2)
                    
                    # 🛡️ REGLA DE SEGURIDAD TOTAL: Si el spread es > MAX_SPREAD_PCT, ignoramos SIEMPRE.
                    if spread <= MAX_SPREAD_PCT:
                        
                        # Vía 1: Ballena (>5k)
                        es_ballena_top = abs(cambio) >= UMBRAL_BALLENA_TOP
                        
                        # Vía 2: Insider (>1.5k + filtros)
                        pasa_filtros_micro = abs(cambio) >= UMBRAL_ALERTA and (spread >= MIN_SPREAD_PCT or tox >= MIN_TOXICITY_PCT)

                        if es_ballena_top or pasa_filtros_micro:
                            tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                            alerta_emoji = "🐋 BALLENA" if es_ballena_top else "🥷 INSIDER"
                            
                            mensaje = (
                                f"🚨 *ALERTA {alerta_emoji}* 🚨\n\n"
                                f"📌 *{id_m}*\n\n"
                                f"💰 *Cambio:* `${cambio:,} USD`\n"
                                f"🕰️ *Delta Anterior:* `${delta_pasado:,} USD`\n"
                                f"📊 *Delta Actual:* `${d_actual:,} USD`\n"
                                f"💧 *Liq:* `${liquidez_m:,} USD`\n"
                                f"⚖️ *Acción:* {tipo}\n\n"
                                f"🧠 *Análisis:*\n"
                                f"🏷️ *Precio:* `${datos['precio']:.3f}`\n"
                                f"📈 *Bid/Ask:* `${datos['best_bid']:.3f}` / `${datos['best_ask']:.3f}`\n"
                                f"🏃 *Spread:* `{spread}%` | 🌊 *Tox:* `{tox}%` \n\n"
                                f"🔗 [Ver en Polymarket](https://polymarket.com/event/{m['slug']})"
                            )
                            enviar_telegram(mensaje)
                
                memoria_deltas[id_m] = {'delta': d_actual}

            print(f"✅ Ciclo OK. Vigilando: {len(memoria_deltas)}")
            time.sleep(INTERVALO_ESC_SEG)
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
