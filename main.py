import requests
import json
import time
import os
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify

# =========================================================
# 🔑 CREDENCIALES (configurar en Render → Environment)
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_IDS_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# 🎚️ FILTROS — CAMBIA ESTOS VALORES A TU GUSTO
# =========================================================
UMBRAL_BALLENA = 9000       # Cambio de delta ≥ esto → alerta directa
UMBRAL_INSIDER = 900        # Cambio de delta ≥ esto → alerta si pasa spread/tox
MIN_LIQUIDITY = 900         # Liquidez mínima del mercado
MIN_PRICE = 0.04            # Precio mínimo YES (Zona de Oro)
MAX_PRICE = 0.96            # Precio máximo YES (Zona de Oro)
MIN_SPREAD_PCT = 3.0        # Spread mínimo para Vía Insider
MAX_SPREAD_PCT = 30.0       # Spread máximo (mercado roto si supera)
MIN_TOXICITY_PCT = 5.0      # Toxicidad mínima para Vía Insider
DEPTH_RANGE = 0.10          # Profundidad del libro (10 centavos)
INTERVALO_SEG = 300         # Segundos entre escaneos

# ⚡ RENDIMIENTO
MAX_WORKERS = 2
BATCH_SIZE = 10
PAUSA_ENTRE_BATCH = 1.5
MAX_MERCADOS_OFFSET = 10000

# =========================================================
# 🚫 BLACKLIST
# =========================================================
blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'vs', 'stoppage', 'points', 'rebounds',
    'assists', 'pts', 'reb', 'ast', 'spread', 'game', 'xrp', 'btc',
    'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

# =========================================================
# 🧠 MEMORIA
# =========================================================
MEMORIA_FILE = "/tmp/memoria_deltas.json"

def guardar_memoria(memoria):
    try:
        with open(MEMORIA_FILE, 'w') as f:
            json.dump(memoria, f)
    except:
        pass

def cargar_memoria():
    try:
        if os.path.exists(MEMORIA_FILE):
            with open(MEMORIA_FILE, 'r') as f:
                data = json.load(f)
                print(f"🧠 Memoria recuperada: {len(data)} mercados", flush=True)
                return data
    except:
        pass
    print("🧠 Memoria nueva", flush=True)
    return {}

# =========================================================
# 📊 STATS PERSISTENTES (compartidos entre gunicorn threads)
# =========================================================
STATS_FILE = "/tmp/bot_stats.json"

def guardar_stats(s):
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(s, f)
    except:
        pass

def leer_stats():
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return None

# Stats en memoria del thread del bot
stats = {
    'estado': '⏳ Arrancando...',
    'ciclos': 0,
    'alertas_total': 0,
    'alertas_ultimo_ciclo': 0,
    'mercados_vigilados': 0,
    'mercados_total_gamma': 0,
    'mercados_post_filtro': 0,
    'libros_ok': 0,
    'errores_clob': 0,
    'ultimo_ciclo': 'N/A',
    'duracion_ciclo': 0,
    'ultimo_error': 'Ninguno',
    'arranque': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
    'errores_log': []  # Últimos 10 errores con timestamp
}

# =========================================================
# 🔧 FLASK
# =========================================================
app = Flask(__name__)

