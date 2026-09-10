import argparse
import html
import json
import os
import re
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_DIR = Path(
    __file__
).resolve().parent

load_dotenv(
    BASE_DIR / ".env"
)


from telegram_utils import (
    telegram_send_message,
)

from youtube_s3_store import (
    load_youtube_state,
    save_youtube_state,
)


SOURCES_FILE = (
    BASE_DIR
    / os.getenv(
        "YOUTUBE_SOURCES_FILE",
        "youtube_sources_mauricio.json",
    )
)

API_KEY = os.getenv(
    "YOUTUBE_API_KEY",
    "",
).strip()

BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

CHAT_ID = (
    os.getenv(
        "TELEGRAM_CHAT_ID_MAURICIO",
        "",
    ).strip()
)


# Interruptor independiente para las alertas de YouTube.
# Por seguridad queda desactivado si la variable no existe.
YOUTUBE_TELEGRAM_ENABLED = (
    os.getenv(
        "YOUTUBE_TELEGRAM_ENABLED",
        "false",
    )
    .strip()
    .lower()
    in {"1", "true", "yes", "si", "sí"}
)


try:
    from zoneinfo import ZoneInfo

    CO_TZ = ZoneInfo(
        "America/Bogota"
    )

except Exception:
    CO_TZ = timezone(
        timedelta(hours=-5)
    )


def utc_now():
    return datetime.now(
        timezone.utc
    ).replace(
        microsecond=0
    )


