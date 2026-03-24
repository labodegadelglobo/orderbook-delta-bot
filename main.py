import requests
import json
import time
import os
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask

# =========================================================
# 🔑 CREDENCIALES (configurar en Render → Environment)
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_IDS_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# 🎚️ FILTROS
# =========================================================
UMBRAL_BALLENA = 5000
UMBRAL_INSIDER = 500
MIN_LIQUIDITY = 500
MIN_PRICE = 0.04
MAX_PRICE = 0.96
MIN_SPREAD_PCT = 2.0
MAX_SPREAD_PCT = 30.0
MIN_TOXICITY_PCT = 3.0
DEPTH_RANGE = 0.10
INTERVALO_SEG = 300

# ⚡ RENDIMIENTO
MAX_WORKERS = 3             # Workers simultáneos para leer orderbooks
BATCH_SIZE = 15             # Mercados por batch antes de pausar
PAUSA_ENTRE_BATCH = 1.0    # Segundos de pausa entre batches (anti rate-limit)
MAX_MERCADOS_OFFSET = 10000 # Hasta dónde buscar mercados en Gamma API

# 🔬 DIAGNÓSTICO
MODO_DIAGNOSTICO = True
TOP_N_DIAGNOSTICO = 5

blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'vs', 'stoppage', 'points', 'rebounds',
    'assists', 'pts', 'reb', 'ast', 'spread', 'game', 'xrp', 'btc',
    'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

# =========================================================
# 🧠 MEMORIA PERSISTENTE
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
                print(f"🧠 Memoria recuperada: {len(data)} mercados")
                return data
    except:
        pass
    print("🧠 Memoria nueva")
    return {}

# =========================================================
# 🔧 SISTEMA
# =========================================================
app = Flask(__name__)
session_local = threading.local()

def get_session():
    """Cada thread tiene su propia sesión HTTP (evita conflictos)."""
    if not hasattr(session_local, 'session'):
        s = requests.Session()
        s.headers.update({'Accept': 'application/json'})
        session_local.session = s
    return session_local.session

# Sesión principal para Telegram y Gamma API
main_session = requests.Session()
main_session.headers.update({'Accept': 'application/json'})

stats = {'ciclos': 0, 'alertas': 0, 'errores': 0, 'ultimo': 'Iniciando...', 'mercados': 0}

@app.route('/')
def home():
    return (
        f"🛰️ Radar 5.5 | Mercados: {stats['mercados']} | "
        f"Ciclos: {stats['ciclos']} | Alertas: {stats['alertas']} | "
        f"Errores CLOB: {stats['errores']} | Último: {stats['ultimo']}"
    ), 200


def enviar_telegram(mensaje):
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        try:
            main_session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=10
            )
        except:
            pass


def leer_libro(token_id):
    """Lee orderbook usando sesión del thread actual."""
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


def analizar_mercado(m):
    """Analiza un mercado leyendo libros YES y NO. Thread-safe."""
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
            stats['errores'] += 1
            return None

        time.sleep(0.1)

        bids_no, asks_no = leer_libro(tokens[1])
        if bids_no is None:
            stats['errores'] += 1
            return None

        # Delta YES
        piso_y = max(0, precio_yes - DEPTH_RANGE)
        techo_y = min(1, precio_yes + DEPTH_RANGE)
        bid_usd_yes = sum(float(b['price']) * float(b['size']) for b in bids_yes if float(b['price']) >= piso_y)
        ask_usd_yes = sum(float(a['price']) * float(a['size']) for a in asks_yes if float(a['price']) <= techo_y)

        # Delta NO
        piso_n = max(0, precio_no - DEPTH_RANGE)
        techo_n = min(1, precio_no + DEPTH_RANGE)
        bid_usd_no = sum(float(b['price']) * float(b['size']) for b in bids_no if float(b['price']) >= piso_n)
        ask_usd_no = sum(float(a['price']) * float(a['size']) for a in asks_no if float(a['price']) <= techo_n)

        delta_yes = bid_usd_yes - ask_usd_yes
        delta_no = bid_usd_no - ask_usd_no
        delta_total = int(delta_yes - delta_no)

        best_bid = float(bids_yes[0]['price']) if bids_yes else 0.0
        best_ask = float(asks_yes[0]['price']) if asks_yes else 1.0

        return {
            'delta': delta_total,
            'best_bid': best_bid,
            'best_ask': best_ask,
            'precio': precio_yes,
            'delta_yes': int(delta_yes),
            'delta_no': int(delta_no)
        }
    except:
        stats['errores'] += 1
        return None


def analizar_batch(batch):
    """Procesa un batch de mercados con workers en paralelo."""
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
    """Obtiene todos los mercados activos de Gamma API."""
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
        except:
            break
        if not data:
            break
        all_markets.extend(data)
        offset += 100
        time.sleep(0.05)
    return all_markets


