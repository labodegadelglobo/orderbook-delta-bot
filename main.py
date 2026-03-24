import requests
import json
import time
import os
import threading
from datetime import datetime
from flask import Flask

# =========================================================
# 🔑 CREDENCIALES (configurar en Render → Environment)
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_IDS_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# 🎚️ FILTROS — AJUSTA ESTOS VALORES A TU GUSTO
# =========================================================

# --- FILTRO 1: DELTA (cambio en el orderbook en 5 minutos) ---
UMBRAL_BALLENA = 5000       # Vía Ballena: si el delta cambia $5,000+ → alerta directa
UMBRAL_INSIDER = 500        # Vía Insider: si el delta cambia $500+ → alerta SI pasa filtros extra

# --- FILTRO 2: LIQUIDEZ MÍNIMA ---
MIN_LIQUIDITY = 500         # Ignorar mercados con menos de $500 de liquidez

# --- FILTRO 3: PRECIO (Zona de Oro) ---
MIN_PRICE = 0.04            # Ignorar mercados con precio YES menor a 4 centavos
MAX_PRICE = 0.96            # Ignorar mercados con precio YES mayor a 96 centavos

# --- FILTRO 4: SPREAD ---
MIN_SPREAD_PCT = 2.0        # Vía Insider requiere spread ≥ 2% (hay urgencia)
MAX_SPREAD_PCT = 30.0       # Si spread > 30% → mercado roto, ignorar siempre

# --- FILTRO 5: TOXICIDAD ---
MIN_TOXICITY_PCT = 3.0      # Vía Insider requiere toxicidad ≥ 3% (hay impacto)

# --- PROFUNDIDAD DEL LIBRO ---
DEPTH_RANGE = 0.10          # Analizar 10 centavos arriba y abajo del precio

# --- INTERVALO ---
INTERVALO_SEG = 300         # Escanear cada 5 minutos (300 segundos)

# --- BLACKLIST (mercados a ignorar) ---
blacklist = [
    'rounds', 'fight', 'ko', 'tko', 'vs', 'stoppage', 'points', 'rebounds',
    'assists', 'pts', 'reb', 'ast', 'spread', 'game', 'xrp', 'btc',
    'eth', 'sol', 'crypto', 'bitcoin', 'ethereum', 'solana', 'doge', 'pepe',
    'nba', 'nfl', 'soccer', 'football', 'ufc', 'boxing', 'tennis', 'mlb', 'nhl'
]

# =========================================================
# 🧠 MEMORIA PERSISTENTE (sobrevive reinicios de Render)
# =========================================================
MEMORIA_FILE = "/tmp/memoria_deltas.json"

def guardar_memoria(memoria):
    """Guarda la memoria en disco para no perderla si Render reinicia."""
    try:
        with open(MEMORIA_FILE, 'w') as f:
            json.dump(memoria, f)
    except:
        pass

def cargar_memoria():
    """Carga la memoria guardada. Si no existe, empieza vacía."""
    try:
        if os.path.exists(MEMORIA_FILE):
            with open(MEMORIA_FILE, 'r') as f:
                data = json.load(f)
                print(f"🧠 Memoria recuperada: {len(data)} mercados")
                return data
    except:
        pass
    print("🧠 Memoria nueva (primer arranque)")
    return {}

# =========================================================
# 🔧 SISTEMA
# =========================================================
app = Flask(__name__)
session = requests.Session()
session.headers.update({'Accept': 'application/json'})

stats = {
    'ciclos': 0,
    'alertas': 0,
    'errores': 0,
    'ultimo': 'Iniciando...'
}

@app.route('/')
def home():
    return (
        f"🛰️ Radar 5.5 | Mercados: {stats.get('mercados', 0)} | "
        f"Ciclos: {stats['ciclos']} | Alertas: {stats['alertas']} | "
        f"Errores CLOB: {stats['errores']} | Último: {stats['ultimo']}"
    ), 200


def enviar_telegram(mensaje):
    """Envía mensaje a Telegram."""
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        try:
            session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={
                    "chat_id": cid,
                    "text": mensaje,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False
                },
                timeout=10
            )
        except:
            pass


