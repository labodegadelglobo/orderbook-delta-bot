import requests
import json
import time
import os
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask

# =========================================================
# 🔑 CREDENCIALES
# =========================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_IDS_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# ⚙️ CONFIGURACIÓN "FRANCOTIRADOR 5.5" (Filtros del Usuario)
# =========================================================
UMBRAL_ALERTA = 500         # Vía Insider: cambio mínimo $500
UMBRAL_BALLENA_TOP = 5000   # Vía Ballena: cambio mínimo $5,000
MIN_LIQUIDITY = 500          # Liquidez mínima del mercado
INTERVALO_ESC_SEG = 300      # Escaneo cada 5 minutos
DEPTH_PERCENT = 10.0         # Profundidad del libro a analizar

# 🧠 MICROESTRUCTURA
MIN_SPREAD_PCT = 2.0         # Spread mínimo para Vía Insider
MAX_SPREAD_PCT = 30.0        # Spread máximo: mercado roto si supera esto
MIN_TOXICITY_PCT = 3.0       # Toxicidad mínima para Vía Insider

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

# =========================================================
# 📊 CONTADORES DE DIAGNÓSTICO
# =========================================================
stats = {
    'ciclos': 0,
    'mercados_escaneados': 0,
    'errores_clob': 0,
    'alertas_enviadas': 0,
    'ultimo_ciclo': 'N/A'
}

@app.route('/')
def home():
    return (
        f"🛰️ Radar 5.5 Online | Vigilando: {len(memoria_deltas)} mercados\n"
        f"📊 Ciclos: {stats['ciclos']} | Alertas: {stats['alertas_enviadas']} | "
        f"Errores CLOB: {stats['errores_clob']} | Último: {stats['ultimo_ciclo']}"
    ), 200


def enviar_telegram(mensaje):
    ids = [idx.strip() for idx in CHAT_IDS_RAW.split(',') if idx.strip()]
    for cid in ids:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": cid,
            "text": mensaje,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False
        }
        try:
            resp = session.post(url, data=payload, timeout=10)
            if resp.status_code != 200:
                print(f"⚠️ Telegram error para {cid}: {resp.status_code} - {resp.text[:100]}")
        except Exception as e:
            print(f"⚠️ Telegram exception para {cid}: {e}")


def obtener_libro(token_id):
    """Obtiene el orderbook de un token con manejo de errores explícito."""
    try:
        resp = session.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=8
        )
        if resp.status_code == 429:
            print(f"⚠️ Rate limited en CLOB para token {token_id[:20]}...")
            time.sleep(2)
            return None
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.exceptions.Timeout:
        print(f"⚠️ Timeout en CLOB para token {token_id[:20]}...")
        return None
    except Exception as e:
        print(f"⚠️ Error CLOB: {e}")
        return None


