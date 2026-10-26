"""RSS V16 corregido: consultas recientes, identidad V6 y almacenamiento V16."""
import argparse
import calendar
import hashlib
import html
import json
import os
import re
import time
import gc
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
from mauricio_rss_capture import query_plan, fetch_feed
from mauricio_article_reader import (read_public_article, supported_url, retry_due, pending_reason)

ARTICLE_READ_STATUS = {}
from dotenv import load_dotenv

from news_s3_store_mem_v16 import (
    load_state,
    save_state,
    load_json_state,
    save_json_state,
)
from telegram_utils import telegram_send_message
import requests

from googlenewsdecoder import gnewsdecoder


load_dotenv()

try:
    from zoneinfo import ZoneInfo
    CO_TZ = ZoneInfo("America/Bogota")
except Exception:
    from datetime import timedelta
    CO_TZ = timezone(timedelta(hours=-5))


BASE_DIR = Path(__file__).resolve().parent

SOURCES_FILE = BASE_DIR / os.getenv(
    "MAURICIO_NEWS_SOURCES_FILE",
    "google_news_sources_mauricio.json",
)

CHECK_INTERVAL_SECONDS = int(
    os.getenv("GOOGLE_NEWS_CHECK_INTERVAL", "300")
)

# V13 | Guardia durable e independiente para alertas de Telegram.
# Evita reenviar la misma historia si Telegram sí recibió el mensaje
# pero después falló el checkpoint grande de news_articles/news_matches.
TELEGRAM_ALERT_LEDGER_KEY = os.getenv(
    "MAURICIO_TELEGRAM_ALERT_LEDGER_S3_KEY",
    "mauricio_toro/news/state/telegram_alerts_ledger.json",
).strip()
TELEGRAM_ALERT_MAX_AGE_HOURS = int(
    os.getenv("MAURICIO_TELEGRAM_ALERT_MAX_AGE_HOURS", "24")
)
TELEGRAM_ALERT_LEDGER_MAX = int(
    os.getenv("MAURICIO_TELEGRAM_ALERT_LEDGER_MAX", "2500")
)

# X se consulta con una cadencia independiente para controlar costos.
X_CHECK_INTERVAL_SECONDS = int(
    os.getenv("X_CHECK_INTERVAL", "900")
)
X_LAST_RUN_FILE = Path(
    os.getenv(
        "MAURICIO_X_LAST_RUN_FILE",
        "/tmp/mauricio_toro_x_last_run.txt",
    )
)

def _load_x_last_run_epoch() -> float | None:
    try:
        raw = X_LAST_RUN_FILE.read_text(encoding="utf-8").strip()
        return float(raw) if raw else None
    except (FileNotFoundError, ValueError, OSError):
        return None

def _save_x_last_run_epoch(value: float) -> None:
    try:
        X_LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        X_LAST_RUN_FILE.write_text(str(value), encoding="utf-8")
    except OSError as error:
        print(f"⚠️ X | no se pudo persistir last_run local: {error}")

# V14 | Límites de memoria y checkpoints defensivos.
ARTICLE_TEXT_MAX_CHARS = int(
    os.getenv("MAURICIO_ARTICLE_TEXT_MAX_CHARS", "30000")
)
CHECKPOINT_EVERY_SOURCES = int(
    os.getenv("MAURICIO_CHECKPOINT_EVERY_SOURCES", "6")
)

BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID_MAURICIO", "").strip()
    or os.getenv("TELEGRAM_CHAT_ID_ALERTAS", "").strip()
    or os.getenv("TELEGRAM_CHAT_ID_DEFAULT", "").strip()
)

# Por defecto, el primer arranque guarda el backlog pero NO lo manda a Telegram.
# Sólo ponlo en true si expresamente quieres enviar también el histórico inicial.
TELEGRAM_ON_BOOTSTRAP = (
    os.getenv("MAURICIO_TELEGRAM_ON_BOOTSTRAP", "false")
    .strip()
    .lower()
    in {"1", "true", "yes", "si", "sí"}
)


MONITORING_START_DATE = date.fromisoformat(
    os.getenv("MAURICIO_MONITORING_START_DATE", "2026-08-31").strip()
)


# V15 CALIBRACIÓN | Contexto colombiano ampliado para radares nacionales.
# Se activa por fuente con "usar_contexto_colombia_ampliado": true.
# No reemplaza el filtro temático: una nota debe seguir tratando realmente
# sobre el eje monitoreado.
COLOMBIA_CONTEXT_TERMS = [
    "Colombia",
    "Gobierno Nacional",
    "Gobierno Petro",
    "Petro",
    "Presidencia",
    "Congreso",
    "Bogotá",
    "Cundinamarca",
    "Medellín",
    "Antioquia",
    "Cali",
    "Valle del Cauca",
    "Barranquilla",
    "Atlántico",
    "Cartagena",
    "Bolívar",
    "Bucaramanga",
    "Santander",
    "Manizales",
    "Caldas",
    "Pereira",
    "Risaralda",
    "Armenia",
    "Quindío",
    "Ibagué",
    "Tolima",
    "Neiva",
    "Huila",
    "Villavicencio",
    "Meta",
    "Cúcuta",
    "Norte de Santander",
    "Santa Marta",
    "Magdalena",
    "Montería",
    "Córdoba",
    "Sincelejo",
    "Sucre",
    "Valledupar",
    "Cesar",
    "Tunja",
    "Boyacá",
    "Pasto",
    "Nariño",
    "Popayán",
    "Cauca",
    "Quibdó",
    "Chocó",
    "Riohacha",
    "La Guajira",
    "San Andrés",
    "Archipiélago de San Andrés",
    "Arauca",
    "Casanare",
    "Yopal",
    "Caquetá",
    "Florencia",
    "Putumayo",
    "Amazonas",
    "Leticia",
    "Guaviare",
    "Guainía",
    "Vaupés",
    "Vichada",
    "MinTIC",
    "Ministerio TIC",
    "Ministerio de Tecnologías de la Información",
    "MinCIT",
    "MinComercio",
    "Ministerio de Comercio",
    "MinEducación",
    "Ministerio de Educación",
    "SENA",
    "ICETEX",
    "Superfinanciera",
    "Banco de la República",
]