def leer_libro(token_id):
    """Lee el orderbook de un token. Retorna (bids, asks) o (None, None) si falla."""
    try:
        resp = session.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=8
        )
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
    """
    Lee los libros YES y NO, calcula el delta combinado.
    Retorna un diccionario con los datos o None si falla.
    """
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5","0.5"]'))

        if len(tokens) < 2 or len(prices) < 2:
            return None

        precio_yes = float(prices[0])

        # FILTRO 3: Zona de Oro
        if precio_yes < MIN_PRICE or precio_yes > MAX_PRICE:
            return None

        # Leer libro YES
        bids_yes, asks_yes = leer_libro(tokens[0])
        if bids_yes is None:
            stats['errores'] += 1
            return None

        time.sleep(0.15)  # Pausa para no saturar la API

        # Leer libro NO
        precio_no = float(prices[1])
        bids_no, asks_no = leer_libro(tokens[1])
        if bids_no is None:
            stats['errores'] += 1
            return None

        # Calcular delta YES (bids - asks en profundidad de 10¢)
        piso_y = max(0, precio_yes - DEPTH_RANGE)
        techo_y = min(1, precio_yes + DEPTH_RANGE)
        bid_usd_yes = sum(float(b['price']) * float(b['size']) for b in bids_yes if float(b['price']) >= piso_y)
        ask_usd_yes = sum(float(a['price']) * float(a['size']) for a in asks_yes if float(a['price']) <= techo_y)

        # Calcular delta NO
        piso_n = max(0, precio_no - DEPTH_RANGE)
        techo_n = min(1, precio_no + DEPTH_RANGE)
        bid_usd_no = sum(float(b['price']) * float(b['size']) for b in bids_no if float(b['price']) >= piso_n)
        ask_usd_no = sum(float(a['price']) * float(a['size']) for a in asks_no if float(a['price']) <= techo_n)

        # Delta combinado: positivo = presión compradora YES, negativo = vendedora
        delta_yes = bid_usd_yes - ask_usd_yes
        delta_no = bid_usd_no - ask_usd_no
        delta_total = int(delta_yes - delta_no)

        # Best bid/ask para calcular spread
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

    except Exception as e:
        print(f"⚠️ Error en '{m.get('question', '?')[:40]}': {e}")
        stats['errores'] += 1
        return None