def calcular_datos_mercado(m):
    """
    FIX #1: Lee AMBOS libros (YES y NO) para capturar el delta completo.
    FIX #3: Errores explícitos en vez de except silencioso.
    FIX #4: Depth corregido - rango absoluto, no relativo al precio.
    """
    try:
        tokens = json.loads(m.get('clobTokenIds', '[]'))
        prices = json.loads(m.get('outcomePrices', '["0.5","0.5"]'))

        if len(tokens) < 2 or len(prices) < 2:
            return None

        precio_yes = float(prices[0])
        precio_no = float(prices[1])

        # Zona de Oro: filtrar por precio YES
        if precio_yes < MIN_PRICE or precio_yes > MAX_PRICE:
            return None

        token_yes = tokens[0]
        token_no = tokens[1]

        # ── FIX #1: Leer AMBOS libros ──
        libro_yes = obtener_libro(token_yes)
        if libro_yes is None:
            stats['errores_clob'] += 1
            return None

        # Pausa pequeña entre llamadas al CLOB para evitar rate limit (FIX #5)
        time.sleep(0.15)

        libro_no = obtener_libro(token_no)
        if libro_no is None:
            stats['errores_clob'] += 1
            return None

        # ── Libro YES ──
        bids_yes = libro_yes.get('bids', [])
        asks_yes = libro_yes.get('asks', [])
        best_bid_yes = float(bids_yes[0]['price']) if bids_yes else 0.0
        best_ask_yes = float(asks_yes[0]['price']) if asks_yes else 1.0

        # ── Libro NO ──
        bids_no = libro_no.get('bids', [])
        asks_no = libro_no.get('asks', [])

        # ── FIX #4: Depth con rango fijo de 10 centavos ──
        # En vez de precio * 10%, usamos un rango fijo que captura
        # más niveles del libro, especialmente en precios bajos.
        DEPTH_RANGE = 0.10  # 10 centavos de profundidad fija

        # Delta YES: bids_yes - asks_yes en profundidad
        piso_yes = max(0, precio_yes - DEPTH_RANGE)
        techo_yes = min(1, precio_yes + DEPTH_RANGE)
        b_usd_yes = sum(
            float(b['price']) * float(b['size'])
            for b in bids_yes if float(b['price']) >= piso_yes
        )
        a_usd_yes = sum(
            float(a['price']) * float(a['size'])
            for a in asks_yes if float(a['price']) <= techo_yes
        )

        # Delta NO: bids_no - asks_no en profundidad
        piso_no = max(0, precio_no - DEPTH_RANGE)
        techo_no = min(1, precio_no + DEPTH_RANGE)
        b_usd_no = sum(
            float(b['price']) * float(b['size'])
            for b in bids_no if float(b['price']) >= piso_no
        )
        a_usd_no = sum(
            float(a['price']) * float(a['size'])
            for a in asks_no if float(a['price']) <= techo_no
        )

        # ── Delta combinado ──
        # Comprar NO = Vender YES, así que restamos el delta NO
        # Delta positivo = presión compradora en YES
        # Delta negativo = presión vendedora en YES (o compradora en NO)
        delta_yes = b_usd_yes - a_usd_yes
        delta_no = b_usd_no - a_usd_no
        delta_total = int(delta_yes - delta_no)

        return {
            'delta': delta_total,
            'best_bid': best_bid_yes,
            'best_ask': best_ask_yes,
            'precio': precio_yes,
            'delta_yes': int(delta_yes),
            'delta_no': int(delta_no)
        }

    except (json.JSONDecodeError, ValueError, IndexError, KeyError) as e:
        # FIX #3: Log explícito del error
        print(f"⚠️ Error parseando mercado '{m.get('question', '?')[:40]}': {e}")
        stats['errores_clob'] += 1
        return None
    except Exception as e:
        print(f"⚠️ Error inesperado en '{m.get('question', '?')[:40]}': {e}")
        stats['errores_clob'] += 1
        return None


