import argparse
import calendar
import hashlib
import html
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
from dotenv import load_dotenv

from news_s3_store import load_state, save_state
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
                "contexto_requerido": (
                    rss.get("contexto_requerido") or []
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
            })

    return sources



def normalize_for_match(value: str) -> str:
    text = clean_html_text(value).lower()
    return re.sub(r"\s+", " ", text).strip()

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
            and result.get("status") is True
        ):
            return safe_text(
                result.get("decoded_url")
            )

    except Exception as error:
        print(
            f"⚠️ No se pudo resolver URL Google News: {error}"
        )

    return ""


def fetch_article_text(
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

        return clean_html_text(
            html_text
        )

    except Exception as error:
        print(
            f"⚠️ No se pudo leer artículo original: {error}"
        )

        return ""
def article_mentions_mauricio(
    article: dict,
    mauricio_source: dict | None,
) -> bool:

    if not mauricio_source:
        return False

    aliases = (
        mauricio_source.get(
            "aliases",
            [],
        )
        or []
    )

    exclusions = (
        mauricio_source.get(
            "exclusiones",
            [],
        )
        or []
    )

    title = safe_text(
        article.get("titulo")
    )

    summary = safe_text(
        article.get("resumen_rss")
    )

    original_url = safe_text(
        article.get("url_original")
    )

    body_text = safe_text(
        article.get("texto_articulo")
    )

    combined = normalize_for_match(
        " ".join([
            title,
            summary,
            original_url,
            body_text,
        ])
    )

    # Exclusiones primero.
    for excluded in exclusions:
        excluded_norm = normalize_for_match(
            excluded
        )

        if (
            excluded_norm
            and excluded_norm in combined
        ):
            return False

    for alias in aliases:
        alias_norm = normalize_for_match(
            alias
        )

        if (
            alias_norm
            and alias_norm in combined
        ):
            return True

    return False

def classify_relevance(
    title: str,
    summary: str,
    aliases: list[str] | None = None,
    exclusions: list[str] | None = None,
    contexto_requerido: list[str] | None = None,
    aceptar_match_rss: bool = False,
) -> tuple[bool, str]:

    combined = normalize_for_match(
        f"{title} {summary}"
    )

    aliases = aliases or []
    exclusions = exclusions or []
    contexto_requerido = (
        contexto_requerido or []
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

    if not matched_alias:
        if aceptar_match_rss:
            return True, "match_rss_google_news"
        return False, "ningun_alias_detectado"

    # 3. Algunas fuentes, como Alianza Verde,
    # requieren además contexto para evitar
    # homónimos o usos no políticos.
    if contexto_requerido:
        context_found = False

        for context_term in contexto_requerido:
            context_norm = normalize_for_match(
                context_term
            )

            if (
                context_norm
                and context_norm in combined
            ):
                context_found = True
                break

        if not context_found:
            return (
                False,
                "sin_contexto_requerido",
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
                    aceptar_match_rss=(
                        source_cfg.get(
                            "aceptar_match_rss",
                            False,
                        )
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

    parts = [
        f"{emoji} {source_cfg['termino']}",
        f"📰 {article['titulo']}",
    ]

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

def choose_telegram_source(
    article: dict,
    matches: list[dict],
    sources: list[dict],
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
  

    # PRIORIDAD ABSOLUTA:
    # si Mauricio aparece en título, resumen,
    # URL original o cuerpo del artículo.
    if article_mentions_mauricio(
        article,
        mauricio_source,
    ):
        return mauricio_source

    relevant_sources = []

    for match in matches:
        if (
            safe_text(match.get("article_id"))
            != article_id
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
            aceptar_match_rss=source_cfg.get(
                "aceptar_match_rss",
                False,
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

def send_pending_telegrams(
    articles: list[dict],
    matches: list[dict],
    sources: list[dict],
    bootstrap: bool,
    new_article_ids: set[str],
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

    # Última barrera contra duplicados:
    # si cualquier copia editorial de una historia
    # ya fue enviada, no volvemos a mandarla.
    sent_story_keys = set()

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

        if story_key:
            sent_story_keys.add(
                story_key
            )

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
        # Puede existir otra fila histórica de la
        # misma noticia que ya fue enviada.
        if (
            story_key
            and story_key in sent_story_keys
        ):
            article["telegram_sent"] = "true"

            print(
                "🛑 TELEGRAM DUPLICADO EVITADO | "
                f"{article.get('titulo', '')}"
            )

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
        )

        if not source_cfg:
            continue

        if not source_cfg.get(
            "enviar_telegram",
            True,
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

        try:
            message = format_telegram(
                article,
                source_cfg,
            )

            telegram_send_message(
                bot_token=BOT_TOKEN,
                chat_id=CHAT_ID,
                text=message,
            )

            article["telegram_sent"] = (
                "true"
            )

            if story_key:
                sent_story_keys.add(
                    story_key
                )

            article[
                "telegram_sent_at"
            ] = utc_iso(
                utc_now()
            )

            stats["telegram_sent"] += 1

            print(
                "📨 Telegram enviado | "
                f"{source_cfg['termino']} | "
                f"{article.get('titulo', '')}"
            )

        except Exception as error:
            print(
                "❌ Telegram falló; "
                f"noticia quedó guardada: {error}"
            )

    return stats

def process_source(
    source_cfg: dict,
    articles: list[dict],
    matches: list[dict],
    existing_article_ids: set[str],
    existing_match_keys: set[tuple[str, str, str]],
    bootstrap: bool,
    new_article_ids: set[str],
    article_by_story_key: dict[str, dict],
    article_by_id: dict[str, dict],
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

    feed = feedparser.parse(
        source_cfg["url"]
    )

    if getattr(feed, "bozo", False):
        print(
            "⚠️ RSS bozo:",
            getattr(feed, "bozo_exception", ""),
        )

    entries = list(
        getattr(feed, "entries", []) or []
    )

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

        relevant, relevance_reason = classify_relevance(
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
            aceptar_match_rss=source_cfg.get(
                "aceptar_match_rss",
                False,
            ),
        )
        operational = is_operational_date(
            published_dt
        )

        existing_id_article = (
            article_by_id.get(
                article_id
            )
        )

        existing_story_article = (
            article_by_story_key.get(
                story_key
            )
        )

        existing_article = (
            existing_id_article
            or existing_story_article
        )

        article_is_new = (
            existing_article is None
        )

        if article_is_new:
            now_dt = utc_now()

            article = {
                "article_id": article_id,
                "story_key": story_key,
                "url_original": "",
                "texto_articulo": "",
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

            status = "RELEVANTE" if relevant else "NO RELEVANTE"
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

            stats["new_matches"] += 1

        if article_is_new and not relevant:
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

    (
        articles,
        matches,
        bootstrap,
    ) = load_state()

    flag_stats = refresh_existing_article_flags(
        articles,
        matches,
        sources,
    )

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

    for source_cfg in sources:
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
            )

            for key in totals:
                totals[key] += stats[key]

        except Exception as error:
            print(
                f"❌ ERROR RSS "
                f"{source_cfg.get('rss_id')} "
                f"({source_cfg.get('cliente_nombre')}): "
                f"{error}"
            )

    # Ahora que ya recorrimos TODOS los RSS,
    # recalculamos relevancia utilizando todos
    # los matches descubiertos en esta corrida.
    final_flag_stats = refresh_existing_article_flags(
        articles,
        matches,
        sources,
    )

    print(
        "Clasificación final | "
        f"relevantes={final_flag_stats['relevant']} | "
        f"no_relevantes={final_flag_stats['irrelevant']}"
    )

    telegram_stats = send_pending_telegrams(
        articles=articles,
        matches=matches,
        sources=sources,
        bootstrap=bootstrap,
        new_article_ids=new_article_ids,
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


def run_forever() -> None:
    while True:
        try:
            run_once()

        except KeyboardInterrupt:
            print(
                "Worker detenido por el usuario."
            )
            return

        except Exception as error:
            print(
                f"❌ ERROR EN CICLO: {error}"
            )

        time.sleep(
            CHECK_INTERVAL_SECONDS
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Google News Colombia -> "
            "S3/CSV + Telegram"
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Ejecuta una sola revisión "
            "y termina."
        ),
    )

    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()