def expanded_required_context(rss: dict) -> list[str]:
    base = list(rss.get("contexto_requerido") or [])
    if rss.get("usar_contexto_colombia_ampliado", False) is True:
        base.extend(COLOMBIA_CONTEXT_TERMS)

    # Conserva orden y elimina duplicados sin distinguir mayúsculas/minúsculas.
    result = []
    seen = set()
    for item in base:
        value = "" if item is None else str(item).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def memory_rss_mb() -> float:
    """Memoria RSS actual del proceso en Linux/Render, en MB."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return kb / 1024.0
    except Exception:
        pass
    return -1.0


def log_memory(stage: str) -> None:
    mb = memory_rss_mb()
    if mb >= 0:
        print(f"🧠 MEM | {stage} | rss={mb:.1f} MB")
    else:
        print(f"🧠 MEM | {stage} | rss=no_disponible")


def safe_text(value) -> str:
    return "" if value is None else str(value).strip()


def clean_html_text(value) -> str:
    text = html.unescape(safe_text(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_iso(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_sources() -> list[dict]:
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(
            f"No existe el archivo de fuentes: {SOURCES_FILE}"
        )

    data = json.loads(
        SOURCES_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(data, list):
        raise ValueError(
            "El archivo de fuentes debe contener una lista de clientes."
        )

    sources = []

    for client in data:
        if (
            not isinstance(client, dict)
            or client.get("activo", True) is not True
        ):
            continue

        client_id = safe_text(client.get("cliente_id"))
        client_name = safe_text(client.get("cliente_nombre"))

        if not client_id or not client_name:
            print(
                f"⚠️ Cliente incompleto omitido: {client}"
            )
            continue

        for rss in client.get("rss") or []:
            if (
                not isinstance(rss, dict)
                or rss.get("activo", True) is not True
            ):
                continue

            rss_id = safe_text(rss.get("id"))
            url = safe_text(rss.get("url"))

            if not rss_id or not url.startswith("http"):
                print(
                    f"⚠️ RSS incompleto omitido para {client_name}: {rss}"
                )
                continue

            sources.append({
                "cliente_id": client_id,
                "cliente_nombre": client_name,
                "rss_id": rss_id,
                "termino": (
                    safe_text(rss.get("termino"))
                    or rss_id
                ),
                "aliases": rss.get("aliases") or [],
                "exclusiones": rss.get("exclusiones") or [],
                "contexto_requerido": expanded_required_context(rss),
                "contexto_tematico": (
                    rss.get("contexto_tematico") or []
                ),
                "aceptar_match_rss": (
                    rss.get("aceptar_match_rss", False) is True
                ),
                "emoji": (
                    safe_text(rss.get("emoji"))
                    or "🔵"
                ),
                "source_type": (
                    safe_text(rss.get("tipo"))
                    or "google_news_search"
                ),
                "url": url,
                "enviar_telegram": (
                    rss.get("enviar_telegram", True) is True
                ),
                "telegram_desde": safe_text(
                    rss.get("telegram_desde")
                ),
                "incluir_brief": (
                    rss.get("incluir_brief", rss_id != "mauricio_toro") is True
                ),
                "mauricio_project_signals": (
                    rss.get("mauricio_project_signals") or []
                ),
            })

    return sources



def normalize_for_match(value: str) -> str:
    text = clean_html_text(value).lower()
    return re.sub(r"\s+", " ", text).strip()

def contains_match_term(
    text: str,
    term: str,
) -> bool:

    text_norm = normalize_for_match(
        text
    )

    term_norm = normalize_for_match(
        term
    )

    if not term_norm:
        return False

    # Para términos cortos como "IA",
    # exigimos palabra completa.
    if len(term_norm) <= 3:
        pattern = (
            r"(?<!\w)"
            + re.escape(term_norm)
            + r"(?!\w)"
        )

        return bool(
            re.search(
                pattern,
                text_norm,
                flags=re.UNICODE,
            )
        )

    return term_norm in text_norm

def normalize_story_component(value: str) -> str:
    text = normalize_for_match(value)

    text = re.sub(
        r"[^\w\s]",
        " ",
        text,
        flags=re.UNICODE,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

def canonical_story_title(
    title: str,
    publisher: str,
) -> str:

    title = clean_html_text(
        title
    )

    publisher = clean_html_text(
        publisher
    )

    # Algunas filas antiguas guardaron:
    # "Titular - Nombre del medio"
    #
    # mientras que las nuevas ya guardan
    # solamente "Titular".
    if " - " in title:
        possible_title, possible_suffix = (
            title.rsplit(" - ", 1)
        )

        suffix_norm = (
            normalize_story_component(
                possible_suffix
            )
        )

        publisher_norm = (
            normalize_story_component(
                publisher
            )
        )

        # Comparamos también sin espacios para
        # tolerar cosas como:
        # "Confidencial Noticias"
        # vs "confidencialnoticias.com"
        suffix_compact = re.sub(
            r"[^a-z0-9]",
            "",
            suffix_norm,
        )

        publisher_compact = re.sub(
            r"[^a-z0-9]",
            "",
            publisher_norm,
        )

        looks_like_publisher = (
            suffix_norm
            and (
                suffix_norm in publisher_norm
                or publisher_norm in suffix_norm
                or suffix_compact in publisher_compact
                or publisher_compact in suffix_compact
            )
        )

        if looks_like_publisher:
            title = possible_title

    return normalize_story_component(
        title
    )

def build_story_key(
    title: str,
    publisher: str,
    published_dt: datetime,
) -> str:

    normalized_title = (
        canonical_story_title(
            title=title,
            publisher=publisher,
        )
    )

    publication_date = (
        published_dt
        .astimezone(CO_TZ)
        .date()
        .isoformat()
    )

    canonical = "|".join([
        normalized_title,
        publication_date,
    ])

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

def decode_google_news_url(
    google_url: str,
) -> str:

    google_url = safe_text(
        google_url
    )

    if not google_url:
        return ""

    try:
        result = gnewsdecoder(
            google_url,
            interval=1,
        )

        if (
            isinstance(result, dict)
            and (
                result.get("success") is True
                or result.get("status") is True
            )
        ):
            return safe_text(
                result.get("decoded_url")
            )

    except Exception as error:
        print(
            f"⚠️ No se pudo resolver URL Google News: {error}"
        )

    return ""


def _fetch_article_text_legacy(
    url: str,
) -> str:

    url = safe_text(url)

    if not url:
        return ""

    try:
        response = requests.get(
            url,
            timeout=12,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/131 Safari/537.36"
                )
            },
        )

        response.raise_for_status()

        html_text = response.text

        # Quitamos scripts y estilos antes de limpiar HTML.
        html_text = re.sub(
            r"<script\b[^>]*>.*?</script>",
            " ",
            html_text,
            flags=re.I | re.S,
        )

        html_text = re.sub(
            r"<style\b[^>]*>.*?</style>",
            " ",
            html_text,
            flags=re.I | re.S,
        )

        cleaned = clean_html_text(
            html_text
        )
        if ARTICLE_TEXT_MAX_CHARS > 0:
            cleaned = cleaned[:ARTICLE_TEXT_MAX_CHARS]
        return cleaned

    except Exception as error:
        print(
            f"⚠️ No se pudo leer artículo original: {error}"
        )

        return ""
def fetch_article_text(url: str) -> str:
    if not supported_url(safe_text(url)):
        return _fetch_article_text_legacy(url)
    result = read_public_article(url, max_chars=ARTICLE_TEXT_MAX_CHARS,
                               allow_cambio_fallback=os.getenv("MAURICIO_CAMBIO_READER_FALLBACK", "true").lower() == "true")
    ARTICLE_READ_STATUS[url] = result.status
    print(f"📖 LECTURA PÚBLICA | estado={result.status} | version={result.version or '-'} | intentos={len(result.attempts)} | url={url}")
    return result.text


# V19: desambiguación de la persona antes de generar alertas.
# Google News puede devolver homónimos, fotos y créditos de prensa.
MAURICIO_FULL_NAME_RE = re.compile(
    r"(?<!\w)Mauricio(?:\s+Andr[eé]s)?\s+Toro(?:\s+Orjuela)?(?!\w)",
    flags=re.I | re.UNICODE,
)

MAURICIO_POLITICAL_CONTEXT_RE = re.compile(
    r"\b(?:representante(?:s)?|congresista(?:s)?|excongresista|"
    r"c[aá]mara(?:\s+de\s+representantes)?|"
    r"alianza\s+verde|icetex|"
    r"diputado(?:s)?|proyecto\s+de\s+ley|"
    r"por\s+all[aá]\s+s[ií]\s+voy|"
    r"comisi[oó]n\s+(?:del|de)\s+congreso)\b",
    flags=re.I | re.UNICODE,
)

MAURICIO_OTHER_PERSON_RE = re.compile(
    r"\b(?:sernageomin|senapred|"
    r"servicio\s+nacional\s+de\s+geolog[ií]a|"
    r"director\s+nacional\s+del\s+organismo)\b",
    flags=re.I | re.UNICODE,
)


def mauricio_person_evidence(article: dict) -> tuple[bool, str]:
    """Confirma nombre y desambigua homónimos. Nunca usa una etiqueta de proyecto
    como prueba suficiente de identidad."""
    title = safe_text(article.get("titulo"))
    summary = safe_text(article.get("resumen_rss"))
    body = safe_text(article.get("texto_articulo"))
    url = safe_text(article.get("url_original"))
    domain = (urlparse(url).netloc or "").lower().removeprefix("www.")
    chunks = [
        ("titulo", title),
        ("resumen", summary),
        ("cuerpo", body),
    ]

    for field, value in chunks:
        for hit in MAURICIO_FULL_NAME_RE.finditer(value):
            near_before = value[max(0, hit.start() - 200):hit.start()]
            near_after = value[hit.end():hit.end() + 240]
            around = near_before + " " + hit.group(0) + " " + near_after
            immediate_before = near_before[-85:]
            immediate_after = near_after[:85]

            # Coincidencia del nombre dentro de nombres inequívocamente ajenos.
            if re.search(r"(?:[OÓ]scar)\s+$", near_before[-20:], flags=re.I):
                continue
            if re.match(r"\s*[-– ]\s*Goya\b", near_after, flags=re.I):
                continue

            # Un crédito de autor/foto no equivale a mencionar al político.
            if (
                re.search(
                    r"(?:foto(?:graf[ií]a)?|fot[oó]grafo|"
                    r"cr[eé]dito(?:s)?|imagen)\s*[:/–-]?\s*$",
                    immediate_before, flags=re.I
                )
                or re.search(r"agencia\s+uno", immediate_after, flags=re.I)
            ):
                continue

            # Chile/Sernageomin nombra a un funcionario homónimo.
            if MAURICIO_OTHER_PERSON_RE.search(around):
                # Sólo una identificación institucional colombiana cercana
                # puede invalidar este descarte.
                if not re.search(
                    r"(?:alianza\s+verde|icetex|c[aá]mara\s+de\s+representantes)",
                    around, flags=re.I
                ):
                    continue

            if re.search(r"\bAndr[eé]s\b|\bOrjuela\b", hit.group(0), flags=re.I):
                return True, f"nombre_completo:{field}"

            if MAURICIO_POLITICAL_CONTEXT_RE.search(around):
                return True, f"contexto_politico:{field}"

            if field == "titulo" and not domain.endswith(".cl"):
                return True, "nombre_en_titulo"

            # En notas colombianas la mención literal puede ser incidental,
            # pero también debe notificarse. Bloqueamos sitios chilenos
            # sin evidencia política concreta.
            colombian_domain = (
                domain.endswith(".co")
                or ".com.co" in domain
                or "/colombia/" in url.lower()
            )
            colombian_title = bool(re.search(
                r"\b(?:colombia|bogot[aá]|congreso\s+colombiano)\b",
                title, flags=re.I
            ))
            if (colombian_domain or colombian_title) and not domain.endswith(".cl"):
                return True, f"medio_colombiano:{field}"

    return False, "identidad_no_confirmada"


def article_mentions_mauricio(
    article: dict,
    mauricio_source: dict | None,
) -> bool:
    if not mauricio_source:
        return False

    # Mantener exclusiones de nombres inequívocamente distintos.
    combined = " ".join([
        safe_text(article.get("titulo")),
        safe_text(article.get("resumen_rss")),
        safe_text(article.get("texto_articulo")),
    ])
    for excluded in mauricio_source.get("exclusiones", []) or []:
        if contains_match_term(combined, excluded):
            return False

    return mauricio_person_evidence(article)[0]


def detect_trigger_term(
    article: dict,
    source_cfg: dict,
) -> str:

    combined = " ".join([
        safe_text(article.get("titulo")),
        safe_text(article.get("resumen_rss")),
        safe_text(article.get("texto_articulo")),
    ])

    # Para radares temáticos queremos mostrar
    # la palabra temática concreta.
    for term in source_cfg.get(
        "contexto_tematico",
        [],
    ):
        if contains_match_term(
            combined,
            term,
        ):
            return term

    # Para personas / partidos mostramos
    # el alias concreto encontrado.
    for alias in source_cfg.get(
        "aliases",
        [],
    ):
        if contains_match_term(
            combined,
            alias,
        ):
            return alias

    return ""

def summary_for_classification(title: str, summary: str, publisher: str) -> str:
    """
    Google News RSS often appends the publisher name to the summary.
    Do not let words in the publisher name (e.g. "El Ecosistema Startup")
    satisfy thematic aliases.
    """
    cleaned = safe_text(summary)
    pub = safe_text(publisher)

    if pub and cleaned.casefold().endswith(pub.casefold()):
        cleaned = cleaned[: len(cleaned) - len(pub)].rstrip(" -|–—")

    return cleaned


def classify_relevance(
    title: str,
    summary: str,
    aliases: list[str] | None = None,
    exclusions: list[str] | None = None,
    contexto_requerido: list[str] | None = None,
    contexto_tematico: list[str] | None = None,
    aceptar_match_rss: bool = False,
    body_text: str = "",
) -> tuple[bool, str]:

    combined = normalize_for_match(
        f"{title} {summary} {body_text}"
    )

    aliases = aliases or []
    exclusions = exclusions or []
    contexto_requerido = (
        contexto_requerido or []
    )
    contexto_tematico = (
        contexto_tematico or []
    )

    # 1. Exclusiones tienen prioridad.
    for excluded in exclusions:
        excluded_norm = normalize_for_match(
            excluded
        )

        if (
            excluded_norm
            and excluded_norm in combined
        ):
            return (
                False,
                f"exclusion_detectada:{excluded}",
            )

    # 2. Debe aparecer al menos un alias.
    matched_alias = None

    for alias in aliases:
        alias_norm = normalize_for_match(alias)

        if (
            alias_norm
            and alias_norm in combined
        ):
            matched_alias = alias
            break

    matched_by_rss = False

    if not matched_alias:
        if aceptar_match_rss:
            matched_by_rss = True
        else:
            return (
                False,
                "ningun_alias_detectado",
            )

    # 3. Algunas fuentes requieren además
    # contexto geográfico o institucional.
    if contexto_requerido:
        context_found = False

        for context_term in contexto_requerido:
            if contains_match_term(
                combined,
                context_term,
            ):
                context_found = True
                break

        if not context_found:
            return (
                False,
                "sin_contexto_requerido",
            )

    # 4. Los radares temáticos pueden exigir
    # además que la noticia realmente trate
    # sobre el asunto monitoreado.
    if contexto_tematico:
        thematic_found = False

        for thematic_term in contexto_tematico:
            if contains_match_term(
                combined,
                thematic_term,
            ):
                thematic_found = True
                break

        if not thematic_found:
            return (
                False,
                "sin_contexto_tematico",
            )

    if matched_by_rss:
        return (
            True,
            "match_rss_google_news",
        )

    return (
        True,
        f"alias_detectado:{matched_alias}",
    )


def is_operational_date(published_dt: datetime) -> bool:
    return published_dt.astimezone(CO_TZ).date() >= MONITORING_START_DATE


def parse_existing_datetime(value: str):
    value = safe_text(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def source_allows_telegram_for_article(
    source_cfg: dict,
    article: dict,
) -> bool:

    if not source_cfg.get(
        "enviar_telegram",
        True,
    ):
        return False

    telegram_desde = safe_text(
        source_cfg.get(
            "telegram_desde"
        )
    )

    # Mauricio y Alianza Verde no tienen
    # telegram_desde: mantienen su comportamiento.
    if not telegram_desde:
        return True

    activation_dt = (
        parse_existing_datetime(
            telegram_desde
        )
    )

    article_dt = (
        parse_existing_datetime(
            article.get(
                "fecha_descarga_utc"
            )
        )
    )

    if (
        activation_dt is None
        or article_dt is None
    ):
        return False

    return article_dt >= activation_dt

def _truthy(value) -> bool:
    return safe_text(value).lower() == "true"


def _merge_article_rows(base: dict, incoming: dict) -> dict:
    """Fusiona dos filas de la misma historia conservando la información más útil."""
    if _truthy(incoming.get("telegram_sent")):
        base["telegram_sent"] = "true"
        if safe_text(incoming.get("telegram_sent_at")):
            base["telegram_sent_at"] = incoming.get("telegram_sent_at")

    if _truthy(incoming.get("relevante")):
        base["relevante"] = "true"
    if _truthy(incoming.get("es_operativa")):
        base["es_operativa"] = "true"

    prefer_longer = {"texto_articulo", "resumen_rss", "motivo_relevancia"}
    prefer_nonempty = {
        "url_original", "google_entry_id", "fecha_publicacion_utc",
        "fecha_descarga_utc", "titulo", "fuente", "enlace",
        "source_type", "story_key",
    }

    for key in prefer_longer:
        old = safe_text(base.get(key))
        new = safe_text(incoming.get(key))
        if len(new) > len(old):
            base[key] = incoming.get(key)

    for key in prefer_nonempty:
        if not safe_text(base.get(key)) and safe_text(incoming.get(key)):
            base[key] = incoming.get(key)

    return base


def sanitize_loaded_state(articles: list[dict], matches: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Deduplica el estado histórico y remapea matches sin perder referencias."""
    stats = {
        "articles_before": len(articles),
        "articles_after": 0,
        "matches_before": len(matches),
        "matches_after": 0,
        "duplicate_articles": 0,
        "duplicate_matches": 0,
        "remapped_article_ids": 0,
    }

    canonical_articles: list[dict] = []
    by_id: dict[str, dict] = {}
    by_story: dict[str, dict] = {}
    remap: dict[str, str] = {}

    for row in articles:
        article_id = safe_text(row.get("article_id"))
        story_key = safe_text(row.get("story_key"))

        existing = by_id.get(article_id) if article_id else None
        if existing is None and story_key:
            candidate = by_story.get(story_key)
            if candidate is not None and same_publication_record(candidate, row):
                existing = candidate

        if existing is None:
            canonical_articles.append(row)
            if article_id:
                by_id[article_id] = row
            if story_key:
                by_story[story_key] = row
            continue

        stats["duplicate_articles"] += 1
        canonical_id = safe_text(existing.get("article_id"))
        if article_id and canonical_id and article_id != canonical_id:
            remap[article_id] = canonical_id
            stats["remapped_article_ids"] += 1
        _merge_article_rows(existing, row)

    seen_matches = set()
    clean_matches: list[dict] = []
    for match in matches:
        old_id = safe_text(match.get("article_id"))
        new_id = remap.get(old_id, old_id)
        if new_id and new_id != old_id:
            match["article_id"] = new_id

        key = (
            new_id,
            safe_text(match.get("cliente_id")),
            safe_text(match.get("rss_id")),
        )
        if not new_id or key in seen_matches:
            stats["duplicate_matches"] += 1
            continue
        seen_matches.add(key)
        clean_matches.append(match)

    stats["articles_after"] = len(canonical_articles)
    stats["matches_after"] = len(clean_matches)
    return canonical_articles, clean_matches, stats


