import requests
import json
import time
import os
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# =========================================================
# 🔑 CREDENCIALES (Configuradas desde Render)
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# =========================================================
# ⚙️ CONFIGURACIÓN DEL RADAR (Ajusta tus niveles aquí)
# =========================================================
UMBRAL_ALERTA = 500      # Te avisa si el Delta cambia más de $5,000 USD
MIN_LIQUIDITY = 100      # Liquidez mínima para vigilar el mercado
INTERVALO_ESC_SEG = 300   # Escaneo cada 5 minutos
DEPTH_PERCENT = 10.0      # Rango de profundidad (0-10%)

# --- 🚫 BLACKLIST TOTAL (Deportes, Cripto y Ruido) ---
blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'stoppage', 'decision',
    'points', 'rebounds', 'assists', 'pts', 'reb', 'ast', 'win', 'spread', 'vs', 'game',
    'xrp', 'btc', 'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

app = Flask(__name__)
memoria_deltas = {} # Aquí el bot guarda los valores anteriores para comparar

@app.route('/')
def home():
    return "🛰️ Bot Centinela de Alvaro Operando... (UptimeRobot activo)", 200

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"❌ Error enviando a Telegram: {e}")

def calcular_delta_mercado(m):
    """ Función para calcular el desequilibrio real fusionando libros de órdenes """
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5"]'))
        if len(tokens) < 2: return None
        
        token_yes, token_no = tokens[0], tokens[1]
        precio_mkt = float(prices[0])
        
        # Peticiones al CLOB (Libro de Órdenes Central)
        res_yes = requests.get(f"https://clob.polymarket.com/book?token_id={token_yes}", timeout=5).json()
        res_no = requests.get(f"https://clob.polymarket.com/book?token_id={token_no}", timeout=5).json()
        
        # Lógica de profundidad (Ancla en el precio +/- 10%)
        distancia = precio_mkt * (DEPTH_PERCENT / 100.0)
        piso, techo = precio_mkt - distancia, precio_mkt + distancia
        
        # Sumar Bids (Compras: YES directo + NO invertido)
        b_usd = sum(float(b['price']) * float(b['size']) for b in res_yes.get('bids', []) if float(b['price']) >= piso)
        b_usd += sum((1.0 - float(a['price'])) * float(a['size']) for a in res_no.get('asks', []) if (1.0 - float(a['price'])) >= piso)
        
        # Sumar Asks (Ventas: YES directo + NO invertido)
        a_usd = sum(float(a['price']) * float(a['size']) for a in res_yes.get('asks', []) if float(a['price']) <= techo)
        a_usd += sum((1.0 - float(b['price'])) * float(b['size']) for b in res_no.get('bids', []) if (1.0 - float(b['price'])) <= techo)
        
        return int(b_usd - a_usd)
    except:
        return None

def bucle_principal():
    global memoria_deltas
    print("🤖 Bot Centinela Iniciado...")
    
    # Mensaje de bienvenida a Telegram
    enviar_telegram("🤖 *¡Bot Centinela de Alvaro en línea!* Vigilando movimientos de ballenas...")

    while True:
        try:
            # 1. Barrido de mercados (Primeros 1500 por liquidez)
            all_m, offset = [], 0
            while offset < 1500:
                url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}&order=liquidity&ascending=false"
                data = requests.get(url, timeout=10).json()
                if not data or len(data) == 0: break
                all_m.extend(data)
                offset += 100
            
            # 2. Filtrado inicial (Blacklist y Liquidez)
            filtrados = []
            for m in all_m:
                txt = f"{m.get('question','')} {m.get('category','')} {m.get('groupItemTitle','')}".lower()
                if any(word in txt for word in blacklist): continue
                if float(m.get('liquidity', 0)) < MIN_LIQUIDITY: continue
                filtrados.append(m)

            # 3. Escaneo Multihilo (Analizando profundidad de todos los filtrados)
            with ThreadPoolExecutor(max_workers=10) as executor:
                resultados = list(executor.map(calcular_delta_mercado, filtrados))

            # 4. Comparación con Memoria y Disparo de Alertas
            for i, m in enumerate(filtrados):
                id_m = m['question']
                d_actual = resultados[i]
                if d_actual is None: continue
                
                if id_m in memoria_deltas:
                    delta_anterior = memoria_deltas[id_m]
                    cambio = d_actual - delta_anterior
                    
                    # Si el cambio supera el umbral, enviamos alerta
                    if abs(cambio) >= UMBRAL_ALERTA:
                        tipo = "🟢 COMPRA MASIVA" if cambio > 0 else "🔴 VENTA/RETIRO"
                        oi = m.get('openInterest') or m.get('open_interest') or 0
                        
                        msg = (f"🚨 *MOVIMIENTO DETECTADO EN {m.get('category', 'Mercado').upper()}*\n\n"
                               f"📌 *{id_m}*\n\n"
                               f"💰 *Cambio de Delta:* `${cambio:,} USD`\n"
                               f"📊 *Delta Actual:* `${d_actual:,} USD`\n"
                               f"🎟️ *Open Interest:* `${int(float(oi)):,}`\n"
                               f"⚖️ *Acción:* {tipo}\n\n"
                               f"🔗 [Ver en Polymarket](https://polymarket.com/event/{m['slug']})")
                        enviar_telegram(msg)
                
                # Actualizar la memoria con el valor actual
                memoria_deltas[id_m] = d_actual

            print(f"✅ Ciclo completado. {len(filtrados)} mercados monitoreados.")
            time.sleep(INTERVALO_ESC_SEG)
            
        except Exception as e:
            print(f"❌ Error en el bucle: {e}")
            time.sleep(60)

if __name__ == "__main__":
    # Iniciar el proceso de escaneo en un hilo separado para no bloquear a Flask
    threading.Thread(target=bucle_principal, daemon=True).start()
    
    # Iniciar servidor web para Render/UptimeRobot
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