def utc_iso(dt):
    return (
        dt.astimezone(
            timezone.utc
        )
        .replace(
            microsecond=0
        )
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def clean_text(value):
    text = html.unescape(
        "" if value is None
        else str(value)
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def normalize(value):
    return clean_text(
        value
    ).lower()


def contains_term(
    text,
    term,
):
    text_norm = normalize(
        text
    )

    term_norm = normalize(
        term
    )

    if not term_norm:
        return False

    if len(term_norm) <= 3:
        pattern = (
            r"(?<!\w)"
            + re.escape(
                term_norm
            )
            + r"(?!\w)"
        )

        return bool(
            re.search(
                pattern,
                text_norm,
                flags=re.UNICODE,
            )
        )

    return (
        term_norm
        in text_norm
    )


def load_sources():

    config = json.loads(
        SOURCES_FILE.read_text(
            encoding="utf-8"
        )
    )

    sources = [
        source
        for source
        in config.get(
            "sources",
            [],
        )
        if source.get(
            "active",
            True,
        )
    ]

    return config, sources


def detect_trigger(
    title,
    description,
    source,
):
    combined = (
        f"{title} {description}"
    )

    for term in source.get(
        "trigger_terms",
        [],
    ):
        if contains_term(
            combined,
            term,
        ):
            return term

    return ""


def classify_video(
    title,
    description,
    source,
):
    combined = (
        f"{title} {description}"
    )

    person_sources = {
        "youtube_abelardo",
        "youtube_mtoro",
        "youtube_carlos_galan",
    }

    exclusion_text = (
        title
        if source.get("id") in person_sources
        else combined
    )

    for excluded in source.get(
        "exclude_terms",
        [],
    ):
        if contains_term(
            exclusion_text,
            excluded,
        ):
            return (
                False,
                f"exclusion:{excluded}",
            )

    required = source.get(
        "required_terms",
        [],
    )

    if source.get("id") in person_sources:

        for term in required:
            if contains_term(
                title,
                term,
            ):
                return (
                    True,
                    f"required_title:{term}",
                )

        return (
            False,
            "sin_termino_requerido_en_titulo",
        )

    # Para temas sí permitimos título + descripción.
    if required:
        for term in required:
            if contains_term(
                combined,
                term,
            ):
                return (
                    True,
                    f"required:{term}",
                )

        return (
            False,
            "sin_termino_requerido",
        )

    trigger = detect_trigger(
        title,
        description,
        source,
    )

    if trigger:
        return (
            True,
            f"trigger:{trigger}",
        )

    return (
        False,
        "sin_trigger",
    )


def search_youtube(
    source,
    config,
    published_after,
):
    if not API_KEY:
        raise RuntimeError(
            "Falta YOUTUBE_API_KEY"
        )

    url = (
        "https://www.googleapis.com/"
        "youtube/v3/search"
    )

    params = {
        "part": "snippet",
        "q": source["query"],
        "type": "video",
        "order": "date",
        "maxResults": config.get(
            "max_results",
            25,
        ),
        "regionCode": config.get(
            "region_code",
            "CO",
        ),
        "relevanceLanguage": (
            config.get(
                "relevance_language",
                "es",
            )
        ),
        "publishedAfter": (
            published_after
        ),
        "key": API_KEY,
    }

    response = requests.get(
        url,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    return (
        response.json()
        .get(
            "items",
            [],
        )
    )


def format_date(
    value,
):
    try:
        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

        local = dt.astimezone(
            CO_TZ
        )

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
            f"{local.day} de "
            f"{months[local.month - 1]} "
            f"de {local.year}"
        )

    except Exception:
        return value

def youtube_alert_quality_gate(
    title,
    description,
    channel_title,
    source,
):
    """
    Segunda barrera:
    el video ya es relevante por tema/persona,
    pero aquí decidimos si tiene suficiente
    valor de asuntos públicos para Telegram.
    """

    title_norm = normalize(title)
    description_norm = normalize(description)
    channel_norm = normalize(channel_title)

    combined = f"{title_norm} {description_norm}"

    # Por ahora aplicamos esta exigencia
    # solamente a las búsquedas de personas.
    person_sources = {
        "youtube_abelardo",
        "youtube_mtoro",
        "youtube_carlos_galan",
    }

    if source.get("id") not in person_sources:
        return True, "tema_sin_quality_gate"

    # -------------------------------------------------
    # 1. FUENTES DE ALTO VALOR / CÍRCULO ROJO
    # -------------------------------------------------

    preferred_channels = [
        "el espectador",
        "el tiempo",
        "semana",
        "vanguardia",
        "pulzo",
        "caracol",
        "blu radio",
        "noticias rcn",
        "noticias uno",
        "cambio",
        "la silla vacía",
        "la silla vacia",
        "wradio",
        "la w",
        "rcn radio",
        "canal 1",
        "canal capital",
        "citytv",
        "red+",
        "red mas",
        "cablenoticias",
        "tercer canal",
        "ntn24",
        "canal institucional",
        "forbes colombia",
        "valora analitik",
        "la república",
        "la republica",
        "portafolio",
        "elespectador",
        "el colombiano",
        "el país",
        "el pais",
        "razón pública",
        "razon publica",
        "cuestión pública",
        "cuestion publica",
    ]

    if any(
        term in channel_norm
        for term in preferred_channels
    ):
        return True, "fuente_prioritaria"

    # -------------------------------------------------
    # 2. INSTITUCIONES, ACADEMIA, ONG, GREMIOS
    # -------------------------------------------------

    institutional_channel_terms = [
        "eps",
        "universidad",
        "fundación",
        "fundacion",
        "instituto",
        "observatorio",
        "centro de estudios",
        "cámara de comercio",
        "camara de comercio",
        "congreso",
        "senado",
        "cámara de representantes",
        "camara de representantes",
        "presidencia",
        "ministerio",
        "alcaldía",
        "alcaldia",
        "gobernación",
        "gobernacion",
        "procuraduría",
        "procuraduria",
        "contraloría",
        "contraloria",
        "fiscalía",
        "fiscalia",
        "defensoría",
        "defensoria",
        "corte constitucional",
        "consejo de estado",
        "onu",
        "naciones unidas",
        "oea",
        "bid",
        "banco mundial",
        "caf",
        "fedesarollo",
        "fedesarrollo",
        "andi",
        "fenalco",
        "moe",
        "misión de observación electoral",
        "mision de observacion electoral",
        "dejusticia",
        "transparencia por colombia",
        "human rights watch",
        "amnistía internacional",
        "amnistia internacional",
        "fundación para la libertad de prensa",
        "flip",
        "ideas para la paz",
        "fip",
        "cifras y conceptos",
        "invamer",
        "yanhaas",
        "cámara colombiana",
        "camara colombiana",
        "asobancaria",
        "acemi",
        "andi",
        "fenalco",
        "camacol",
    ]

    if any(
        term in channel_norm
        for term in institutional_channel_terms
    ):
        return True, "fuente_institucional"

    # -------------------------------------------------
    # 3. CONTENIDO SUSTANTIVO
    # -------------------------------------------------

    substantive_terms = [
        "decreto",
        "ley",
        "reforma",
        "congreso",
        "senado",
        "cámara",
        "camara",
        "corte",
        "fiscalía",
        "fiscalia",
        "procuraduría",
        "procuraduria",
        "contraloría",
        "contraloria",
        "investigación",
        "investigacion",
        "denuncia",
        "demanda",
        "fallo",
        "sentencia",
        "presupuesto",
        "contrato",
        "contratación",
        "contratacion",
        "licitación",
        "licitacion",
        "nombramiento",
        "ministro",
        "ministra",
        "gabinete",
        "política pública",
        "politica publica",
        "impuesto",
        "tributaria",
        "pensión",
        "pension",
        "pensiones",
        "fonpet",
        "reconstrucción",
        "reconstruccion",
        "terremoto",
        "seguridad",
        "extradición",
        "extradicion",
        "porte de armas",
        "relaciones internacionales",
        "marco rubio",
        "estados unidos",
        "encuesta",
        "aprobación",
        "aprobacion",
        "desaprobación",
        "desaprobacion",
        "gasto público",
        "gasto publico",
        "recursos públicos",
        "recursos publicos",
    ]

    substantive_hits = sum(
        1
        for term in substantive_terms
        if term in combined
    )

    # -------------------------------------------------
    # 4. SEÑALES DE CONTENIDO DE BAJO VALOR
    # -------------------------------------------------

    low_value_terms = [
        "meme",
        "memes",
        "humor",
        "parodia",
        "reaccionando",
        "reacción",
        "reaccion",
        "destrozó",
        "destrozo",
        "humilló",
        "humillo",
        "dejó callado",
        "dejo callado",
        "no vas a creer",
        "última hora",
        "ultima hora",
        "bombazo",
        "se robó",
        "se robo",
        "se robará",
        "se robara",
        "ateo",
    ]

    low_value_hits = sum(
        1
        for term in low_value_terms
        if term in title_norm
    )

    if low_value_hits >= 1 and substantive_hits < 2:
        return False, "contenido_bajo_valor"

    # Fuente desconocida:
    # sólo pasa si la pieza contiene
    # varias señales sustantivas.
    if substantive_hits >= 2:
        return True, f"contenido_sustantivo:{substantive_hits}"

    return False, "fuente_no_prioritaria_y_bajo_valor"

def format_telegram(
    video,
    category,
    trigger,
):
    parts = [
        f"▶️ YouTube | {category}",
    ]

    if trigger:
        parts.append(
            f"🔎 Detectado: {trigger}"
        )

    parts.extend([
        f"🎬 {video['title']}",
        f"📺 {video['channel_title']}",
        "📅 "
        + format_date(
            video["published_at"]
        ),
        f"🔗 {video['url']}",
    ])

    return "\n".join(
        parts
    )

def parse_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )
    except Exception:
        return None


def source_is_due(
    source,
    state,
):
    source_id = source["id"]

    source_states = state.get(
        "sources",
        {},
    )

    source_state = source_states.get(
        source_id,
        {},
    )

    last_run = parse_datetime(
        source_state.get(
            "last_successful_run"
        )
    )

    # Compatibilidad con el estado
    # que ya creamos anteriormente.
    if last_run is None:
        last_run = parse_datetime(
            state.get(
                "last_successful_run"
            )
        )

    if last_run is None:
        return True

    interval = int(
        source.get(
            "check_interval_seconds",
            10800,
        )
    )

    elapsed = (
        utc_now() - last_run
    ).total_seconds()

    return elapsed >= interval

def run_once(
    force=True,
):

    config, sources = (
        load_sources()
    )

    videos, matches, state = (
        load_youtube_state()
    )

    existing_video_ids = {
        row.get("video_id")
        for row in videos
        if row.get("video_id")
    }

    existing_matches = {
        (
            row.get("video_id"),
            row.get("source_id"),
        )
        for row in matches
    }

    bootstrap = not bool(
        state.get(
            "initialized"
        )
    )

    print("=" * 90)
    print(
        "MONITOREO YOUTUBE | "
        f"búsquedas activas="
        f"{len(sources)}"
    )
    print(
        f"Videos existentes="
        f"{len(existing_video_ids)}"
    )
    print(
        f"Bootstrap={bootstrap}"
    )
    print(
        f"Modo force={force}"
    )
    print("=" * 90)

    total_new = 0
    total_relevant = 0
    total_sent = 0
    total_searches = 0
    total_skipped = 0

    source_states = state.setdefault(
        "sources",
        {},
    )

    for source in sources:

        if (
            not force
            and not source_is_due(
                source,
                state,
            )
        ):
            total_skipped += 1

            print(
                f"⏳ YOUTUBE AÚN NO TOCA | "
                f"{source['category']}"
            )
            continue

        source_state = source_states.setdefault(
            source["id"],
            {},
        )

        last_source_run = parse_datetime(
            source_state.get(
                "last_successful_run"
            )
        )

        if last_source_run is None:
            last_source_run = parse_datetime(
                state.get(
                    "last_successful_run"
                )
            )

        if bootstrap:
            source_published_after_dt = (
                utc_now()
                - timedelta(
                    hours=24
                )
            )

        elif last_source_run is None:
            source_published_after_dt = (
                utc_now()
                - timedelta(
                    hours=3
                )
            )

        else:
            source_published_after_dt = (
                last_source_run
                - timedelta(
                    minutes=10
                )
            )

        source_published_after = utc_iso(
            source_published_after_dt
        )

        try:
            items = search_youtube(
                source,
                config,
                source_published_after,
            )

            total_searches += 1

            print(
                f"{source['category']} | "
                f"desde={source_published_after} | "
                f"resultados={len(items)}"
            )

            for item in reversed(
                items
            ):
                video_id = (
                    item.get(
                        "id",
                        {},
                    ).get(
                        "videoId",
                        "",
                    )
                )

                if not video_id:
                    continue

                snippet = item.get(
                    "snippet",
                    {},
                )

                title = clean_text(
                    snippet.get(
                        "title",
                        "",
                    )
                )

                description = clean_text(
                    snippet.get(
                        "description",
                        "",
                    )
                )

                trigger = (
                    detect_trigger(
                        title,
                        description,
                        source,
                    )
                )

                relevant, reason = (
                    classify_video(
                        title,
                        description,
                        source,
                    )
                )

                match_key = (
                    video_id,
                    source["id"],
                )

                if (
                    match_key
                    not in existing_matches
                ):
                    matches.append({
                        "video_id": video_id,
                        "source_id": source["id"],
                        "category": source["category"],
                        "trigger_term": trigger,
                        "matched_at": utc_iso(
                            utc_now()
                        ),
                    })

                    existing_matches.add(
                        match_key
                    )

                if (
                    video_id
                    in existing_video_ids
                ):
                    continue

                video = {
                    "video_id": video_id,
                    "title": title,
                    "description": description,
                    "channel_id": snippet.get(
                        "channelId",
                        "",
                    ),
                    "channel_title": clean_text(
                        snippet.get(
                            "channelTitle",
                            "",
                        )
                    ),
                    "published_at": snippet.get(
                        "publishedAt",
                        "",
                    ),
                    "url": (
                        "https://www.youtube.com/"
                        f"watch?v={video_id}"
                    ),
                    "downloaded_at": utc_iso(
                        utc_now()
                    ),
                    "relevant": (
                        "true"
                        if relevant
                        else "false"
                    ),
                    "relevance_reason": reason,
                    "telegram_sent": "false",
                    "telegram_sent_at": "",
                }

                videos.append(
                    video
                )

                existing_video_ids.add(
                    video_id
                )

                total_new += 1

                if not relevant:
                    print(
                        "🚫 YOUTUBE NO RELEVANTE | "
                        f"{reason} | {title}"
                    )
                    continue

                alertable, alert_reason = (
                    youtube_alert_quality_gate(
                        title=title,
                        description=description,
                        channel_title=video[
                            "channel_title"
                        ],
                        source=source,
                    )
                )

                if not alertable:
                    print(
                        "🟡 YOUTUBE RELEVANTE SIN ALERTA | "
                        f"{alert_reason} | "
                        f"{video['channel_title']} | "
                        f"{title}"
                    )
                    continue

                print(
                    "🎯 YOUTUBE ALERTABLE | "
                    f"{alert_reason} | "
                    f"{video['channel_title']} | "
                    f"{title}"
                )

                total_relevant += 1

                print(
                    "✅ YOUTUBE RELEVANTE | "
                    f"{source['category']} | "
                    f"trigger={trigger or '-'} | "
                    f"{title}"
                )

                if bootstrap:
                    continue

                if not YOUTUBE_TELEGRAM_ENABLED:
                    print(
                        "🧪 MODO PRUEBA | "
                        "Telegram YouTube desactivado."
                    )
                    continue

                if (
                    not BOT_TOKEN
                    or not CHAT_ID
                ):
                    print(
                        "⚠️ Telegram "
                        "no configurado."
                    )
                    continue

                message = (
                    format_telegram(
                        video,
                        source[
                            "category"
                        ],
                        trigger,
                    )
                )

                telegram_send_message(
                    bot_token=BOT_TOKEN,
                    chat_id=CHAT_ID,
                    text=message,
                )

                video[
                    "telegram_sent"
                ] = "true"

                video[
                    "telegram_sent_at"
                ] = utc_iso(
                    utc_now()
                )

                total_sent += 1

                print(
                    "📨 Telegram YouTube | "
                    f"{source['category']} | "
                    f"{title}"
                )

            source_state[
                "last_successful_run"
            ] = utc_iso(
                utc_now()
            )

            source_state[
                "check_interval_seconds"
            ] = int(
                source.get(
                    "check_interval_seconds",
                    10800,
                )
            )

        except Exception as error:
            print(
                "❌ ERROR YOUTUBE | "
                f"{source.get('id')} | "
                f"{error}"
            )

    state[
        "initialized"
    ] = True

    state[
        "last_successful_run"
    ] = utc_iso(
        utc_now()
    )

    state[
        "check_interval_seconds"
    ] = config.get(
        "check_interval_seconds",
        9000,
    )

    save_youtube_state(
        videos,
        matches,
        state,
    )

    print("=" * 90)
    print(
        "TOTAL YOUTUBE | "
        f"busquedas={total_searches} | "
        f"omitidas_por_intervalo={total_skipped} | "
        f"nuevos={total_new} | "
        f"relevantes={total_relevant} | "
        f"telegram={total_sent}"
    )
    print("=" * 90)

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--once",
        action="store_true",
    )

    args = parser.parse_args()

    if args.once:
        run_once(
            force=True
        )
    else:
        run_once(
            force=False
        )


if __name__ == "__main__":
    main()