import requests
import json
import time
import os
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# =========================================================
# 🔑 CREDENCIALES
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# =========================================================
# ⚙️ CONFIGURACIÓN OPTIMIZADA
# =========================================================
UMBRAL_ALERTA = 500       # Sensibilidad alta para detectar ballenas
MIN_LIQUIDITY = 100       # Filtro mínimo para no procesar basura
INTERVALO_ESC_SEG = 300   # 5 minutos entre barridos totales
DEPTH_PERCENT = 10.0      # Rango de profundidad 
MAX_WORKERS = 20          # Aumentamos a 20 hilos para velocidad máxima en Render

# --- 🚫 BLACKLIST REFORZADA ---
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
    return f"🛰️ Centinela Total de Alvaro: {len(memoria_deltas)} mercados en memoria.", 200

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try: requests.post(url, data=payload, timeout=10)
    except: print("❌ Error Telegram")

def calcular_delta_mercado(m):
    """ El núcleo del análisis: Libros de órdenes YES + NO """
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5"]'))
        if len(tokens) < 2: return None
        
        token_yes, token_no = tokens[0], tokens[1]
        precio_mkt = float(prices[0])
        
        # Consultas paralelas al CLOB
        res_yes = requests.get(f"https://clob.polymarket.com/book?token_id={token_yes}", timeout=5).json()
        res_no = requests.get(f"https://clob.polymarket.com/book?token_id={token_no}", timeout=5).json()
        
        dist = precio_mkt * (DEPTH_PERCENT / 100.0)
        piso, techo = precio_mkt - dist, precio_mkt + dist
        
        # Cálculo de imbalance (Delta)
        b_usd = sum(float(b['price']) * float(b['size']) for b in res_yes.get('bids', []) if float(b['price']) >= piso)
        b_usd += sum((1.0 - float(a['price'])) * float(a['size']) for a in res_no.get('asks', []) if (1.0 - float(a['price'])) >= piso)
        
        a_usd = sum(float(a['price']) * float(a['size']) for a in res_yes.get('asks', []) if float(a['price']) <= techo)
        a_usd += sum((1.0 - float(b['price'])) * float(b['size']) for b in res_no.get('bids', []) if (1.0 - float(b['price'])) <= techo)
        
        return int(b_usd - a_usd)
    except: return None

def bucle_principal():
    global memoria_deltas
    print("🚀 Iniciando Centinela en MODO TOTAL...")
    enviar_telegram("⚡ *Modo Escaneo Total Activado:* Revisando hasta el último rincón de Polymarket.")

    while True:
        try:
            # 1. BARRIDO INFINITO (Paginación completa)
            all_m, offset = [], 0
            print(f"📦 Descargando base de datos completa...")
            while True:
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                data = requests.get(url, timeout=10).json()
                if not data or len(data) == 0: break
                all_m.extend(data)
                offset += 100
                if offset > 5000: break # Seguro de vida por si la API falla

            # 2. FILTRADO RÁPIDO (Eliminar ruido antes de procesar libros)
            filtrados = []
            for m in all_m:
                txt = f"{m.get('question','')} {m.get('category','')} {m.get('groupItemTitle','')}".lower()
                if any(word in txt for word in blacklist): continue
                if float(m.get('liquidity', 0)) < MIN_LIQUIDITY: continue
                filtrados.append(m)

            print(f"🎯 {len(all_m)} mercados encontrados. {len(filtrados)} pasan a análisis de profundidad.")

            # 3. ESCANEO MULTIHILO (Máxima potencia)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                resultados = list(executor.map(calcular_delta_mercado, filtrados))

            # 4. DETECCIÓN DE CAMBIOS
            for i, m in enumerate(filtrados):
                id_m = m['question']
                d_actual = resultados[i]
                if d_actual is None: continue
                
                if id_m in memoria_deltas:
                    cambio = d_actual - memoria_deltas[id_m]
                    if abs(cambio) >= UMBRAL_ALERTA:
                        tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                        enviar_telegram(f"🚨 *BALLENA DETECTADA*\n\n📌 *{id_m}*\n\n💰 *Variación:* `${cambio:,} USD`\n⚖️ *Delta:* `${d_actual:,} USD`\n🔗 [Link]({m['slug']})")
                
                memoria_deltas[id_m] = d_actual

            print(f"✅ Ciclo Total Completado: {datetime.now().strftime('%H:%M:%S')}")
            time.sleep(INTERVALO_ESC_SEG)
            
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