def bucle_principal():
    memoria = cargar_memoria()

    print("🤖 Radar 5.5 arrancando...")
    enviar_telegram(
        f"⚡ *Radar 5.5 Online*\n\n"
        f"🎚️ *Filtros activos:*\n"
        f"🐋 Ballena: ≥ ${UMBRAL_BALLENA:,}\n"
        f"🥷 Insider: ≥ ${UMBRAL_INSIDER:,} + Spread ≥{MIN_SPREAD_PCT}% ó Tox ≥{MIN_TOXICITY_PCT}%\n"
        f"💧 Liquidez mín: ${MIN_LIQUIDITY:,}\n"
        f"🏷️ Precio: ${MIN_PRICE} - ${MAX_PRICE}\n"
        f"🚫 Spread máx: {MAX_SPREAD_PCT}%\n"
        f"⏱️ Escaneo cada {INTERVALO_SEG}s\n"
        f"🧠 Memoria: {len(memoria)} mercados recuperados"
    )

    while True:
        try:
            inicio = time.time()

            # ── Paso 1: Obtener mercados activos ──
            all_markets = []
            offset = 0
            while offset < 5000:
                try:
                    data = session.get(
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
                time.sleep(0.1)

            # ── Paso 2: Aplicar filtros básicos ──
            # FILTRO 2: Liquidez mínima + Blacklist
            mercados = [
                m for m in all_markets
                if float(m.get('liquidity', 0)) >= MIN_LIQUIDITY
                and not any(w in m.get('question', '').lower() for w in blacklist)
            ]

            print(f"📡 Total: {len(all_markets)} | Filtrados: {len(mercados)}")

            # ── Paso 3: Analizar cada mercado ──
            alertas_ciclo = 0
            ok_count = 0

            for m in mercados:
                nombre = m['question']
                liquidez = int(float(m.get('liquidity', 0)))
                datos = analizar_mercado(m)

                if datos is None:
                    continue

                ok_count += 1
                d_actual = datos['delta']

                # Si ya lo tenemos en memoria, comparar
                if nombre in memoria:
                    d_pasado = memoria[nombre]['delta']
                    cambio = d_actual - d_pasado

                    # Sin cambio → siguiente
                    if cambio == 0:
                        memoria[nombre] = {'delta': d_actual}
                        continue

                    # Calcular spread
                    mid = (datos['best_ask'] + datos['best_bid']) / 2.0
                    spread = round(((datos['best_ask'] - datos['best_bid']) / mid) * 100, 2) if mid > 0 else 0

                    # Calcular toxicidad
                    tox = round((abs(cambio) / liquidez) * 100, 2) if liquidez > 0 else 0

                    # ═══════════════════════════════════════
                    # 🎯 LÓGICA DE ALERTAS (Doble Vía)
                    # ═══════════════════════════════════════

                    # FILTRO 4: Spread máximo → mercado roto
                    if spread > MAX_SPREAD_PCT:
                        memoria[nombre] = {'delta': d_actual}
                        continue

                    # VÍA 1: 🐋 BALLENA
                    # Cambio ≥ $5,000 → alerta directa (solo necesita spread ≤ 30%)
                    es_ballena = abs(cambio) >= UMBRAL_BALLENA

                    # VÍA 2: 🥷 INSIDER
                    # Cambio ≥ $500 Y (spread ≥ 2% Ó toxicidad ≥ 3%)
                    es_insider = (
                        abs(cambio) >= UMBRAL_INSIDER
                        and (spread >= MIN_SPREAD_PCT or tox >= MIN_TOXICITY_PCT)
                    )

                    if es_ballena or es_insider:
                        tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                        etiqueta = "🐋 BALLENA" if es_ballena else "🥷 INSIDER"

                        # Explicar por qué pasó los filtros
                        if es_ballena:
                            razon = f"Delta ≥ ${UMBRAL_BALLENA:,}"
                        else:
                            razones = []
                            if spread >= MIN_SPREAD_PCT:
                                razones.append(f"Spread {spread}% ≥ {MIN_SPREAD_PCT}%")
                            if tox >= MIN_TOXICITY_PCT:
                                razones.append(f"Tox {tox}% ≥ {MIN_TOXICITY_PCT}%")
                            razon = " + ".join(razones)

                        mensaje = (
                            f"🚨 *ALERTA {etiqueta}* 🚨\n\n"
                            f"📌 *{nombre}*\n\n"
                            f"💰 *Cambio:* `${cambio:,}`\n"
                            f"🕰️ *Delta Ant:* `${d_pasado:,}` → *Actual:* `${d_actual:,}`\n"
                            f"   ├ YES: `${datos['delta_yes']:,}` | NO: `${datos['delta_no']:,}`\n"
                            f"💧 *Liquidez:* `${liquidez:,}`\n"
                            f"⚖️ *Acción:* {tipo}\n\n"
                            f"🧠 *Microestructura:*\n"
                            f"🏷️ Precio: `${datos['precio']:.3f}`\n"
                            f"📈 Bid/Ask: `${datos['best_bid']:.3f}` / `${datos['best_ask']:.3f}`\n"
                            f"🏃 Spread: `{spread}%` | 🌊 Tox: `{tox}%`\n"
                            f"✅ *Razón:* {razon}\n\n"
                            f"🔗 [Ver en Polymarket](https://polymarket.com/event/{m.get('slug', '')})"
                        )
                        enviar_telegram(mensaje)
                        alertas_ciclo += 1
                        stats['alertas'] += 1

                # Guardar delta actual
                memoria[nombre] = {'delta': d_actual}

                # Pausa entre mercados
                time.sleep(0.1)

            # ── Guardar memoria a disco ──
            guardar_memoria(memoria)

            # ── Stats ──
            stats['ciclos'] += 1
            stats['mercados'] = len(memoria)
            duracion = round(time.time() - inicio, 1)
            stats['ultimo'] = datetime.now().strftime('%H:%M:%S')

            print(
                f"✅ Ciclo #{stats['ciclos']} ({duracion}s) | "
                f"OK: {ok_count}/{len(mercados)} | "
                f"Alertas: {alertas_ciclo} | "
                f"Memoria: {len(memoria)}"
            )

            # Diagnóstico cada 12 ciclos (~1 hora)
            if stats['ciclos'] % 12 == 0:
                enviar_telegram(
                    f"📊 *Diagnóstico #{stats['ciclos']}*\n"
                    f"👁️ Mercados: {len(memoria)}\n"
                    f"📡 Libros OK: {ok_count}/{len(mercados)}\n"
                    f"🚨 Alertas total: {stats['alertas']}\n"
                    f"❌ Errores CLOB: {stats['errores']}\n"
                    f"⏱️ Ciclo: {duracion}s"
                )

            time.sleep(INTERVALO_SEG)

        except Exception as e:
            print(f"❌ Error principal: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
