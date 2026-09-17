import json
import os
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from urllib.parse import urlparse
import html
import requests

from dotenv import load_dotenv
from openai import OpenAI
from googlenewsdecoder import gnewsdecoder

from news_s3_store import load_state, load_json_state, save_json_state
from telegram_utils import telegram_send_message

load_dotenv()

try:
    from zoneinfo import ZoneInfo
    CO_TZ = ZoneInfo("America/Bogota")
except Exception:
    from datetime import timedelta
    CO_TZ = timezone(timedelta(hours=-5))

BASE_DIR = Path(__file__).resolve().parent
SOURCES_FILE = BASE_DIR / os.getenv("MAURICIO_NEWS_SOURCES_FILE", "google_news_sources_mauricio.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID_MAURICIO", "").strip()
    or os.getenv("TELEGRAM_CHAT_ID_ALERTAS", "").strip()
    or os.getenv("TELEGRAM_CHAT_ID_DEFAULT", "").strip()
)
BRIEF_MODEL = os.getenv("MAURICIO_BRIEF_MODEL", "gpt-5-mini").strip()
BRIEF_STATE_KEY = os.getenv(
    "MAURICIO_NEWS_BRIEF_STATE_S3_KEY",
    "mauricio_toro/news/state/news_brief_state.json",
)
BRIEF_HOURS = tuple(
    int(x.strip()) for x in os.getenv("MAURICIO_BRIEF_HOURS_CO", "8,12,18").split(",") if x.strip()
)
MAX_CANDIDATES = int(os.getenv("MAURICIO_BRIEF_MAX_CANDIDATES", "40"))
MAX_CANDIDATES_PER_TOPIC = int(os.getenv("MAURICIO_BRIEF_MAX_PER_TOPIC", "6"))
MAX_NOTES = int(os.getenv("MAURICIO_BRIEF_MAX_NOTES", "6"))
MAX_TELEGRAM_CHARS = int(os.getenv("MAURICIO_BRIEF_MAX_CHARS", "3700"))

NON_NEWS_DOMAINS = {
    "facebook.com", "www.facebook.com",
    "instagram.com", "www.instagram.com",
    "x.com", "twitter.com",
    "youtube.com", "www.youtube.com", "youtu.be",
    "tiktok.com", "www.tiktok.com",
}

# Consolida RSS específicos en los grandes ejes que verá el equipo.
# Los temas sin noticias no se envían al modelo y, por tanto, no generan bullet.
RSS_TOPIC_OVERRIDES = {
    "tema_educacion_icetex": "Educación",
    "tema_educacion_superior": "Educación",
    "tema_educacion_tecnica_sena": "Educación",
    "tema_emprendimiento_mipymes": "Emprendimiento",
    "tema_emprendimiento_startups": "Emprendimiento",
    "tema_emprendimiento_microempresas": "Emprendimiento",
    "tema_tecnologia_ia": "Tecnología",
    "tema_tecnologia_telecomunicaciones": "Tecnología",
    "tema_tecnologia_transformacion_digital": "Tecnología",
    "tema_tecnologia_innovacion": "Tecnología",
    "tema_tecnologia_gobierno_digital": "Tecnología",
    "tema_tecnologia_notarias": "Tecnología",
    "tema_economia_digital_fintech": "Economía digital",
    "tema_economia_digital_cripto": "Economía digital",
    "tema_economia_digital_plataformas": "Economía digital",
    "tema_economia_digital_colaborativa": "Economía digital",
    "tema_salud_endometriosis": "Salud",
    "tema_salud_etiquetado_frontal": "Salud",
    "tema_salud_asbesto": "Salud",
    "tema_bogota_carlos_fernando_galan": "Alcalde Galán",
    "tema_bogota_clara_lucia_sandoval": "Alcaldía de Bogotá",
    "tema_bogota_campin": "Alcaldía de Bogotá",
    "tema_bogota_ptar_canoas": "Alcaldía de Bogotá",
    "tema_bogota_presupuesto_2027": "Alcaldía de Bogotá",
    "tema_bogota_rio_bogota": "Alcaldía de Bogotá",
    "tema_seguridad_hurto_bogota": "Seguridad",
    "tema_seguridad_extorsion_bogota": "Seguridad",
    "tema_seguridad_camaras_bogota": "Seguridad",
    "tema_diversidad_lgbtiq": "Diversidad",
    "tema_energia_soberania": "Energía",
}