def bucle_principal():
    memoria = cargar_memoria()

    print("🤖 Radar 5.5 arrancando...")
    modo_txt = "🔬 DIAGNÓSTICO" if MODO_DIAGNOSTICO else "🎯 Producción"
    enviar_telegram(
        f"⚡ *Radar 5.5 Online* — {modo_txt}\n\n"
        f"🐋 Ballena: ≥${UMBRAL_BALLENA:,} | 🥷 Insider: ≥${UMBRAL_INSIDER:,}\n"
        f"💧 Liq mín: ${MIN_LIQUIDITY:,} | 🏷️ Precio: ${MIN_PRICE}-${MAX_PRICE}\n"
        f"🏃 Spread: {MIN_SPREAD_PCT}%-{MAX_SPREAD_PCT}% | 🌊 Tox: ≥{MIN_TOXICITY_PCT}%\n"
        f"⚡ Workers: {MAX_WORKERS} | Batch: {BATCH_SIZE} | Offset: {MAX_MERCADOS_OFFSET}\n"
        f"🧠 Memoria: {len(memoria)} mercados"
    )

    while True:
        try:
            inicio = time.time()

            # ── Paso 1: Obtener TODOS los mercados ──
            all_markets = obtener_todos_mercados()

            # ── Paso 2: Filtrar ──
            mercados = [
                m for m in all_markets
                if float(m.get('liquidity', 0)) >= MIN_LIQUIDITY
                and not any(w in m.get('question', '').lower() for w in blacklist)
            ]

            print(f"📡 Total Gamma: {len(all_markets)} | Post-filtro: {len(mercados)}")

            # ── Paso 3: Procesar en batches ──
            alertas_ciclo = 0
            ok_count = 0
            todos_cambios = []

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

                    if nombre in memoria:
                        d_pasado = memoria[nombre]['delta']
                        cambio = d_actual - d_pasado

                        if cambio == 0:
                            memoria[nombre] = {'delta': d_actual}
                            continue

                        mid = (datos['best_ask'] + datos['best_bid']) / 2.0
                        spread = round(((datos['best_ask'] - datos['best_bid']) / mid) * 100, 2) if mid > 0 else 0
                        tox = round((abs(cambio) / liquidez) * 100, 2) if liquidez > 0 else 0

                        # Diagnóstico
                        if MODO_DIAGNOSTICO:
                            entry = {
                                'nombre': nombre[:60],
                                'cambio': cambio,
                                'abs_cambio': abs(cambio),
                                'spread': spread,
                                'tox': tox,
                                'liquidez': liquidez,
                                'precio': datos['precio'],
                                'bloqueado_por': []
                            }
                            if spread > MAX_SPREAD_PCT:
                                entry['bloqueado_por'].append(f"Spread {spread}%>{MAX_SPREAD_PCT}%")
                            elif abs(cambio) >= UMBRAL_BALLENA:
                                pass
                            elif abs(cambio) >= UMBRAL_INSIDER:
                                if spread < MIN_SPREAD_PCT and tox < MIN_TOXICITY_PCT:
                                    entry['bloqueado_por'].append(f"Spr {spread}%<{MIN_SPREAD_PCT}% Y Tox {tox}%<{MIN_TOXICITY_PCT}%")
                            else:
                                entry['bloqueado_por'].append(f"Delta ${abs(cambio)}<${UMBRAL_INSIDER}")
                            todos_cambios.append(entry)

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
                            stats['alertas'] += 1

                    memoria[nombre] = {'delta': d_actual}

                # Pausa entre batches
                time.sleep(PAUSA_ENTRE_BATCH)

            # ── Guardar memoria ──
            guardar_memoria(memoria)

            # ── Stats ──
            stats['ciclos'] += 1
            stats['mercados'] = len(memoria)
            duracion = round(time.time() - inicio, 1)
            stats['ultimo'] = datetime.now().strftime('%H:%M:%S')

            print(
                f"✅ Ciclo #{stats['ciclos']} ({duracion}s) | "
                f"Libros OK: {ok_count}/{len(mercados)} | Alertas: {alertas_ciclo}"
            )

            # ── Diagnóstico ──
            if MODO_DIAGNOSTICO and todos_cambios and stats['ciclos'] >= 2:
                todos_cambios.sort(key=lambda x: x['abs_cambio'], reverse=True)
                top = todos_cambios[:TOP_N_DIAGNOSTICO]

                lineas = [f"🔬 *DIAG #{stats['ciclos']}* ({duracion}s)\n"]
                lineas.append(f"📡 OK: {ok_count}/{len(mercados)} | Mov: {len(todos_cambios)}\n")

                for j, t in enumerate(top, 1):
                    d = "🟢" if t['cambio'] > 0 else "🔴"
                    bloq = " | ".join(t['bloqueado_por']) if t['bloqueado_por'] else "✅ PASÓ"
                    lineas.append(
                        f"\n*{j}. {t['nombre']}*\n"
                        f"   {d} `${t['cambio']:,}` | Liq: `${t['liquidez']:,}`\n"
                        f"   Spr: `{t['spread']}%` | Tox: `{t['tox']}%`\n"
                        f"   → _{bloq}_"
                    )

                total_bloq = sum(1 for t in todos_cambios if t['bloqueado_por'])
                total_delta = sum(1 for t in todos_cambios if any('Delta' in b for b in t['bloqueado_por']))
                total_spr = sum(1 for t in todos_cambios if any('>' in b for b in t['bloqueado_por']))
                total_micro = sum(1 for t in todos_cambios if any('Y Tox' in b for b in t['bloqueado_por']))

                lineas.append(
                    f"\n\n📈 *Filtros:*\n"
                    f"Total: {len(todos_cambios)} | Bloq: {total_bloq}\n"
                    f"├ Delta bajo: {total_delta}\n"
                    f"├ Spread roto: {total_spr}\n"
                    f"└ Sin urgencia: {total_micro}\n"
                    f"✅ Pasaron: {len(todos_cambios) - total_bloq}"
                )

                enviar_telegram("\n".join(lineas))

            if stats['ciclos'] % 12 == 0:
                enviar_telegram(
                    f"📊 *Reporte #{stats['ciclos']}*\n"
                    f"👁️ Mercados: {len(memoria)}\n"
                    f"📡 OK: {ok_count}/{len(mercados)}\n"
                    f"🚨 Alertas: {stats['alertas']}\n"
                    f"❌ Errores: {stats['errores']}\n"
                    f"⏱️ Ciclo: {duracion}s"
                )

            time.sleep(INTERVALO_SEG)

        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
