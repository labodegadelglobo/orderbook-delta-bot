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
# En Render pon: 338647966, -5136216182
CHAT_IDS_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# ⚙️ CONFIGURACIÓN "MODO TITÁN"
# =========================================================
UMBRAL_ALERTA = 1000       # Sensibilidad +/- $500 USD
MIN_LIQUIDITY = 500       # Mínimo $100 USD de liquidez real
INTERVALO_ESC_SEG = 300   # Escaneo cada 5 minutos
DEPTH_PERCENT = 10.0      # Rango de profundidad (10%)

# --- 🚫 BLACKLIST (Filtro de ruido) ---
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
    return f"🛰️ Radar 10k Online. Usuarios: {len(CHAT_IDS_RAW.split(','))}. Mercados: {len(memoria_deltas)}", 200

def enviar_telegram(mensaje):
    """ Envía el mensaje a cada ID en la lista de Render """
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": cid, "text": mensaje, "parse_mode": "Markdown"}
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code == 200:
                print(f"✅ Enviado a: {cid}")
            else:
                print(f"❌ Error en {cid}: {r.text}")
        except:
            print(f"❌ Fallo de conexión con {cid}")

def calcular_delta_mercado(m):
    """ Análisis profundo de libros de órdenes YES/NO """
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
        
        # Bids (Compras)
        b_usd = sum(float(b['price']) * float(b['size']) for b in res_yes.get('bids', []) if float(b['price']) >= piso)
        b_usd += sum((1.0 - float(a['price'])) * float(a['size']) for a in res_no.get('asks', []) if (1.0 - float(a['price'])) >= piso)
        
        # Asks (Ventas)
        a_usd = sum(float(a['price']) * float(a['size']) for a in res_yes.get('asks', []) if float(a['price']) <= techo)
        a_usd += sum((1.0 - float(b['price'])) * float(b['size']) for b in res_no.get('bids', []) if (1.0 - float(a['price'])) <= techo)
        
        return int(b_usd - a_usd)
    except:
        return None

def bucle_principal():
    global memoria_deltas
    print("🤖 Centinela Multi-ID Iniciado...")
    enviar_telegram("🚀 *Radar Titán Multi-Usuario:* Conexión exitosa con el equipo.")

    while True:
        try:
            # 1. BARRIDO MASIVO (Hasta 10,000)
            all_m, offset = [], 0
            while offset < 10000:
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                data = requests.get(url, timeout=10).json()
                if not data or len(data) == 0: break
                all_m.extend(data)
                offset += 100
                if float(data[-1].get('liquidity', 0)) < 20: break

            # 2. FILTRADO
            filtrados = [m for m in all_m if not any(w in m.get('question','').lower() for w in blacklist) and float(m.get('liquidity', 0)) >= MIN_LIQUIDITY]

            # 3. ESCANEO MULTIHILO (Potencia dinámica)
            with ThreadPoolExecutor() as executor:
                resultados = list(executor.map(calcular_delta_mercado, filtrados))

            # 4. DETECCIÓN Y TIEMPO
            ahora = time.time()
            for i, m in enumerate(filtrados):
                id_m = m['question']
                d_actual = resultados[i]
                if d_actual is None: continue
                
                if id_m in memoria_deltas:
                    info_v = memoria_deltas[id_m]
                    cambio = d_actual - info_v['delta']
                    min_seg = int((ahora - info_v['first_seen']) / 60)
                    
                    if abs(cambio) >= UMBRAL_ALERTA:
                        tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                        emoji = "🐋" if abs(cambio) > 5000 else "🐟"
                        t_txt = f"{min_seg} min" if min_seg < 60 else f"{min_seg//60}h {min_seg%60}min"

                        enviar_telegram(
                            f"{emoji} *MOVIMIENTO DETECTADO*\n\n"
                            f"📌 *{id_m}*\n\n"
                            f"💰 *Variación:* `${cambio:,} USD`\n"
                            f"⚖️ *Delta:* `${d_actual:,} USD`\n"
                            f"⚖️ *Acción:* {tipo}\n"
                            f"⏱️ *Monitoreado:* `{t_txt}`\n"
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
    # El puerto 10000 es el estándar de Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