def build_matches_by_article(matches: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for match in matches:
        article_id = safe_text(match.get("article_id"))
        if article_id:
            result.setdefault(article_id, []).append(match)
    return result


def summarize_existing_flags(articles: list[dict]) -> dict:
    stats = {"relevant": 0, "irrelevant": 0, "operational": 0, "non_operational": 0}
    for row in articles:
        if _truthy(row.get("relevante")):
            stats["relevant"] += 1
        else:
            stats["irrelevant"] += 1
        if _truthy(row.get("es_operativa")):
            stats["operational"] += 1
        else:
            stats["non_operational"] += 1
    return stats


def refresh_existing_article_flags(
    articles: list[dict],
    matches: list[dict],
    sources: list[dict],
) -> dict:

    stats = {
        "relevant": 0,
        "irrelevant": 0,
        "operational": 0,
        "non_operational": 0,
    }

    source_by_rss = {
        source["rss_id"]: source
        for source in sources
    }

    matches_by_article = {}

    for match in matches:
        article_id = safe_text(
            match.get("article_id")
        )

        if not article_id:
            continue

        matches_by_article.setdefault(
            article_id,
            [],
        ).append(match)

    for row in articles:
        article_id = safe_text(
            row.get("article_id")
        )

        title = safe_text(
            row.get("titulo")
        )

        summary = safe_text(
            row.get("resumen_rss")
        )

        article_matches = (
            matches_by_article.get(
                article_id,
                [],
            )
        )

        relevant = False
        reasons = []

        for match in article_matches:
            rss_id = safe_text(
                match.get("rss_id")
            )

            source_cfg = source_by_rss.get(
                rss_id
            )

            if not source_cfg:
                continue

            match_relevant, reason = (
                classify_relevance(
                    title,
                    summary,
                    aliases=source_cfg.get(
                        "aliases",
                        [],
                    ),
                    exclusions=source_cfg.get(
                        "exclusiones",
                        [],
                    ),
                    contexto_requerido=(
                        source_cfg.get(
                            "contexto_requerido",
                            [],
                        )
                    ),
                    contexto_tematico=(
                        source_cfg.get(
                            "contexto_tematico",
                            [],
                        )
                    ),
                    aceptar_match_rss=(
                        source_cfg.get(
                            "aceptar_match_rss",
                            False,
                        )
                    ),
                    body_text=safe_text(
                        row.get("texto_articulo")
                    ),
                )
            )

            reasons.append(
                f"{rss_id}:{reason}"
            )

            if match_relevant:
                relevant = True

        row["relevante"] = (
            "true" if relevant else "false"
        )

        row["motivo_relevancia"] = (
            " | ".join(reasons)
            if reasons
            else safe_text(
                row.get(
                    "motivo_relevancia"
                )
            )
        )

        published_dt = (
            parse_existing_datetime(
                row.get(
                    "fecha_publicacion_utc"
                )
            )
        )

        operational = bool(
            published_dt is not None
            and is_operational_date(
                published_dt
            )
        )

        row["es_operativa"] = (
            "true"
            if operational
            else "false"
        )

        if relevant:
            stats["relevant"] += 1
        else:
            stats["irrelevant"] += 1

        if operational:
            stats["operational"] += 1
        else:
            stats["non_operational"] += 1

    return stats

def get_entry_datetime(entry):
    parsed = (
        getattr(entry, "published_parsed", None)
        or getattr(entry, "updated_parsed", None)
    )

    if parsed is None:
        return None

    try:
        ts = calendar.timegm(parsed)
        return datetime.fromtimestamp(
            ts,
            tz=timezone.utc,
        )
    except Exception:
        return None


def get_title_and_publisher(entry):
    raw_title = clean_html_text(
        getattr(entry, "title", "")
    )

    publisher = ""

    entry_source = getattr(entry, "source", None)
    if entry_source is not None:
        publisher = clean_html_text(
            getattr(entry_source, "title", "")
        )

    title = raw_title

    # Google News RSS suele venir como:
    # "Titular - Nombre del medio"
    if " - " in raw_title:
        possible_title, possible_publisher = (
            raw_title.rsplit(" - ", 1)
        )
        title = clean_html_text(possible_title)

        if not publisher:
            publisher = clean_html_text(
                possible_publisher
            )

    link = safe_text(
        getattr(entry, "link", "")
    )

    if not publisher and link:
        try:
            publisher = (
                urlparse(link)
                .netloc
                .replace("www.", "")
                .strip()
            )
        except Exception:
            publisher = ""

    return title, publisher


def build_article_id(entry, title: str, link: str):
    google_entry_id = (
        safe_text(getattr(entry, "id", ""))
        or safe_text(getattr(entry, "guid", ""))
        or link
    )

    # No incluimos cliente, término ni rss_id.
    # Una misma noticia detectada por varios feeds debe conservar
    # el mismo article_id.
    canonical = "|".join([
        google_entry_id.strip(),
        title.lower().strip(),
        link.strip(),
    ])

    article_id = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    return article_id, google_entry_id


def format_date_colombia(dt_utc: datetime) -> str:
    dt = dt_utc.astimezone(CO_TZ)

    months = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]

    return (
        f"{dt.day} de {months[dt.month - 1]} "
        f"de {dt.year}"
    )


