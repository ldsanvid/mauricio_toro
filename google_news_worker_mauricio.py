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

EXCLUDED_NAME_PATTERNS = [
    "oscar mauricio toro",
    "óscar mauricio toro",
    "mauricio toro-goya",
    "mauricio toro goya",
]

TARGET_NAME_PATTERNS = [
    "mauricio toro",
    "mauricio andres toro",
    "mauricio andrés toro",
    "mauricio toro orjuela",
    "mauricio andres toro orjuela",
    "mauricio andrés toro orjuela",
]


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


def classify_relevance(title: str, summary: str) -> tuple[bool, str]:
    combined = normalize_for_match(f"{title} {summary}")

    for excluded in EXCLUDED_NAME_PATTERNS:
        if excluded in combined:
            return False, f"homonimo_excluido:{excluded}"

    for target in TARGET_NAME_PATTERNS:
        if target in combined:
            return True, "nombre_objetivo_detectado"

    return False, "nombre_objetivo_no_detectado"


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


def refresh_existing_article_flags(articles: list[dict]) -> dict:
    stats = {
        "relevant": 0,
        "irrelevant": 0,
        "operational": 0,
        "non_operational": 0,
    }

    for row in articles:
        relevant, reason = classify_relevance(
            safe_text(row.get("titulo")),
            safe_text(row.get("resumen_rss")),
        )
        row["relevante"] = "true" if relevant else "false"
        row["motivo_relevancia"] = reason

        published_dt = parse_existing_datetime(
            row.get("fecha_publicacion_utc")
        )
        operational = bool(
            published_dt is not None
            and is_operational_date(published_dt)
        )
        row["es_operativa"] = "true" if operational else "false"

        stats["relevant" if relevant else "irrelevant"] += 1
        stats["operational" if operational else "non_operational"] += 1

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
    parts = [
        f"🟣 {source_cfg['cliente_nombre']}",
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

    parts.append(
        f"🔎 Término: {source_cfg['termino']}"
    )

    if article.get("enlace"):
        parts.append(
            f"🔗 {article['enlace']}"
        )

    return "\n".join(parts)


def process_source(
    source_cfg: dict,
    articles: list[dict],
    matches: list[dict],
    existing_article_ids: set[str],
    existing_match_keys: set[tuple[str, str, str]],
    bootstrap: bool,
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

        summary = clean_html_text(
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
        )

        relevant, relevance_reason = classify_relevance(
            title,
            summary,
        )
        operational = is_operational_date(
            published_dt
        )

        article_is_new = (
            article_id not in existing_article_ids
        )

        if article_is_new:
            now_dt = utc_now()

            article = {
                "article_id": article_id,
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
            existing_article_ids.add(
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
            article = next(
                (
                    row
                    for row in articles
                    if row.get("article_id")
                    == article_id
                ),
                None,
            )

            if article is not None:
                article["_published_dt"] = (
                    published_dt
                )
                article["relevante"] = "true" if relevant else "false"
                article["motivo_relevancia"] = relevance_reason
                article["es_operativa"] = "true" if operational else "false"

        if article is None:
            continue

        already_sent = (
            safe_text(article.get("telegram_sent")).lower()
            == "true"
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

        should_send_telegram = (
            relevant
            and operational
            and not already_sent
            and source_cfg.get(
                "enviar_telegram",
                True,
            )
        )

        # Telegram se intenta para cualquier artículo relevante y operativo
        # que aún no haya quedado marcado como enviado.
        if should_send_telegram:
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
                article[
                    "telegram_sent_at"
                ] = utc_iso(
                    utc_now()
                )

                stats[
                    "telegram_sent"
                ] += 1

                print(
                    "📨 Telegram enviado"
                )

            except Exception as error:
                print(
                    "❌ Telegram falló; "
                    f"noticia quedó guardada: {error}"
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
        articles
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