def bucle_principal():
    global memoria_deltas
    print("🤖 Radar 5.5: Iniciando sistema con FIX de libro dual YES+NO...")
    enviar_telegram(
        "⚡ *Radar 5.5 Online*\n"
        "✅ FIX: Libro dual YES+NO\n"
        "✅ FIX: Depth corregido (10¢ fijo)\n"
        "✅ FIX: Rate limit controlado\n"
        "✅ FIX: Logging de errores activo\n"
        f"⏱️ Intervalo: {INTERVALO_ESC_SEG}s | 🎚️ Ballena: ${UMBRAL_BALLENA_TOP:,} | Insider: ${UMBRAL_ALERTA:,}"
    )

    while True:
        try:
            ciclo_inicio = time.time()

            # ── Paso 1: Obtener todos los mercados activos ──
            all_m = []
            offset = 0
            while offset < 5000:
                url = (
                    f"https://gamma-api.polymarket.com/markets?"
                    f"active=true&closed=false&limit=100&offset={offset}"
                    f"&order=liquidity&ascending=false"
                )
                data = session.get(url, timeout=15).json()
                if not data:
                    break
                all_m.extend(data)
                offset += 100
                time.sleep(0.1)

            # ── Paso 2: Filtrar por blacklist y liquidez ──
            filtrados = [
                m for m in all_m
                if not any(w in m.get('question', '').lower() for w in blacklist)
                and float(m.get('liquidity', 0)) >= MIN_LIQUIDITY
            ]

            print(f"📡 Mercados totales: {len(all_m)} | Post-filtro: {len(filtrados)}")

            # ── FIX #5: Procesar secuencialmente con pausas ──
            # En vez de ThreadPoolExecutor que bombardea la API,
            # procesamos de forma secuencial con pausas controladas.
            # La pausa ya está dentro de calcular_datos_mercado().
            resultados = []
            errores_ciclo = 0
            for m in filtrados:
                dato = calcular_datos_mercado(m)
                resultados.append(dato)
                if dato is None:
                    errores_ciclo += 1
                # Pausa entre mercados para respetar rate limits
                time.sleep(0.1)

            exitos = len(resultados) - errores_ciclo
            print(f"📊 Libros leídos OK: {exitos}/{len(filtrados)} | Errores: {errores_ciclo}")

            # ── Paso 3: Comparar deltas y generar alertas ──
            alertas_ciclo = 0
            for i, m in enumerate(filtrados):
                id_m = m['question']
                datos = resultados[i]
                liquidez_m = int(float(m.get('liquidity', 0)))

                if datos is None or liquidez_m == 0:
                    continue

                d_actual = datos['delta']

                if id_m in memoria_deltas:
                    delta_pasado = memoria_deltas[id_m]['delta']
                    cambio = d_actual - delta_pasado

                    # Si no hubo cambio, saltamos
                    if cambio == 0:
                        memoria_deltas[id_m] = {'delta': d_actual}
                        continue

                    mid = (datos['best_ask'] + datos['best_bid']) / 2.0
                    spread = round(
                        ((datos['best_ask'] - datos['best_bid']) / mid) * 100, 2
                    ) if mid > 0 else 0
                    tox = round((abs(cambio) / liquidez_m) * 100, 2)

                    # 🛡️ REGLA DE SEGURIDAD: Spread > MAX = mercado roto
                    if spread > MAX_SPREAD_PCT:
                        memoria_deltas[id_m] = {'delta': d_actual}
                        continue

                    # ── Vía 1: BALLENA (≥ $5,000) ──
                    es_ballena_top = abs(cambio) >= UMBRAL_BALLENA_TOP

                    # ── Vía 2: INSIDER (≥ $500 + spread ≥ 2% O toxicidad ≥ 3%) ──
                    pasa_filtros_micro = (
                        abs(cambio) >= UMBRAL_ALERTA
                        and (spread >= MIN_SPREAD_PCT or tox >= MIN_TOXICITY_PCT)
                    )

                    if es_ballena_top or pasa_filtros_micro:
                        tipo = "🟢 COMPRA" if cambio > 0 else "🔴 VENTA"
                        alerta_emoji = "🐋 BALLENA" if es_ballena_top else "🥷 INSIDER"

                        mensaje = (
                            f"🚨 *ALERTA {alerta_emoji}* 🚨\n\n"
                            f"📌 *{id_m}*\n\n"
                            f"💰 *Cambio:* `${cambio:,} USD`\n"
                            f"🕰️ *Delta Anterior:* `${delta_pasado:,} USD`\n"
                            f"📊 *Delta Actual:* `${d_actual:,} USD`\n"
                            f"   ├ YES: `${datos['delta_yes']:,}` | NO: `${datos['delta_no']:,}`\n"
                            f"💧 *Liq:* `${liquidez_m:,} USD`\n"
                            f"⚖️ *Acción:* {tipo}\n\n"
                            f"🧠 *Análisis:*\n"
                            f"🏷️ *Precio:* `${datos['precio']:.3f}`\n"
                            f"📈 *Bid/Ask:* `${datos['best_bid']:.3f}` / `${datos['best_ask']:.3f}`\n"
                            f"🏃 *Spread:* `{spread}%` | 🌊 *Tox:* `{tox}%`\n\n"
                            f"🔗 [Ver en Polymarket](https://polymarket.com/event/{m.get('slug', '')})"
                        )
                        enviar_telegram(mensaje)
                        alertas_ciclo += 1
                        stats['alertas_enviadas'] += 1

                # Guardar delta actual para el próximo ciclo
                memoria_deltas[id_m] = {'delta': d_actual}

            # ── Estadísticas del ciclo ──
            stats['ciclos'] += 1
            stats['mercados_escaneados'] = len(memoria_deltas)
            duracion = round(time.time() - ciclo_inicio, 1)
            stats['ultimo_ciclo'] = datetime.now().strftime('%H:%M:%S')

            resumen = (
                f"✅ Ciclo #{stats['ciclos']} OK en {duracion}s | "
                f"Vigilando: {len(memoria_deltas)} | "
                f"Alertas este ciclo: {alertas_ciclo} | "
                f"Errores CLOB: {errores_ciclo}"
            )
            print(resumen)

            # Cada 20 ciclos, mandar diagnóstico por Telegram
            if stats['ciclos'] % 20 == 0:
                enviar_telegram(
                    f"📊 *Diagnóstico Ciclo #{stats['ciclos']}*\n"
                    f"👁️ Mercados: {len(memoria_deltas)}\n"
                    f"📡 Libros OK: {exitos}/{len(filtrados)}\n"
                    f"🚨 Alertas totales: {stats['alertas_enviadas']}\n"
                    f"❌ Errores CLOB acumulados: {stats['errores_clob']}\n"
                    f"⏱️ Duración ciclo: {duracion}s"
                )

            time.sleep(INTERVALO_ESC_SEG)

        except Exception as e:
            print(f"❌ Error en bucle principal: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=bucle_principal, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