def format_telegram(
    article: dict,
    source_cfg: dict,
) -> str:

    emoji = (
        safe_text(
            source_cfg.get("emoji")
        )
        or "🔵"
    )

    trigger_term = detect_trigger_term(
        article,
        source_cfg,
    )

    parts = [
        f"{emoji} {source_cfg['termino']}",
    ]

    if trigger_term:
        parts.append(
            f"🔎 Detectado: {trigger_term}"
        )

    parts.append(
        f"📰 {article['titulo']}"
    )

    if article.get("fuente"):
        parts.append(
            f"🗞 {article['fuente']}"
        )

    if article.get("_published_dt"):
        parts.append(
            "📅 "
            + format_date_colombia(
                article["_published_dt"]
            )
        )

    final_url = (
        safe_text(
            article.get("url_original")
        )
        or safe_text(
            article.get("enlace")
        )
    )

    if final_url:
        parts.append(
            f"🔗 {final_url}"
        )

    return "\n".join(parts)


# V9 | Rescate del apellido "Toro" sólo dentro del RSS específico de Mauricio,
# con bloqueo de usos evidentemente animales/taurinos.
TORO_FALSE_POSITIVE_PATTERNS = (
    r"\b(?:un|el|este|ese|aquel)\s+toro\b",
    r"\btoros?\s+(?:bravo|de lidia|ganadero|bovino)\b",
    r"\b(?:ganaderia|ganadería|ganadero|ganadera|bovino|bovina|corrida|corridas|tauromaquia|taurino|taurina)\b",
)

def is_contextual_toro_rescue(article, matches, matches_by_article=None):
    # V19: el apellido aislado no demuestra identidad. El RSS sirve para
    # descubrir una nota, pero nunca como evidencia para alertar.
    return mauricio_person_evidence(article)[0]


def match_mauricio_project_signal(
    title: str,
    summary: str,
    source_cfg: dict,
    published_dt: datetime | None = None,
) -> tuple[bool, str]:

    signals = source_cfg.get("mauricio_project_signals") or []
    if not signals:
        return False, ""

    haystack = normalize_for_match(
        f"{safe_text(title)} {safe_text(summary)}"
    )

    pub_date = None
    if published_dt is not None:
        try:
            pub_date = published_dt.astimezone(CO_TZ).date()
        except Exception:
            pub_date = None

    for signal in signals:
        signal_id = safe_text(signal.get("id"))
        if not signal_id:
            continue

        desde = safe_text(signal.get("desde"))
        hasta = safe_text(signal.get("hasta"))

        try:
            if pub_date is not None and desde:
                if pub_date < datetime.fromisoformat(desde).date():
                    continue
            if pub_date is not None and hasta:
                if pub_date > datetime.fromisoformat(hasta).date():
                    continue
        except Exception:
            pass

        for phrase in signal.get("frases_exactas") or []:
            if contains_match_term(haystack, phrase):
                return True, signal_id

        for pattern in signal.get("patrones") or []:
            groups = pattern.get("todos_los_grupos") or []
            if not groups:
                continue

            all_groups_ok = True
            for group in groups:
                if not any(
                    contains_match_term(haystack, term)
                    for term in (group or [])
                ):
                    all_groups_ok = False
                    break

            if all_groups_ok:
                return True, signal_id

    return False, ""


def article_mauricio_project_id(article: dict) -> str:
    reason = safe_text(article.get("motivo_relevancia"))
    match = re.search(r"(?:^|\|)\s*mauricio_proyecto:([a-zA-Z0-9_-]+)", reason)
    return safe_text(match.group(1)) if match else ""


def choose_telegram_source(
    article: dict,
    matches: list[dict],
    sources: list[dict],
    matches_by_article: dict[str, list[dict]] | None = None,
) -> dict | None:

    article_id = safe_text(
        article.get("article_id")
    )

    title = safe_text(
        article.get("titulo")
    )

    summary = safe_text(
        article.get("resumen_rss")
    )

    source_by_rss = {
        source["rss_id"]: source
        for source in sources
    }

    mauricio_source = (
        source_by_rss.get(
            "mauricio_toro"
        )
    )
  

    # V19: ni el proyecto ni un match RSS bastan para confirmar identidad.
    # Incluye todas las publicaciones con mención verificable al político.
    # V9: rescate contextual del apellido con bloqueo de falsos positivos obvios.
    if mauricio_source and is_contextual_toro_rescue(
        article, matches, matches_by_article=matches_by_article
    ):
        return mauricio_source

    # PRIORIDAD ABSOLUTA:
    # si Mauricio aparece en título, resumen,
    # URL original o cuerpo del artículo.
    if article_mentions_mauricio(
        article,
        mauricio_source,
    ):
        return mauricio_source

    relevant_sources = []

    article_matches = (
        matches_by_article.get(article_id, [])
        if matches_by_article is not None
        else matches
    )

    for match in article_matches:
        if (
            matches_by_article is None
            and safe_text(match.get("article_id")) != article_id
        ):
            continue

        rss_id = safe_text(
            match.get("rss_id")
        )

        source_cfg = source_by_rss.get(
            rss_id
        )

        if not source_cfg:
            continue

        # V9 FIX: el RSS mauricio_toro NO puede generar alerta sólo por
        # aceptar_match_rss. A esta altura ya fallaron tanto la mención
        # explícita como el rescate contextual de "Toro", así que se omite.
        if rss_id == "mauricio_toro":
            continue

        match_relevant, _ = classify_relevance(
            title,
            summary,
            aliases=source_cfg.get(
                "aliases",
                [],
            ),
            exclusions=source_cfg.get(
                "exclusiones",
                [],
            ),
            contexto_requerido=source_cfg.get(
                "contexto_requerido",
                [],
            ),
            contexto_tematico=source_cfg.get(
                "contexto_tematico",
                [],
            ),
            aceptar_match_rss=source_cfg.get(
                "aceptar_match_rss",
                False,
            ),
            body_text=safe_text(
                article.get("texto_articulo")
            ),
        )

        if match_relevant:
            relevant_sources.append(
                source_cfg
            )

    # Mauricio también gana si el propio RSS
    # de Mauricio produjo un match válido.
    for source_cfg in relevant_sources:
        if (
            source_cfg.get("rss_id")
            == "mauricio_toro"
        ):
            return source_cfg

    # Luego Alianza Verde general.
    for source_cfg in relevant_sources:
        if (
            source_cfg.get("rss_id")
            == "alianza_verde_general"
        ):
            return source_cfg

    # Finalmente cualquier RSS individual.
    for source_cfg in relevant_sources:
        if source_cfg.get(
            "enviar_telegram",
            True,
        ):
            return source_cfg

    return None

def detect_trigger_location(
    article: dict,
    trigger_term: str,
) -> str:

    if not trigger_term:
        return ""

    if contains_match_term(
        safe_text(article.get("titulo")),
        trigger_term,
    ):
        return "titulo"

    if contains_match_term(
        safe_text(article.get("resumen_rss")),
        trigger_term,
    ):
        return "resumen_rss"

    if contains_match_term(
        safe_text(article.get("texto_articulo")),
        trigger_term,
    ):
        return "cuerpo"

    return ""

def article_publication_key(article: dict) -> str:
    """Misma URL del mismo medio = misma publicación, aunque cambie titular."""
    url = safe_text(article.get("url_original"))
    if not url or "news.google.com" in url.lower():
        return ""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().split("@")[-1].split(":")[0]
        domain = re.sub(r"^(?:www\.|amp\.|m\.)", "", domain)
        path = parsed.path.lower().rstrip("/")
        path = re.sub(r"^/amp/", "/", path)
        path = re.sub(r"/amp$", "", path)
        path = re.sub(r"\.amp$", "", path)
        if not domain or not path or path == "/":
            return ""
        return f"{domain}{path}"
    except Exception:
        return ""


def telegram_guard_key(article: dict) -> str:
    publication = article_publication_key(article)
    if publication:
        return "publication:" + hashlib.sha256(publication.encode("utf-8")).hexdigest()
    entry_id = safe_text(article.get("google_entry_id"))
    if entry_id:
        return "rss_entry:" + hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
    link = safe_text(article.get("enlace"))
    if link:
        return "rss_link:" + hashlib.sha256(link.encode("utf-8")).hexdigest()
    article_id = safe_text(article.get("article_id"))
    return f"article:{article_id}" if article_id else ""