@app.route('/')
def home():
    s = leer_stats()
    if s is None:
        return "⏳ Bot arrancando, espera unos minutos...", 200

    # Página de status bonita y clara
    errores_txt = ""
    if s.get('errores_log'):
        errores_txt = "\n\n🔴 ÚLTIMOS ERRORES:\n"
        for err in s['errores_log'][-5:]:
            errores_txt += f"  [{err['time']}] {err['msg']}\n"

    return (
        f"🛰️ RADAR 5.5 — STATUS\n"
        f"{'='*40}\n\n"
        f"🟢 Estado: {s['estado']}\n"
        f"🕐 Arrancó: {s['arranque']}\n"
        f"🕐 Último ciclo: {s['ultimo_ciclo']}\n\n"
        f"📊 CICLOS\n"
        f"  Total: {s['ciclos']}\n"
        f"  Duración último: {s['duracion_ciclo']}s\n\n"
        f"📡 MERCADOS\n"
        f"  Gamma API: {s['mercados_total_gamma']}\n"
        f"  Post-filtro: {s['mercados_post_filtro']}\n"
        f"  Libros OK: {s['libros_ok']}\n"
        f"  En memoria: {s['mercados_vigilados']}\n\n"
        f"🚨 ALERTAS\n"
        f"  Total enviadas: {s['alertas_total']}\n"
        f"  Último ciclo: {s['alertas_ultimo_ciclo']}\n\n"
        f"❌ ERRORES\n"
        f"  CLOB (rate limit/timeout): {s['errores_clob']}\n"
        f"  Último error: {s['ultimo_error']}\n"
        f"{errores_txt}\n"
        f"🎚️ FILTROS ACTIVOS\n"
        f"  🐋 Ballena: ≥${UMBRAL_BALLENA:,}\n"
        f"  🥷 Insider: ≥${UMBRAL_INSIDER:,} + Spr≥{MIN_SPREAD_PCT}% ó Tox≥{MIN_TOXICITY_PCT}%\n"
        f"  💧 Liquidez mín: ${MIN_LIQUIDITY:,}\n"
        f"  🏷️ Precio: ${MIN_PRICE}-${MAX_PRICE}\n"
        f"  🏃 Spread máx: {MAX_SPREAD_PCT}%\n"
    ), 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/health')
def health():
    return "OK", 200

@app.route('/stats')
def stats_json():
    s = leer_stats()
    if s:
        return jsonify(s)
    return jsonify({'estado': 'arrancando'}), 200

# =========================================================
# 🔧 HTTP
# =========================================================
session_local = threading.local()

def get_session():
    if not hasattr(session_local, 'session'):
        s = requests.Session()
        s.headers.update({'Accept': 'application/json'})
        session_local.session = s
    return session_local.session

main_session = requests.Session()
main_session.headers.update({'Accept': 'application/json'})


def log_error(msg):
    """Registra un error en el log interno (últimos 10)."""
    stats['errores_log'].append({
        'time': datetime.utcnow().strftime('%H:%M:%S'),
        'msg': str(msg)[:100]
    })
    if len(stats['errores_log']) > 10:
        stats['errores_log'] = stats['errores_log'][-10:]
    stats['ultimo_error'] = str(msg)[:100]


def enviar_telegram(mensaje):
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        try:
            resp = main_session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=10
            )
            if resp.status_code != 200:
                log_error(f"Telegram {resp.status_code}: {resp.text[:50]}")
        except Exception as e:
            log_error(f"Telegram: {e}")


def leer_libro(token_id):
    s = get_session()
    try:
        resp = s.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=8)
        if resp.status_code == 429:
            time.sleep(3)
            return None, None
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        return data.get('bids', []), data.get('asks', [])
    except:
        return None, None


def calcular_spread_real(bids, asks, precio_mkt):
    RANGO_CERCANO = 0.20

    best_bid = None
    for b in bids:
        p = float(b['price'])
        if abs(p - precio_mkt) <= RANGO_CERCANO:
            if best_bid is None or p > best_bid:
                best_bid = p

    best_ask = None
    for a in asks:
        p = float(a['price'])
        if abs(p - precio_mkt) <= RANGO_CERCANO:
            if best_ask is None or p < best_ask:
                best_ask = p

    if best_bid is None or best_ask is None:
        return None, None, None
    if best_bid >= best_ask:
        return None, None, None

    mid = (best_ask + best_bid) / 2.0
    if mid <= 0:
        return None, None, None

    spread = round(((best_ask - best_bid) / mid) * 100, 2)
    return spread, best_bid, best_ask