def safe_text(value):
    return "" if value is None else str(value).strip()


def parse_dt(value):
    value = safe_text(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def load_source_map():
    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    result = {}
    for client in data:
        if client.get("activo", True) is not True:
            continue
        for rss in client.get("rss", []):
            if rss.get("activo", True) is not True:
                continue
            result[safe_text(rss.get("id"))] = rss
    return result


def current_due_slot(now_co=None):
    now_co = now_co or datetime.now(CO_TZ)
    due = [hour for hour in BRIEF_HOURS if now_co.time() >= time(hour, 0)]
    if not due:
        return None
    hour = max(due)
    return f"{now_co.date().isoformat()}T{hour:02d}:00:00-05:00"


def collect_candidates():
    articles, matches, _ = load_state()
    source_map = load_source_map()
    matches_by_article = defaultdict(list)
    for match in matches:
        matches_by_article[safe_text(match.get("article_id"))].append(match)

    today = datetime.now(CO_TZ).date()
    candidates = []

    for article in articles:
        if safe_text(article.get("relevante")).lower() != "true":
            continue
        if safe_text(article.get("es_operativa")).lower() != "true":
            continue

        published = parse_dt(article.get("fecha_publicacion_utc"))
        if published is None or published.astimezone(CO_TZ).date() != today:
            continue

        topics = []
        eligible = False
        for match in matches_by_article.get(safe_text(article.get("article_id")), []):
            rss_id = safe_text(match.get("rss_id"))
            cfg = source_map.get(rss_id) or {}
            if cfg.get("incluir_brief", rss_id != "mauricio_toro") is not True:
                continue
            eligible = True
            label = RSS_TOPIC_OVERRIDES.get(rss_id)

            # Compatibilidad defensiva con registros históricos de ICETEX/SENA.
            # Fuera de esto, un RSS no mapeado NO crea un eje nuevo.
            if not label:
                termino = safe_text(cfg.get("termino")).lower()
                if "icetex" in termino or "sena" in termino:
                    label = "Educación"

            if label and label not in topics:
                topics.append(label)

        if not eligible or not topics:
            continue

        candidates.append({
            "titulo": safe_text(article.get("titulo")),
            "fuente": safe_text(article.get("fuente")),
            "resumen_rss": safe_text(article.get("resumen_rss"))[:900],
            "url": safe_text(article.get("url_original")) or safe_text(article.get("enlace")),
            "fecha_publicacion_utc": safe_text(article.get("fecha_publicacion_utc")),
            "temas": topics[:4],
        })

    candidates.sort(key=lambda x: x["fecha_publicacion_utc"], reverse=True)

    # Balance local por eje para ahorrar tokens y evitar que un tema de alto volumen
    # desplace por completo a los demás. No hace llamadas adicionales a OpenAI.
    balanced = []
    topic_counts = defaultdict(int)
    for row in candidates:
        if len(balanced) >= MAX_CANDIDATES:
            break
        if all(topic_counts[t] >= MAX_CANDIDATES_PER_TOPIC for t in row["temas"]):
            continue
        balanced.append(row)
        for topic in row["temas"]:
            topic_counts[topic] += 1

    return balanced



def is_news_selection_candidate(row):
    """Evita redes sociales en la sección de lecturas seleccionadas."""
    url = safe_text(row.get("url"))
    source = safe_text(row.get("fuente")).lower()

    host = ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        pass

    if host in NON_NEWS_DOMAINS:
        return False

    social_source_tokens = (
        "facebook", "instagram", "youtube", "tiktok", "twitter", "x.com"
    )
    if any(token in source for token in social_source_tokens):
        return False

    return True



def resolve_selected_urls(result, candidates):
    """
    Garantiza URL final sólo para las notas ya seleccionadas.
    Prioridad:
      1) url_original persistida por el worker.
      2) Si aún queda Google News, decodifica en memoria.
      3) Si falla, conserva la URL existente.

    No usa OpenAI y no modifica S3.
    Como máximo intenta MAX_NOTES URLs por brief.
    """
    for idx in result.get("seleccion_ids", []):
        try:
            row = candidates[int(idx) - 1]
        except Exception:
            continue

        current_url = safe_text(row.get("url"))
        if not current_url or "news.google.com" not in current_url:
            continue

        try:
            decoded = gnewsdecoder(current_url, interval=1)
            if isinstance(decoded, dict) and decoded.get("status") is True:
                final_url = safe_text(decoded.get("decoded_url"))
                if final_url and "news.google.com" not in final_url:
                    row["url"] = final_url
        except Exception as error:
            print(
                "⚠️ BRIEF | no se pudo resolver URL seleccionada | "
                f"{safe_text(row.get('titulo'))} | {error}"
            )


def trim_message(message, max_chars=MAX_TELEGRAM_CHARS):
    """Mantiene el brief en un solo mensaje de Telegram."""
    if len(message) <= max_chars:
        return message

    lines = message.splitlines()

    # Primero elimina URLs/notas desde el final, conservando el cuerpo ejecutivo.
    while len("\n".join(lines)) > max_chars and lines:
        # Busca el último bloque de nota (línea bullet + posible URL).
        last_bullet = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("• "):
                last_bullet = i
                break
        if last_bullet is None:
            break
        del lines[last_bullet:]

    result = "\n".join(lines).strip()
    if len(result) > max_chars:
        result = result[: max_chars - 1].rstrip() + "…"
    return result

def build_prompt(candidates):
    active_topics = []
    for row in candidates:
        for topic in row["temas"]:
            if topic and topic not in active_topics:
                active_topics.append(topic)

    notes = [
        {
            "id": i + 1,
            "titulo": row["titulo"],
            "medio": row["fuente"],
            "resumen": row["resumen_rss"][:500],
            "temas": row["temas"],
        }
        for i, row in enumerate(candidates)
    ]
    return f"""
Produce un brief informativo neutral y estrictamente basado en estas notas.
Devuelve SOLO JSON válido:
{{"bullets":[{{"tema":"Educación","texto":"..."}}],"seleccion_ids":[1,2,3]}}

EJES CON NOTICIAS DISPONIBLES:
{json.dumps(active_topics, ensure_ascii=False)}

Reglas:
- Genera COMO MÁXIMO UN bullet por cada eje disponible.
- Genera un bullet SOLO cuando haya información sustantiva para ese eje.
- Si un eje no tiene noticias útiles, OMÍTELO. Nunca escribas "sin noticias",
  "sin novedades", "no hubo información" ni equivalentes.
- El campo tema debe coincidir exactamente con uno de los EJES CON NOTICIAS DISPONIBLES.
- "tema" es metadata interna. NO escribas el nombre del eje como encabezado ni al inicio de "texto".
- Sintetiza dentro de cada bullet los hechos relevantes de ese eje; agrupa duplicados.
- No mezcles dos ejes distintos en un mismo bullet.
- No incluyas a Mauricio Toro como tema: sus menciones tienen alerta inmediata aparte.
- No agregues hechos, opiniones, inferencias ni contexto ausente.
- Cada bullet debe ser autosuficiente: identifica explícitamente personas, instituciones
  o actores. Evita referencias ambiguas.
- No redactes como agregador: evita "el medio informó", "X reportó" y fórmulas similares.
- Tono descriptivo, neutral y no persuasivo.
- No existe un mínimo de bullets. No generes bullets de relleno.
- seleccion_ids: máximo {MAX_NOTES} notas útiles y diversas.
- Para seleccion_ids prioriza notas informativas de medios; no selecciones
  publicaciones de Facebook, Instagram, X/Twitter, YouTube o TikTok.
- No selecciones varias notas que cuenten esencialmente el mismo hecho.
- Sin markdown dentro del JSON.

NOTAS:
{json.dumps(notes, ensure_ascii=False)}
""".strip()


def generate_brief(candidates):
    client = OpenAI()
    response = client.responses.create(model=BRIEF_MODEL, input=build_prompt(candidates))
    raw = response.output_text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    result = json.loads(raw)

    valid_topics = {topic for row in candidates for topic in row.get("temas", [])}
    bullets = []
    seen_topics = set()
    for item in result.get("bullets", []):
        if not isinstance(item, dict):
            continue
        topic = safe_text(item.get("tema"))
        text = safe_text(item.get("texto"))
        if not topic or not text or topic not in valid_topics or topic in seen_topics:
            continue
        seen_topics.add(topic)
        bullets.append({"tema": topic, "texto": text})
    result["bullets"] = bullets

    ids = []
    for value in result.get("seleccion_ids", []):
        try:
            idx = int(value)
        except Exception:
            continue
        if (
            1 <= idx <= len(candidates)
            and idx not in ids
            and is_news_selection_candidate(candidates[idx - 1])
        ):
            ids.append(idx)
    result["seleccion_ids"] = ids[:MAX_NOTES]
    return result


def format_message(result, candidates, now_co):
    lines = [
        "🗞️ RESUMEN DE NOTICIAS",
        f"Actualización: {now_co.strftime('%d/%m/%Y · %H:%M')} Colombia",
        "",
    ]
    for bullet in result.get("bullets", []):
        topic = safe_text(bullet.get("tema")) if isinstance(bullet, dict) else ""
        text = safe_text(bullet.get("texto")) if isinstance(bullet, dict) else safe_text(bullet)
        if text:
            lines.append(f"• {text}")

    if result.get("seleccion_ids"):
        lines.extend(["", "📌 Selección de notas"])
        for idx in result["seleccion_ids"]:
            row = candidates[idx - 1]
            title = html.escape(row["titulo"], quote=False)
            source = html.escape(
                row["fuente"] or "Medio no identificado",
                quote=False,
            )
            url = html.escape(row["url"], quote=True)

            if url:
                lines.append(
                    f'<a href="{url}">• {title}</a> — {source}'
                )
            else:
                lines.append(f"• {title} — {source}")
    return trim_message("\n".join(lines).strip())


def build_current_brief():
    """Construye el brief actual sin enviarlo ni modificar estado en S3."""
    now_co = datetime.now(CO_TZ)
    candidates = collect_candidates()

    if not candidates:
        return now_co, candidates, None, None

    result = generate_brief(candidates)
    resolve_selected_urls(result, candidates)
    message = format_message(result, candidates, now_co)
    return now_co, candidates, result, message



def telegram_send_html_message(bot_token, chat_id, text):
    """
    Envío exclusivo del brief con parse_mode=HTML.
    No modifica telegram_utils ni el flujo de alertas inmediatas.
    """
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def run_if_due(force=False):
    now_co = datetime.now(CO_TZ)
    slot = current_due_slot(now_co)

    if not force and not slot:
        print("⏳ BRIEF | todavía no hay corte vencido.")
        return False

    state, _ = load_json_state(BRIEF_STATE_KEY)
    if not force and state.get("last_sent_slot") == slot:
        print(f"⏳ BRIEF | corte ya enviado: {slot}")
        return False

    candidates = collect_candidates()
    if not candidates:
        print("ℹ️ BRIEF | sin noticias elegibles hoy; no se envía.")
        return False
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ BRIEF | Telegram no configurado.")
        return False

    result = generate_brief(candidates)
    resolve_selected_urls(result, candidates)
    telegram_send_html_message(
        bot_token=BOT_TOKEN,
        chat_id=CHAT_ID,
        text=format_message(result, candidates, now_co),
    )

    sent_slot = slot or f"force:{now_co.isoformat()}"
    save_json_state(BRIEF_STATE_KEY, {
        "last_sent_slot": sent_slot,
        "last_sent_at_utc": datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z"),
        "candidate_count": len(candidates),
        "selected_count": len(result.get("seleccion_ids", [])),
    })
    print(
        f"📨 BRIEF ENVIADO | slot={sent_slot} | candidatas={len(candidates)} | "
        f"seleccionadas={len(result.get('seleccion_ids', []))}"
    )
    return True


def run_dry_run():
    """
    Prueba segura:
    - Lee noticias/matches reales desde S3.
    - Puede llamar a OpenAI para construir el brief.
    - NO envía Telegram.
    - NO modifica el estado del brief en S3.
    """
    now_co, candidates, result, message = build_current_brief()

    print("=" * 100)
    print("DRY RUN | BRIEF MAURICIO")
    print(f"Hora Colombia: {now_co.isoformat()}")
    print(f"Candidatas: {len(candidates)}")
    print("Telegram: DESACTIVADO")
    print("Estado S3 del brief: NO SE MODIFICA")
    print("=" * 100)

    if not candidates:
        print("No hay noticias elegibles para construir el brief.")
        return False

    topic_counts = defaultdict(int)
    for row in candidates:
        for topic in row.get("temas", []):
            topic_counts[topic] += 1
    print("EJES ENVIADOS A OPENAI:")
    for topic, count in topic_counts.items():
        print(f"  - {topic}: {count} candidata(s)")
    print("-" * 100)

    print(message)
    print("=" * 100)
    print(f"Caracteres del mensaje: {len(message)} / {MAX_TELEGRAM_CHARS}")

    if result.get("seleccion_ids"):
        print("-" * 100)
        print("DIAGNÓSTICO DE URLS SELECCIONADAS")
        for idx in result["seleccion_ids"]:
            row = candidates[idx - 1]
            print(f"[{idx}] {row['titulo']}")
            print(f"    URL USADA: {row['url'] or '(vacía)'}")
            if "news.google.com" in (row["url"] or ""):
                print("    ⚠️ Sigue siendo URL de Google News (registro histórico o resolución pendiente).")
            else:
                print("    ✅ URL final/no-Google disponible.")

    print("=" * 100)
    print(
        f"Selección: {len(result.get('seleccion_ids', []))} nota(s) | "
        "NINGÚN MENSAJE FUE ENVIADO"
    )
    return True


def run_test_telegram():
    """
    Envía el brief únicamente al chat de prueba.
    Requiere TELEGRAM_CHAT_ID_TEST.
    No modifica el estado del brief en S3.
    """
    test_chat_id = os.getenv("TELEGRAM_CHAT_ID_TEST", "").strip()

    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN.")

    if not test_chat_id:
        raise RuntimeError(
            "Falta TELEGRAM_CHAT_ID_TEST. "
            "No se usará el chat de producción como fallback."
        )

    now_co, candidates, result, message = build_current_brief()

    if not candidates:
        print("ℹ️ TEST TELEGRAM | sin noticias elegibles.")
        return False

    telegram_send_html_message(
        bot_token=BOT_TOKEN,
        chat_id=test_chat_id,
        text=message,
    )

    print(
        f"🧪 BRIEF ENVIADO A CHAT DE PRUEBA | "
        f"candidatas={len(candidates)} | "
        f"seleccionadas={len(result.get('seleccion_ids', []))}"
    )
    print("Estado S3 del brief: NO SE MODIFICA")
    return True


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generador del brief de noticias de Mauricio Toro."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Genera e imprime el brief sin Telegram y sin modificar el estado.",
    )
    group.add_argument(
        "--test-telegram",
        action="store_true",
        help="Envía el brief sólo a TELEGRAM_CHAT_ID_TEST; no modifica estado.",
    )
    group.add_argument(
        "--run",
        action="store_true",
        help="Modo producción: envía únicamente el corte vencido que aún no haya sido enviado.",
    )

    args = parser.parse_args()

    if args.dry_run:
        run_dry_run()
    elif args.test_telegram:
        run_test_telegram()
    elif args.run:
        run_if_due()


if __name__ == "__main__":
    main()