def load_telegram_alert_ledger() -> dict:
    try:
        state, _ = load_json_state(TELEGRAM_ALERT_LEDGER_KEY)
    except Exception as error:
        # Si no podemos leer la barrera durable, NO enviamos a ciegas.
        raise RuntimeError(
            f"No se pudo leer ledger de Telegram: {error}"
        ) from error

    entries = state.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"entries": entries}


def save_telegram_alert_ledger(ledger: dict) -> None:
    entries = ledger.get("entries") or {}
    # Acotamos el archivo conservando las entradas más recientes.
    ordered = sorted(
        entries.items(),
        key=lambda kv: safe_text((kv[1] or {}).get("updated_at_utc")),
    )
    if len(ordered) > TELEGRAM_ALERT_LEDGER_MAX:
        ordered = ordered[-TELEGRAM_ALERT_LEDGER_MAX:]
    ledger["entries"] = dict(ordered)
    save_json_state(TELEGRAM_ALERT_LEDGER_KEY, ledger)


def article_alert_age_hours(article: dict) -> float | None:
    published_dt = parse_existing_datetime(
        article.get("fecha_publicacion_utc")
    )
    if published_dt is None:
        return None
    return max(
        0.0,
        (utc_now() - published_dt.astimezone(timezone.utc)).total_seconds() / 3600.0,
    )


def ledger_record(
    ledger: dict,
    key: str,
    article: dict,
    status: str,
    error: str = "",
) -> None:
    now = utc_iso(utc_now())
    current = (ledger.get("entries") or {}).get(key) or {}
    first_claimed = safe_text(current.get("claimed_at_utc")) or now
    ledger.setdefault("entries", {})[key] = {
        "status": status,
        "claimed_at_utc": first_claimed,
        "updated_at_utc": now,
        "article_id": safe_text(article.get("article_id")),
        "story_key": safe_text(article.get("story_key")),
        "titulo": safe_text(article.get("titulo")),
        "url_original": safe_text(article.get("url_original")),
        "error": safe_text(error)[:500],
    }


def send_pending_telegrams(
    articles: list[dict],
    matches: list[dict],
    sources: list[dict],
    bootstrap: bool,
    new_article_ids: set[str],
    matches_by_article: dict[str, list[dict]] | None = None,
) -> dict:

    stats = {
        "telegram_sent": 0,
        "telegram_skipped_bootstrap": 0,
    }

    source_by_rss = {
        source["rss_id"]: source
        for source in sources
    }

    mauricio_source = (
        source_by_rss.get(
            "mauricio_toro"
        )
    )

    # V13: barrera durable pequeña. Si S3 no está disponible para leerla,
    # abortamos las alertas en vez de arriesgar una ráfaga duplicada.
    ledger = load_telegram_alert_ledger()
    ledger_dirty = False
    # Compatibilidad con claims anteriores basados en story_key: conservar
    # el bloqueo por article_id aun si ahora la clave usa URL/identificador RSS.
    legacy_by_article = {}
    for saved_entry in (ledger.get("entries") or {}).values():
        saved_id = safe_text((saved_entry or {}).get("article_id"))
        if saved_id:
            legacy_by_article[saved_id] = saved_entry

    # Última barrera contra duplicados:
    # la misma URL o entrada RSS ya enviada no se vuelve a mandar.
    # Dos medios con el mismo titular conservan alertas independientes.
    sent_alert_keys = set()
    sent_publication_keys = set()

    for row in articles:
        already_sent = (
            safe_text(
                row.get("telegram_sent")
            ).lower()
            == "true"
        )

        if not already_sent:
            continue

        story_key = safe_text(
            row.get("story_key")
        )

        if not story_key:
            published_dt = (
                parse_existing_datetime(
                    row.get(
                        "fecha_publicacion_utc"
                    )
                )
            )

            if published_dt is not None:
                story_key = build_story_key(
                    title=safe_text(
                        row.get("titulo")
                    ),
                    publisher=safe_text(
                        row.get("fuente")
                    ),
                    published_dt=published_dt,
                )

                row["story_key"] = story_key

        saved_guard = telegram_guard_key(row)
        if saved_guard:
            sent_alert_keys.add(saved_guard)
        url_key = article_publication_key(row)
        if url_key:
            sent_publication_keys.add(url_key)

    for article in articles:
        article_id = safe_text(
            article.get("article_id")
        )
        story_key = safe_text(
            article.get("story_key")
        )

        if not story_key:
            published_dt_for_key = (
                parse_existing_datetime(
                    article.get(
                        "fecha_publicacion_utc"
                    )
                )
            )

            if published_dt_for_key is not None:
                story_key = build_story_key(
                    title=safe_text(
                        article.get("titulo")
                    ),
                    publisher=safe_text(
                        article.get("fuente")
                    ),
                    published_dt=published_dt_for_key,
                )

                article["story_key"] = (
                    story_key
                )

        if not article_id:
            continue

        relevant = (
            safe_text(
                article.get("relevante")
            ).lower()
            == "true"
        )

        operational = (
            safe_text(
                article.get("es_operativa")
            ).lower()
            == "true"
        )

        already_sent = (
            safe_text(
                article.get("telegram_sent")
            ).lower()
            == "true"
        )

        if (
            not relevant
            or not operational
            or already_sent
        ):
            continue

        guard_key = telegram_guard_key(article)
        ledger_entry = ((ledger.get("entries") or {}).get(guard_key)
                        or legacy_by_article.get(article_id))
        if ledger_entry:
            print(
                "🛑 TELEGRAM LEDGER | ya procesada | "
                f"status={safe_text(ledger_entry.get('status'))} | "
                f"{article.get('titulo', '')}"
            )
            continue

        # Una URL publicada que ya generó Telegram no debe disparar
        # una segunda alerta porque el medio cambió el título.
        publication_key = article_publication_key(article)
        if publication_key and publication_key in sent_publication_keys:
            article["telegram_sent"] = "true"
            print(
                "🛑 TELEGRAM MISMA PUBLICACIÓN EVITADA | "
                f"{article.get('titulo', '')}"
            )
            continue

        # Puede existir otra fila histórica de la
        # misma noticia que ya fue enviada.
        if (
            guard_key
            and guard_key in sent_alert_keys
        ):
            article["telegram_sent"] = "true"

            print(
                "🛑 TELEGRAM DUPLICADO EVITADO | "
                f"{article.get('titulo', '')}"
            )

            continue
        
        # Primero verificamos si esta noticia tiene
        # algún match que realmente pueda producir
        # una alerta de Telegram.
        #
        # Los radares de calibración tienen
        # enviar_telegram=false, por lo que no tiene
        # sentido resolver URLs ni descargar artículos.
        preliminary_source = (
            choose_telegram_source(
                article=article,
                matches=matches,
                sources=sources,
                matches_by_article=matches_by_article,
            )
        )

        if not preliminary_source and mauricio_source:
            # Un proyecto o un match RSS es una pista para leer el cuerpo,
            # NO autorización para enviar una alerta.
            candidate_matches = (
                matches_by_article.get(article_id, [])
                if matches_by_article is not None else
                [m for m in matches if safe_text(m.get("article_id")) == article_id]
            )
            direct_rss_candidate = any(
                safe_text(m.get("rss_id")) == "mauricio_toro"
                for m in candidate_matches
            )
            if article_mauricio_project_id(article) or direct_rss_candidate:
                preliminary_source = mauricio_source

        if not preliminary_source:
            continue

        if not source_allows_telegram_for_article(
            preliminary_source,
            article,
        ):
            continue

        # Antes de decidir la etiqueta,
        # intentamos enriquecer la noticia.
        # Esto sólo se hace para alertas pendientes.
        if not article_mentions_mauricio(
            article,
            mauricio_source,
        ):
            google_url = safe_text(
                article.get("enlace")
            )

            original_url = safe_text(
                article.get("url_original")
            )

            if (
                not original_url
                and google_url
            ):
                original_url = (
                    decode_google_news_url(
                        google_url
                    )
                )

                if original_url:
                    article[
                        "url_original"
                    ] = original_url

            if (
                original_url
                and not safe_text(
                    article.get(
                        "texto_articulo"
                    )
                )
            ):
                article[
                    "texto_articulo"
                ] = fetch_article_text(
                    original_url
                )

        source_cfg = choose_telegram_source(
            article=article,
            matches=matches,
            sources=sources,
            matches_by_article=matches_by_article,
        )

        if not source_cfg:
            if article_mauricio_project_id(article):
                print(
                    "🚫 MAURICIO IDENTIDAD NO CONFIRMADA | "
                    f"proyecto={article_mauricio_project_id(article)} | "
                    f"{article.get('titulo', '')}"
                )
            continue

        # El enlace original puede haberse resuelto durante el enriquecimiento.
        # Volvemos a calcular la clave antes del envío.
        final_guard_key = telegram_guard_key(article)
        if final_guard_key != guard_key:
            guard_key = final_guard_key
            if (ledger.get("entries") or {}).get(guard_key):
                print(
                    "🛑 TELEGRAM LEDGER | misma publicación ya procesada | "
                    f"{article.get('titulo', '')}"
                )
                continue
        final_publication_key = article_publication_key(article)
        if final_publication_key and final_publication_key in sent_publication_keys:
            article["telegram_sent"] = "true"
            print(
                "🛑 TELEGRAM MISMA URL YA ENVIADA | "
                f"{article.get('titulo', '')}"
            )
            continue

        # V7: las alertas inmediatas de Google News quedan reservadas
        # exclusivamente para menciones directas de Mauricio Toro.
        # Los radares temáticos alimentan el brief programado, pero no
        # deben generar alertas individuales.
        if safe_text(source_cfg.get("rss_id")) != "mauricio_toro":
            print(
                "🧱 SOLO BRIEF | "
                f"categoria={source_cfg.get('termino')} | "
                f"rss={source_cfg.get('rss_id')} | "
                f"titulo={article.get('titulo', '')}"
            )
            continue

        trigger_term = detect_trigger_term(
            article,
            source_cfg,
        )
        trigger_location = detect_trigger_location(
            article,
            trigger_term,
        )

        rss_id = safe_text(
            source_cfg.get("rss_id")
        )

        is_alianza_source = (
            rss_id == "alianza_verde_general"
            or rss_id.startswith("alianza_verde_")
        )

        if (
            is_alianza_source
            and not trigger_term
        ):
            print(
                "🚫 ALIANZA VERDE SIN CONFIRMACIÓN | "
                f"rss={rss_id} | "
                f"titulo={article.get('titulo', '')}"
            )
            continue

        print(
            "🔎 DIAGNÓSTICO ALERTA | "
            f"categoria={source_cfg.get('termino')} | "
            f"rss={source_cfg.get('rss_id')} | "
            f"trigger={trigger_term or '[SOLO MATCH RSS]'} | "
            f"ubicacion={trigger_location or '-'} | "
            f"titulo={article.get('titulo', '')}"
        )

        if not source_allows_telegram_for_article(
            source_cfg,
            article,
        ):
            continue

        article_is_new = (
            article_id in new_article_ids
        )

        if (
            bootstrap
            and article_is_new
            and not TELEGRAM_ON_BOOTSTRAP
        ):
            stats[
                "telegram_skipped_bootstrap"
            ] += 1

            print(
                "🧱 BOOTSTRAP: noticia guardada "
                "sin enviar a Telegram"
            )

            continue

        if not BOT_TOKEN or not CHAT_ID:
            print(
                "⚠️ Telegram no configurado; "
                "noticia guardada en S3."
            )
            continue

        published_dt = (
            parse_existing_datetime(
                article.get(
                    "fecha_publicacion_utc"
                )
            )
        )

        if published_dt is not None:
            article["_published_dt"] = (
                published_dt
            )

        # V13: no enviamos alertas antiguas al reanudar un worker detenido.
        age_hours = article_alert_age_hours(article)
        if (
            age_hours is not None
            and age_hours > TELEGRAM_ALERT_MAX_AGE_HOURS
        ):
            if guard_key:
                ledger_record(
                    ledger,
                    guard_key,
                    article,
                    status="stale_skipped",
                )
                ledger_dirty = True
            print(
                "🧊 TELEGRAM ANTIGUA OMITIDA | "
                f"edad_h={age_hours:.1f} | "
                f"{article.get('titulo', '')}"
            )
            continue

        message = format_telegram(
            article,
            source_cfg,
        )

        # CLAIM DURABLE ANTES DEL SEND.
        # Si este put a S3 falla, no enviamos: preferimos una alerta pendiente
        # a una posible ráfaga duplicada.
        if guard_key:
            ledger_record(
                ledger,
                guard_key,
                article,
                status="claimed",
            )
            save_telegram_alert_ledger(ledger)
            ledger_dirty = False

        try:
            telegram_send_message(
                bot_token=BOT_TOKEN,
                chat_id=CHAT_ID,
                text=message,
            )
        except Exception as error:
            # Un timeout de red puede ocurrir DESPUÉS de que Telegram haya
            # aceptado el mensaje. Dejamos el claim como delivery_uncertain y
            # NO reintentamos automáticamente en ciclos futuros.
            if guard_key:
                ledger_record(
                    ledger,
                    guard_key,
                    article,
                    status="delivery_uncertain",
                    error=str(error),
                )
                try:
                    save_telegram_alert_ledger(ledger)
                    ledger_dirty = False
                except Exception as ledger_error:
                    print(
                        "❌ TELEGRAM LEDGER | no se pudo guardar estado incierto: "
                        f"{ledger_error}"
                    )
            print(
                "❌ Telegram sin confirmación; NO se reintentará automáticamente | "
                f"{error}"
            )
            continue

        # Telegram confirmó el envío. Primero cerramos el ledger pequeño.
        if guard_key:
            ledger_record(
                ledger,
                guard_key,
                article,
                status="sent",
            )
            save_telegram_alert_ledger(ledger)
            ledger_dirty = False

        article["telegram_sent"] = "true"

        if guard_key:
            sent_alert_keys.add(guard_key)
        if publication_key:
            sent_publication_keys.add(publication_key)

        article["telegram_sent_at"] = utc_iso(utc_now())

        # El checkpoint grande queda separado del envío. Si falla, el ledger
        # ya impide que la misma historia vuelva a salir en el próximo ciclo.
        try:
            clean_internal_fields(articles)
            save_state(
                articles=articles,
                matches=matches,
            )
        except Exception as state_error:
            print(
                "⚠️ S3 CHECKPOINT NEWS falló DESPUÉS de Telegram; "
                "ledger conserva deduplicación | "
                f"{state_error}"
            )

        stats["telegram_sent"] += 1

        print(
            "📨 Telegram enviado | "
            f"{source_cfg['termino']} | "
            f"{article.get('titulo', '')}"
        )

    if ledger_dirty:
        try:
            save_telegram_alert_ledger(ledger)
        except Exception as ledger_error:
            print(
                "⚠️ TELEGRAM LEDGER | no se pudo guardar cierre del ciclo: "
                f"{ledger_error}"
            )

    return stats