def analizar_mercado(m):
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5","0.5"]'))
        if len(tokens) < 2 or len(prices) < 2:
            return None

        precio_yes = float(prices[0])
        if precio_yes < MIN_PRICE or precio_yes > MAX_PRICE:
            return None

        precio_no = float(prices[1])

        bids_yes, asks_yes = leer_libro(tokens[0])
        if bids_yes is None:
            stats['errores_clob'] += 1
            return None

        time.sleep(0.1)

        bids_no, asks_no = leer_libro(tokens[1])
        if bids_no is None:
            stats['errores_clob'] += 1
            return None

        spread, best_bid, best_ask = calcular_spread_real(bids_yes, asks_yes, precio_yes)
        if spread is None:
            return None

        piso_y = max(0, precio_yes - DEPTH_RANGE)
        techo_y = min(1, precio_yes + DEPTH_RANGE)
        bid_usd_yes = sum(float(b['price']) * float(b['size']) for b in bids_yes if float(b['price']) >= piso_y)
        ask_usd_yes = sum(float(a['price']) * float(a['size']) for a in asks_yes if float(a['price']) <= techo_y)

        piso_n = max(0, precio_no - DEPTH_RANGE)
        techo_n = min(1, precio_no + DEPTH_RANGE)
        bid_usd_no = sum(float(b['price']) * float(b['size']) for b in bids_no if float(b['price']) >= piso_n)
        ask_usd_no = sum(float(a['price']) * float(a['size']) for a in asks_no if float(a['price']) <= techo_n)

        delta_yes = bid_usd_yes - ask_usd_yes
        delta_no = bid_usd_no - ask_usd_no
        delta_total = int(delta_yes - delta_no)

        return {
            'delta': delta_total,
            'best_bid': best_bid,
            'best_ask': best_ask,
            'spread': spread,
            'precio': precio_yes,
            'delta_yes': int(delta_yes),
            'delta_no': int(delta_no)
        }
    except Exception as e:
        stats['errores_clob'] += 1
        return None


def analizar_batch(batch):
    resultados = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futuros = {executor.submit(analizar_mercado, m): m for m in batch}
        for futuro in as_completed(futuros):
            m = futuros[futuro]
            try:
                dato = futuro.result()
            except:
                dato = None
            resultados.append((m, dato))
    return resultados


def obtener_todos_mercados():
    all_markets = []
    offset = 0
    while offset < MAX_MERCADOS_OFFSET:
        try:
            data = main_session.get(
                f"https://gamma-api.polymarket.com/markets?"
                f"active=true&closed=false&limit=100&offset={offset}"
                f"&order=liquidity&ascending=false",
                timeout=15
            ).json()
        except Exception as e:
            log_error(f"Gamma API offset {offset}: {e}")
            break
        if not data:
            break
        all_markets.extend(data)
        offset += 100
        time.sleep(0.05)
    return all_markets


