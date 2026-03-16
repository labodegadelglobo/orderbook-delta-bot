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
# ⚙️ CONFIGURACIÓN "FRANCOTIRADOR 5.1" (Ajustables)
# =========================================================
UMBRAL_ALERTA = 3000       # Sugerencia: Bajar a 3000 para cazar Inside Info temprano
MIN_LIQUIDITY = 500        # Sugerencia: Bajar a 500 para ver mercados emergentes
INTERVALO_ESC_SEG = 300   
DEPTH_PERCENT = 10.0      

# 🧠 NUEVOS FILTROS DE MICROESTRUCTURA
MIN_SPREAD_PCT = 3.0       # % Mínimo de hueco (Urgencia) entre comprador y vendedor
MAX_SPREAD_PCT = 25.0      # % Máximo de hueco (Evita mercados fantasma/rotos)
MIN_TOXICITY_PCT = 5.0     # % Mínimo del mercado que barrió la ballena (Toxicidad)

# 🎯 ZONA DE ORO (Filtro de Precios)
MIN_PRICE = 0.04           # Ignorar mercados casi muertos (menores a 4 centavos)
MAX_PRICE = 0.96           # Ignorar mercados ya decididos (mayores a 96 centavos)

blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'vs', 'stoppage', 'points', 'rebounds', 
    'assists', 'pts', 'reb', 'ast', 'spread', 'game', 'xrp', 'btc', 
    'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

app = Flask(__name__)
memoria_deltas = {}

# 🚀 OPTIMIZACIÓN: Reutilizar conexiones para mayor velocidad
session = requests.Session()

@app.route('/')
def home():
    return f"🛰️ Radar Institucional 5.1 | Mercados en vigilancia: {len(memoria_deltas)}", 200

def enviar_telegram(mensaje):
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": cid, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": False}
        try: session.post(url, data=payload, timeout=10)
        except: pass

def calcular_datos_mercado(m):
    """ Modificado para extraer Delta, Best Bid, Best Ask y Precio simultáneamente """
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5"]'))
        if len(tokens) < 2: return None
        
        precio_mkt = float(prices[0])
        
        # 🛡️ FILTRO ZONA DE ORO: Si está fuera del rango, ignoramos el mercado
        if precio_mkt < MIN_PRICE or precio_mkt > MAX_PRICE:
            return None
            
        token_yes = tokens[0]
        res_yes = session.get(f"https://clob.polymarket.com/book?token_id={token_yes}", timeout=5).json()
        
        # 1. Extraer puntas de mercado (Best Bid y Best Ask)
        bids = res_yes.get('bids', [])
        asks = res_yes.get('asks', [])
        best_bid = float(bids[0]['price']) if bids else 0.0
        best_ask = float(asks[0]['price']) if asks else 1.0
        
        # 2. Calcular Delta
        dist = precio_mkt * (DEPTH_PERCENT / 100.0)
        piso, techo = precio_mkt - dist, precio_mkt + dist
        b_usd = sum(float(b['price']) * float(b['size']) for b in bids if float(b['price']) >= piso)
        a_usd = sum(float(a['price']) * float(a['size']) for a in asks if float(a['price']) <= techo)
        
        return {
            'delta': int(b_usd - a_usd),
            'best_bid': best_bid,
            'best_ask': best_ask,
            'precio': precio_mkt
        }
    except: return None

def bucle_principal():
    global memoria_deltas
    print("🤖 Modo Institucional 5.1 Online.")
    enviar_telegram("🚀 *Radar Quant Activado:* Filtros de Zona de Oro y Tope de Spread en línea. Cazando ballenas informadas...")

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
                
                if float(data[-1].get('liquidity', 0)) < 10: break
                
                # 🛠️ LA VACUNA 1: Descanso para no ahogar a Render
                time.sleep(0.2) 

            filtrados = [m for m in all_m if not any(w in m.get('question','').lower() for w in blacklist) and float(m.get('liquidity', 0)) >= MIN_LIQUIDITY]
            
            # 🛠️ LA VACUNA 2: 5 trabajadores en vez de 12
            with ThreadPoolExecutor(max_workers=5) as executor:
                resultados = list(executor.map(calcular_datos_mercado, filtrados))

            for i, m in enumerate(filtrados):
                id_m = m['question']
                datos = resultados[i]
                liquidez_m = int(float(m.get('liquidity', 0)))
                
                # Filtramos si hay error, si la liquidez es 0, o si fue rechazado por estar fuera del precio
                if datos is None or liquidez_m == 0: continue
                
                d_actual = datos['delta']
                best_bid = datos['best_bid']
                best_ask = datos['best_ask']
                precio_mkt = datos['precio']
                
                if id_m in memoria_deltas:
                    delta_pasado = memoria_deltas[id_m]['delta']
                    cambio = d_actual - delta_pasado
                    
                    # 🧠 CÁLCULOS INSTITUCIONALES (Microestructura)
                    mid_price = (best_ask + best_bid) / 2.0
                    spread_pct = round(((best_ask - best_bid) / mid_price) * 100, 2) if mid_price > 0 else 0
                    toxicidad_pct = round((abs(cambio) / liquidez_m) * 100, 2)
                    
                    # 🎯 LA CUÁDRUPLE CONDICIÓN ESTRICTA:
                    # 1. Delta superó los $3,000
                    # 2. El Spread NO es un mercado roto (menor o igual a 25%)
                    # 3. Y (Spread de urgencia activo O Toxicidad alta activa)
                    if abs(cambio) >= UMBRAL_ALERTA and spread_pct <= MAX_SPREAD_PCT and (spread_pct >= MIN_SPREAD_PCT or toxicidad_pct >= MIN_TOXICITY_PCT):
                        
                        tipo = "🟢 COMPRA INSTITUCIONAL" if cambio > 0 else "🔴 VENTA INSTITUCIONAL"
                        
                        mensaje_alert = (
                            f"🚨 *ALERTA DE INSIDER FLOW* 🚨\n\n"
                            f"📌 *{id_m}*\n\n"
                            f"💰 *Cambio de Delta:* `${cambio:,} USD`\n"
                            f"🕰️ *Delta Anterior:* `${delta_pasado:,} USD`\n"
                            f"📊 *Delta Actual:* `${d_actual:,} USD`\n"
                            f"💧 *Liquidez:* `${liquidez_m:,} USD`\n"
                            f"⚖️ *Acción:* {tipo}\n\n"
                            f"🧠 *Análisis de Microestructura:*\n"
                            f"🏷️ *Precio Actual:* `${precio_mkt:.3f}`\n"
                            f"📈 *Best Bid / Ask:* `${best_bid:.3f}` / `${best_ask:.3f}`\n"
                            f"🏃‍♂️ *Urgencia (Spread):* `{spread_pct}%`\n"
                            f"🌊 *Toxicidad:* `{toxicidad_pct}%`\n\n"
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