def same_publication_record(left: dict, right: dict) -> bool:
    """Un titular compartido nunca prueba que dos filas sean la misma publicación."""
    left_url, right_url = article_publication_key(left), article_publication_key(right)
    if left_url and right_url:
        return left_url == right_url
    left_entry, right_entry = safe_text(left.get("google_entry_id")), safe_text(right.get("google_entry_id"))
    if left_entry and right_entry and left_entry == right_entry:
        return True
    left_link, right_link = safe_text(left.get("enlace")), safe_text(right.get("enlace"))
    return bool(left_link and right_link and left_link == right_link)


def index_publication(row: dict, lookup: dict) -> None:
    # Tuplas con referencias a cadenas existentes: no copiar cuerpos ni URLs largas.
    for label, value in (("entry", safe_text(row.get("google_entry_id"))),
                         ("link", safe_text(row.get("enlace"))),
                         ("publication", article_publication_key(row))):
        if value:
            lookup.setdefault((label, value), row)


def find_existing_publication(article_id, entry_id, link, original_url,
                              by_id, lookup):
    existing = by_id.get(article_id)
    if existing is not None:
        return existing
    for label, value in (("entry", entry_id), ("link", link),
                         ("publication", article_publication_key({"url_original": original_url or link}))):
        if value:
            existing = lookup.get((label, value))
            if existing is not None:
                return existing
    return None