def bucle_principal():
    print("🤖 Radar 5.5: Bot iniciado", flush=True)
    memoria = cargar_memoria()
    stats['estado'] = '✅ Activo'
    guardar_stats(stats)

    enviar_telegram(
        f"⚡ *Radar 5.5 Online*\n\n"
        f"🐋 Ballena: ≥${UMBRAL_BALLENA:,} | 🥷 Insider: ≥${UMBRAL_INSIDER:,}\n"
        f"💧 Liq: ${MIN_LIQUIDITY:,} | 🏷️ Precio: ${MIN_PRICE}-${MAX_PRICE}\n"
        f"🏃 Spread: {MIN_SPREAD_PCT}%-{MAX_SPREAD_PCT}% | 🌊 Tox: ≥{MIN_TOXICITY_PCT}%\n"
        f"🧠 Memoria: {len(memoria)}"
    )

    while True:
        try:
            inicio = time.time()
            stats['estado'] = '🔄 Escaneando...'
            guardar_stats(stats)

            all_markets = obtener_todos_mercados()
            stats['mercados_total_gamma'] = len(all_markets)

            mercados = [
                m for m in all_markets
                if float(m.get('liquidity', 0)) >= MIN_LIQUIDITY
                and not any(w in m.get('question', '').lower() for w in blacklist)
            ]
            stats['mercados_post_filtro'] = len(mercados)

            print(f"📡 Total: {len(all_markets)} | Filtrados: {len(mercados)}", flush=True)

            alertas_ciclo = 0
            ok_count = 0

            for batch_start in range(0, len(mercados), BATCH_SIZE):
                batch = mercados[batch_start:batch_start + BATCH_SIZE]
                resultados_batch = analizar_batch(batch)

                for m, datos in resultados_batch:
                    nombre = m['question']
                    liquidez = int(float(m.get('liquidity', 0)))

                    if datos is None:
                        continue

                    ok_count += 1
                    d_actual = datos['delta']
                    spread = datos['spread']

                    if nombre in memoria:
                        d_pasado = memoria[nombre]['delta']
                        cambio = d_actual - d_pasado

                        if cambio == 0:
                            memoria[nombre] = {'delta': d_actual}
                            continue

                        tox = round((abs(cambio) / liquidez) * 100, 2) if liquidez > 0 else 0

                        # ═══ ALERTAS ═══
                        if spread > MAX_SPREAD_PCT:
                            memoria[nombre] = {'delta': d_actual}
                            continue

                        es_ballena = abs(cambio) >= UMBRAL_BALLENA
                        es_insider = (
                            abs(cambio) >= UMBRAL_INSIDER
                            and (spread >= MIN_SPREAD_PCT or tox >= MIN_TOXICITY_PCT)
                        )

                        if es_ballena or es_insider:
                            tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                            etiqueta = "🐋 BALLENA" if es_ballena else "🥷 INSIDER"

                            if es_ballena:
                                razon = f"Delta ≥ ${UMBRAL_BALLENA:,}"
                            else:
                                razones = []
                                if spread >= MIN_SPREAD_PCT:
                                    razones.append(f"Spr {spread}%≥{MIN_SPREAD_PCT}%")
                                if tox >= MIN_TOXICITY_PCT:
                                    razones.append(f"Tox {tox}%≥{MIN_TOXICITY_PCT}%")
                                razon = " + ".join(razones)

                            mensaje = (
                                f"🚨 *ALERTA {etiqueta}* 🚨\n\n"
                                f"📌 *{nombre}*\n\n"
                                f"💰 *Cambio:* `${cambio:,}`\n"
                                f"🕰️ *Delta:* `${d_pasado:,}` → `${d_actual:,}`\n"
                                f"   ├ YES: `${datos['delta_yes']:,}` | NO: `${datos['delta_no']:,}`\n"
                                f"💧 *Liquidez:* `${liquidez:,}`\n"
                                f"⚖️ *Acción:* {tipo}\n\n"
                                f"🏷️ Precio: `${datos['precio']:.3f}`\n"
                                f"📈 Bid/Ask: `${datos['best_bid']:.3f}` / `${datos['best_ask']:.3f}`\n"
                                f"🏃 Spread: `{spread}%` | 🌊 Tox: `{tox}%`\n"
                                f"✅ *Razón:* {razon}\n\n"
                                f"🔗 [Ver en Polymarket](https://polymarket.com/event/{m.get('slug', '')})"
                            )
                            enviar_telegram(mensaje)
                            alertas_ciclo += 1
                            stats['alertas_total'] += 1

                    memoria[nombre] = {'delta': d_actual}

                time.sleep(PAUSA_ENTRE_BATCH)

            guardar_memoria(memoria)

            # Actualizar stats
            stats['ciclos'] += 1
            stats['alertas_ultimo_ciclo'] = alertas_ciclo
            stats['mercados_vigilados'] = len(memoria)
            stats['libros_ok'] = ok_count
            duracion = round(time.time() - inicio, 1)
            stats['duracion_ciclo'] = duracion
            stats['ultimo_ciclo'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
            stats['estado'] = f'✅ Activo'
            guardar_stats(stats)

            print(f"✅ Ciclo #{stats['ciclos']} ({duracion}s) | OK: {ok_count}/{len(mercados)} | Alertas: {alertas_ciclo}", flush=True)

            time.sleep(INTERVALO_SEG)

        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:80]}"
            print(f"❌ Error: {error_msg}", flush=True)
            import traceback
            traceback.print_exc()
            log_error(error_msg)
            stats['estado'] = f'⚠️ Error (reintentando...)'
            guardar_stats(stats)
            time.sleep(60)


# =========================================================
# 🚀 ARRANQUE
# =========================================================
bot_iniciado = False
bot_lock = threading.Lock()

def iniciar_bot():
    global bot_iniciado
    with bot_lock:
        if not bot_iniciado:
            bot_iniciado = True
            t = threading.Thread(target=bucle_principal, daemon=True)
            t.start()
            print("🚀 Bot thread lanzado", flush=True)

iniciar_bot()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