def capture_source_entries(source_cfg: dict) -> list:
    """Consulta secuencialmente, une publicaciones y registra las fechas recibidas."""
    merged = []
    seen = set()
    successful = 0
    for label, url in query_plan(source_cfg["url"], source_cfg["rss_id"]):
        response = fetch_feed(label, url)
        if response.error:
            print(f"⚠️ RSS CONSULTA FALLIDA | {label} | {response.error}")
            continue
        feed = feedparser.parse(response.body)
        entries = getattr(feed, "entries", []) or []
        if getattr(feed, "bozo", False):
            print(f"⚠️ RSS XML | {label} | {getattr(feed, 'bozo_exception', '')}")
            if not entries:
                continue
        successful += 1
        dates = [get_entry_datetime(entry) for entry in entries]
        valid = [dt for dt in dates if dt is not None]
        oldest = utc_iso(min(valid)) if valid else "sin fecha"
        newest = utc_iso(max(valid)) if valid else "sin fecha"
        print(f"📡 RSS CONSULTA | {label} | entradas={len(entries)} | "
              f"fecha_min_utc={oldest} | fecha_max_utc={newest}")
        for entry in entries:
            entry_key = (safe_text(getattr(entry, "id", ""))
                         or safe_text(getattr(entry, "link", ""))
                         or safe_text(getattr(entry, "title", "")))
            if entry_key in seen:
                continue
            seen.add(entry_key)
            merged.append(entry)
    if not successful:
        raise RuntimeError("Todas las consultas de esta fuente RSS fallaron")
    merged.sort(key=lambda entry: get_entry_datetime(entry)
                or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    print(f"RSS UNIFICADO | {source_cfg['rss_id']} | publicaciones_unicas={len(merged)}")
    return merged


def recent_enough_for_article_fetch(published_dt: datetime) -> bool:
    """Ahorra descargas antiguas, sin borrar filas ni alterar el límite de alertas."""
    if not is_operational_date(published_dt):
        return False
    age_hours = max(0.0, (utc_now() - published_dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
    return age_hours <= TELEGRAM_ALERT_MAX_AGE_HOURS


def process_source(
    source_cfg: dict,
    articles: list[dict],
    matches: list[dict],
    existing_article_ids: set[str],
    existing_match_keys: set[tuple[str, str, str]],
    bootstrap: bool,
    new_article_ids: set[str],
    article_by_story_key: dict[object, dict],
    article_by_id: dict[str, dict],
    matches_by_article: dict[str, list[dict]],
    mauricio_source: dict | None = None,
) -> dict:
    print("-" * 100)
    print(
        f"RSS | {source_cfg['cliente_nombre']} | "
        f"{source_cfg['termino']} | "
        f"{source_cfg['rss_id']}"
    )
    print(
        f"URL | {source_cfg['url']}"
    )

    entries = capture_source_entries(source_cfg)

    print(
        f"Entradas recibidas: {len(entries)}"
    )

    stats = {
        "entries": len(entries),
        "new_articles": 0,
        "new_matches": 0,
        "telegram_sent": 0,
        "telegram_skipped_bootstrap": 0,
    }

    # Primero viejas, luego nuevas.
    for entry in reversed(entries):
        title, publisher = (
            get_title_and_publisher(entry)
        )

        link = safe_text(
            getattr(entry, "link", "")
        )

        published_dt = get_entry_datetime(
            entry
        )

        if not title or not link:
            continue

        if published_dt is None:
            print(
                f"OMITIDA SIN FECHA | {title}"
            )
            continue

        article_id, google_entry_id = (
            build_article_id(
                entry=entry,
                title=title,
                link=link,
            )
        )
        story_key = build_story_key(
            title=title,
            publisher=publisher,
            published_dt=published_dt,
        )

        summary = clean_html_text(
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
        )
        summary_classification = summary_for_classification(
            title,
            summary,
            publisher,
        )

        operational = is_operational_date(
            published_dt
        )

        existing_pre = find_existing_publication(
            article_id, google_entry_id, link, "", article_by_id, article_by_story_key
        )
        article_is_new_pre = (
            existing_pre is None
        )

        relevant, relevance_reason = classify_relevance(
            title,
            summary_classification,
            aliases=source_cfg.get(
                "aliases",
                [],
            ),
            exclusions=source_cfg.get(
                "exclusiones",
                [],
            ),
            contexto_requerido=source_cfg.get(
                "contexto_requerido",
                [],
            ),
            contexto_tematico=source_cfg.get(
                "contexto_tematico",
                [],
            ),
            aceptar_match_rss=source_cfg.get(
                "aceptar_match_rss",
                False,
            ),
        )
        # V17: proyectos configurados de Mauricio.
        # Esto evita depender de que Google News exponga "Mauricio Toro"
        # en el título/resumen o de que podamos resolver/descargar la URL.
        if not relevant:
            project_match, project_id = match_mauricio_project_signal(
                title,
                summary_classification,
                source_cfg,
                published_dt=published_dt,
            )
            if project_match:
                relevant = True
                relevance_reason = f"mauricio_proyecto:{project_id}"
                print(
                    "🟣 MAURICIO RESCATADO POR PROYECTO | "
                    f"{project_id} | {title}"
                )

        # Reutilizar cuerpos guardados evita volver a descargar notas de años
        # anteriores. La decisión de identidad sigue las reglas V6.
        cached = existing_pre or {}
        pre_resolved_url = safe_text(cached.get("url_original"))
        pre_article_text = safe_text(cached.get("texto_articulo"))
        allow_recent_fetch = recent_enough_for_article_fetch(published_dt)
        read_pending = False
        read_pending_reason = ""

        if source_cfg.get("rss_id") == "mauricio_toro":
            evidence_article = {
                "titulo": title, "resumen_rss": summary_classification,
                "url_original": pre_resolved_url, "texto_articulo": pre_article_text,
            }
            confirmed = article_mentions_mauricio(evidence_article, mauricio_source)
            # Las exclusiones de identidad no requieren descargar la página.
            excluded = any(contains_match_term(
                f"{title} {summary_classification}", excluded_name
            ) for excluded_name in source_cfg.get("exclusiones", []) or [])
            read_attempt_allowed = retry_due(safe_text(cached.get("motivo_relevancia")), utc_now().timestamp())
            if not confirmed and not excluded and not pre_article_text and allow_recent_fetch and read_attempt_allowed:
                if not pre_resolved_url:
                    pre_resolved_url = decode_google_news_url(link)
                if pre_resolved_url:
                    pre_article_text = fetch_article_text(pre_resolved_url)
                evidence_article.update(url_original=pre_resolved_url,
                                        texto_articulo=pre_article_text)
                confirmed = article_mentions_mauricio(evidence_article, mauricio_source)
            relevant = confirmed
            evidence = mauricio_person_evidence(evidence_article)[1]
            relevance_reason = ("mauricio_identidad:" + evidence
                                if confirmed else "mauricio_identidad_no_confirmada")
            if (not confirmed and not excluded and not pre_article_text
                    and allow_recent_fetch and supported_url(pre_resolved_url)):
                read_pending = True
                read_pending_reason = (pending_reason(ARTICLE_READ_STATUS.get(pre_resolved_url, "sin_cuerpo"), utc_now().timestamp())
                                       if read_attempt_allowed else safe_text(cached.get("motivo_relevancia")))
                relevance_reason = read_pending_reason
            if confirmed and pre_article_text and article_is_new_pre:
                print(f"🟣 MAURICIO CONFIRMADO | {evidence} | {title}")

        # Conservar el rescate desde RSS temáticos de V6, verificando identidad.
        if (not relevant and article_is_new_pre and operational
                and source_cfg.get("rss_id") != "mauricio_toro" and mauricio_source
                and allow_recent_fetch):
            if not pre_resolved_url:
                pre_resolved_url = decode_google_news_url(link)
            if pre_resolved_url and not pre_article_text:
                pre_article_text = fetch_article_text(pre_resolved_url)
            evidence_article = {"titulo": title, "resumen_rss": summary_classification,
                                "url_original": pre_resolved_url,
                                "texto_articulo": pre_article_text}
            if article_mentions_mauricio(evidence_article, mauricio_source):
                relevant = True
                relevance_reason = "mauricio_cuerpo:" + mauricio_person_evidence(evidence_article)[1]
                print(f"🟣 MAURICIO RESCATADO DESDE RSS TEMÁTICO | {title}")

        # Una URL resuelta puede identificar una publicación ya guardada con
        # otro GUID/titular. Nunca reutilizar por coincidencia de titular/fecha.
        if (existing_pre is None and relevant and allow_recent_fetch
                and not pre_resolved_url
                and (source_cfg.get("enviar_telegram", True)
                     or source_cfg.get("incluir_brief", False))):
            pre_resolved_url = decode_google_news_url(link)
        existing_article = find_existing_publication(
            article_id, google_entry_id, link, pre_resolved_url,
            article_by_id, article_by_story_key,
        )

        article_is_new = (
            existing_article is None
        )

        if article_is_new:
            now_dt = utc_now()

            # Resolver la URL original al momento de ingresar una noticia
            # relevante que pueda alimentar Telegram o el brief.
            # Antes esto ocurría sólo dentro de send_pending_telegrams(),
            # por lo que las fuentes con enviar_telegram=false conservaban
            # permanentemente la URL de Google News.
            original_url = pre_resolved_url

            should_resolve_url = (
                relevant
                and (
                    source_cfg.get("enviar_telegram", True)
                    or source_cfg.get("incluir_brief", False)
                )
            )

            if should_resolve_url and not original_url:
                original_url = decode_google_news_url(link)

                if original_url:
                    print(
                        "🔗 URL RESUELTA | "
                        f"{publisher or 'sin fuente'} | "
                        f"{original_url}"
                    )
                else:
                    print(
                        "⚠️ URL NO RESUELTA | "
                        f"{publisher or 'sin fuente'} | "
                        f"{link}"
                    )

            article = {
                "article_id": article_id,
                "story_key": story_key,
                "url_original": original_url,
                "texto_articulo": pre_article_text,
                "google_entry_id": google_entry_id,
                "fecha_publicacion_utc": utc_iso(
                    published_dt
                ),
                "fecha_descarga_utc": utc_iso(
                    now_dt
                ),
                "titulo": title,
                "resumen_rss": summary,
                "fuente": publisher,
                "enlace": link,
                "source_type": source_cfg[
                    "source_type"
                ],
                "relevante": "true" if relevant else "false",
                "motivo_relevancia": relevance_reason,
                "es_operativa": "true" if operational else "false",
                "telegram_sent": "false",
                "telegram_sent_at": "",
                "_published_dt": published_dt,
            }

            articles.append(article)

            article_by_story_key[
                story_key
            ] = article

            article_by_id[
                article_id
            ] = article

            existing_article_ids.add(
                article_id
            )

            new_article_ids.add(
                article_id
            )

            stats["new_articles"] += 1

            status = "RELEVANTE" if relevant else ("PENDIENTE DE LECTURA" if read_pending else "NO RELEVANTE")
            scope = "OPERATIVA" if operational else "HISTÓRICA"
            print(
                f"✅ NUEVA NOTICIA | {status} | {scope} | "
                f"{utc_iso(published_dt)} | "
                f"{publisher or 'sin fuente'} | "
                f"{title}"
            )
        else:
            article = existing_article

            if article is not None:
                # Conservamos el article_id de la primera
                # copia registrada de esta historia.
                article_id = safe_text(
                    article.get("article_id")
                )

                article["_published_dt"] = (
                    published_dt
                )

                article["es_operativa"] = (
                    "true"
                    if operational
                    else "false"
                )

        if article is None:
            continue
        index_publication(article, article_by_story_key)
        if google_entry_id:
            article_by_story_key[("entry", google_entry_id)] = article
        if link:
            article_by_story_key[("link", link)] = article

        # Reparar únicamente la publicación que volvió a aparecer en el RSS;
        # nunca reclasificar ni reiniciar todo el histórico ni los envíos.
        if not article_is_new and source_cfg.get("rss_id") == "mauricio_toro":
            if pre_resolved_url and not safe_text(article.get("url_original")):
                article["url_original"] = pre_resolved_url
            if pre_article_text and not safe_text(article.get("texto_articulo")):
                article["texto_articulo"] = pre_article_text
            if safe_text(article.get("relevante")).lower() != "true":
                if read_pending:
                    article["motivo_relevancia"] = read_pending_reason
                elif pre_article_text:
                    article["motivo_relevancia"] = relevance_reason
            if (safe_text(article.get("relevante")).lower() != "true"
                    and article_mentions_mauricio(article, mauricio_source)):
                article["relevante"] = "true"
                article["motivo_relevancia"] = "mauricio_identidad_cache:" + mauricio_person_evidence(article)[1]

        # Reparación de registros históricos que todavía conservan
        # únicamente la URL de Google News. No usa OpenAI y deja de
        # ejecutarse para la noticia en cuanto url_original queda guardada.
        if (
            not article_is_new
            and relevant
            and not safe_text(article.get("url_original"))
            and (source_cfg.get("rss_id") != "mauricio_toro" or allow_recent_fetch)
        ):
            stored_google_url = (
                safe_text(article.get("enlace"))
                or link
            )

            if (
                stored_google_url
                and "news.google.com" in stored_google_url
                and (
                    source_cfg.get("enviar_telegram", True)
                    or source_cfg.get("incluir_brief", False)
                )
            ):
                repaired_url = decode_google_news_url(
                    stored_google_url
                )

                if repaired_url:
                    article["url_original"] = repaired_url
                    print(
                        "🔧 URL HISTÓRICA REPARADA | "
                        f"{publisher or 'sin fuente'} | "
                        f"{repaired_url}"
                    )
                else:
                    print(
                        "⚠️ URL HISTÓRICA NO RESUELTA | "
                        f"{publisher or 'sin fuente'} | "
                        f"{title}"
                    )

        if (
            not article_is_new
            and safe_text(
                article.get("google_entry_id")
            )
            != safe_text(google_entry_id)
        ):
            print(
                "🔁 DUPLICADO EDITORIAL UNIFICADO | "
                f"{publisher or 'sin fuente'} | "
                f"{title}"
            )

        match_key = (
            article_id,
            source_cfg["cliente_id"],
            source_cfg["rss_id"],
        )

        if (
            match_key
            not in existing_match_keys
        ):
            matches.append({
                "article_id": article_id,
                "cliente_id": source_cfg[
                    "cliente_id"
                ],
                "cliente_nombre": source_cfg[
                    "cliente_nombre"
                ],
                "termino": source_cfg[
                    "termino"
                ],
                "rss_id": source_cfg[
                    "rss_id"
                ],
                "source_type": source_cfg[
                    "source_type"
                ],
                "fecha_match_utc": utc_iso(
                    utc_now()
                ),
            })

            existing_match_keys.add(
                match_key
            )
            matches_by_article.setdefault(article_id, []).append(matches[-1])

            # V14: si una historia histórica adquiere un match nuevo,
            # reclasificamos sólo ESA historia con ESA fuente, no todo el histórico.
            if not article_is_new:
                incremental_relevant, incremental_reason = classify_relevance(
                    safe_text(article.get("titulo")),
                    safe_text(article.get("resumen_rss")),
                    aliases=source_cfg.get("aliases", []),
                    exclusions=source_cfg.get("exclusiones", []),
                    contexto_requerido=source_cfg.get("contexto_requerido", []),
                    contexto_tematico=source_cfg.get("contexto_tematico", []),
                    aceptar_match_rss=source_cfg.get("aceptar_match_rss", False),
                    body_text=safe_text(article.get("texto_articulo")),
                )
                if incremental_relevant:
                    article["relevante"] = "true"
                    previous_reason = safe_text(article.get("motivo_relevancia"))
                    tagged_reason = f"{source_cfg['rss_id']}:{incremental_reason}"
                    if tagged_reason not in previous_reason:
                        article["motivo_relevancia"] = (
                            f"{previous_reason} | {tagged_reason}".strip(" |")
                        )

            stats["new_matches"] += 1

        if read_pending:
            print(f"⏳ MAURICIO PENDIENTE DE LECTURA | reintento=30min | {relevance_reason} | {title}")
        elif article_is_new and not relevant:
            print(
                f"🚫 FILTRO: no se notificará | {relevance_reason}"
            )
        elif article_is_new and relevant and not operational:
            print(
                f"🗂️ HISTÓRICA: anterior al inicio operativo "
                f"{MONITORING_START_DATE.isoformat()}"
            )

    return stats


def clean_internal_fields(
    articles: list[dict],
) -> None:
    for row in articles:
        row.pop(
            "_published_dt",
            None,
        )


def run_once() -> None:
    sources = load_sources()

    log_memory("antes load_state")
    (
        articles,
        matches,
        bootstrap,
    ) = load_state()
    log_memory("después load_state")

    articles, matches, sanitize_stats = sanitize_loaded_state(articles, matches)
    print(
        "🧹 ESTADO SANEADO | "
        f"articulos={sanitize_stats['articles_before']}→{sanitize_stats['articles_after']} | "
        f"dup_articulos={sanitize_stats['duplicate_articles']} | "
        f"matches={sanitize_stats['matches_before']}→{sanitize_stats['matches_after']} | "
        f"dup_matches={sanitize_stats['duplicate_matches']} | "
        f"ids_remapeados={sanitize_stats['remapped_article_ids']}"
    )
    gc.collect()
    log_memory("después saneamiento")

    # V14: no reclasificamos todo el histórico en cada ciclo.
    # Las filas históricas conservan sus flags; nuevas/afectadas se actualizan incrementalmente.
    flag_stats = summarize_existing_flags(articles)
    matches_by_article = build_matches_by_article(matches)

    existing_article_ids = {
        safe_text(
            row.get("article_id")
        )
        for row in articles
        if safe_text(
            row.get("article_id")
        )
    }
    article_by_id = {
        safe_text(
            row.get("article_id")
        ): row
        for row in articles
        if safe_text(
            row.get("article_id")
        )
    }

    article_by_story_key = {}

    for row in articles:
        story_key = safe_text(
            row.get("story_key")
        )

        if not story_key:
            published_dt = (
                parse_existing_datetime(
                    row.get(
                        "fecha_publicacion_utc"
                    )
                )
            )

            if published_dt is not None:
                story_key = build_story_key(
                    title=safe_text(
                        row.get("titulo")
                    ),
                    publisher=safe_text(
                        row.get("fuente")
                    ),
                    published_dt=published_dt,
                )

                row["story_key"] = (
                    story_key
                )

        if story_key:
            article_by_story_key[
                story_key
            ] = row
        index_publication(row, article_by_story_key)

    existing_match_keys = {
        (
            safe_text(
                row.get("article_id")
            ),
            safe_text(
                row.get("cliente_id")
            ),
            safe_text(
                row.get("rss_id")
            ),
        )
        for row in matches
        if safe_text(
            row.get("article_id")
        )
    }

    new_article_ids = set()

    print("=" * 100)
    print(
        "MONITOREO GOOGLE NEWS | "
        "MAURICIO TORO"
    )
    print(
        f"Fuentes activas: {len(sources)}"
    )
    print(
        f"Noticias existentes: "
        f"{len(existing_article_ids)}"
    )
    print(
        f"Matches existentes: "
        f"{len(existing_match_keys)}"
    )
    print(
        f"Modo bootstrap: {bootstrap}"
    )
    print(
        f"Inicio operativo: {MONITORING_START_DATE.isoformat()}"
    )
    print(
        "Histórico clasificado | "
        f"relevantes={flag_stats['relevant']} | "
        f"no_relevantes={flag_stats['irrelevant']} | "
        f"operativas={flag_stats['operational']} | "
        f"historicas={flag_stats['non_operational']}"
    )
    print(
        f"Intervalo configurado: "
        f"{CHECK_INTERVAL_SECONDS}s"
    )
    print("=" * 100)

    totals = {
        "entries": 0,
        "new_articles": 0,
        "new_matches": 0,
        "telegram_sent": 0,
        "telegram_skipped_bootstrap": 0,
    }

    mauricio_source = next(
        (
            src for src in sources
            if safe_text(src.get("rss_id")) == "mauricio_toro"
        ),
        None,
    )

    for source_index, source_cfg in enumerate(sources, start=1):
        try:
            stats = process_source(
                source_cfg=source_cfg,
                articles=articles,
                matches=matches,
                existing_article_ids=(
                    existing_article_ids
                ),
                existing_match_keys=(
                    existing_match_keys
                ),
                bootstrap=bootstrap,
                new_article_ids=(
                    new_article_ids
                ),
                article_by_story_key=(
                    article_by_story_key
                ),
                article_by_id=(
                    article_by_id
                ),
                matches_by_article=matches_by_article,
                mauricio_source=mauricio_source,
            )

            for key in totals:
                totals[key] += stats[key]

            if CHECKPOINT_EVERY_SOURCES > 0 and source_index % CHECKPOINT_EVERY_SOURCES == 0:
                clean_internal_fields(articles)
                gc.collect()
                log_memory(f"antes checkpoint {source_index}/{len(sources)}")
                save_state(articles=articles, matches=matches)
                print(
                    f"💾 CHECKPOINT PARCIAL | fuente={source_index}/{len(sources)} | "
                    f"articulos={len(articles)} | matches={len(matches)}"
                )
                gc.collect()
                log_memory(f"checkpoint {source_index}/{len(sources)}")

        except Exception as error:
            print(
                f"❌ ERROR RSS "
                f"{source_cfg.get('rss_id')} "
                f"({source_cfg.get('cliente_nombre')}): "
                f"{error}"
            )

    # V14: resumen de flags sin segunda reclasificación global.
    final_flag_stats = summarize_existing_flags(articles)

    print(
        "Clasificación final | "
        f"relevantes={final_flag_stats['relevant']} | "
        f"no_relevantes={final_flag_stats['irrelevant']}"
    )
    gc.collect()
    log_memory("después Google News / antes Telegram")

    telegram_stats = send_pending_telegrams(
        articles=articles,
        matches=matches,
        sources=sources,
        bootstrap=bootstrap,
        new_article_ids=new_article_ids,
        matches_by_article=matches_by_article,
    )

    totals["telegram_sent"] = (
        telegram_stats["telegram_sent"]
    )

    totals[
        "telegram_skipped_bootstrap"
    ] = telegram_stats[
        "telegram_skipped_bootstrap"
    ]

    clean_internal_fields(
        articles
    )

    # S3 es la fuente de verdad.
    # Guardamos al final de cada revisión completa.
    save_state(
        articles=articles,
        matches=matches,
    )
    gc.collect()
    log_memory("después save_state final")

    print("=" * 100)
    print(
        "TOTAL | "
        f"entries={totals['entries']} | "
        f"nuevas={totals['new_articles']} | "
        f"matches_nuevos={totals['new_matches']} | "
        f"telegram={totals['telegram_sent']} | "
        f"bootstrap_sin_telegram="
        f"{totals['telegram_skipped_bootstrap']}"
    )
    print("=" * 100)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Etapa RSS aislada con las reglas originales V6."
    )
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args()
    print("ETAPA RSS V16 LECTURA | recientes + identidad V6 + lectura pública", flush=True)
    run_once()
    gc.collect()
    log_memory("fin proceso RSS V16 corregido")


if __name__ == "__main__":
    main()
