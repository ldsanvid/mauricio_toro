import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from news_s3_store import load_json_state

load_dotenv()

BRIEF_STATE_KEY = os.getenv(
    "MAURICIO_NEWS_BRIEF_STATE_S3_KEY",
    "mauricio_toro/news/state/news_brief_state.json",
)
EVENT_HISTORY_HOURS = int(os.getenv("MAURICIO_BRIEF_EVENT_HISTORY_HOURS", "72"))

TERMS = ("david", "velez", "vélez", "nubank", "asesor", "inteligencia artificial")


def safe_text(value):
    return "" if value is None else str(value).strip()


def normalize(value):
    text = safe_text(value).lower()
    return (
        text.replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
            .replace("ü", "u")
    )


def main():
    state, exists = load_json_state(BRIEF_STATE_KEY)

    print("=" * 100)
    print("DIAGNÓSTICO | MEMORIA DEL BRIEF | DAVID VÉLEZ")
    print(f"S3 key: {BRIEF_STATE_KEY}")
    print(f"Estado encontrado: {'SÍ' if exists else 'NO'}")
    print(f"Ventana configurada: {EVENT_HISTORY_HOURS} horas")
    print("=" * 100)

    events = state.get("reported_events", [])
    print(f"reported_events totales guardados: {len(events)}")

    now_utc = datetime.now(timezone.utc)
    matches = []

    for i, raw in enumerate(events, start=1):
        if not isinstance(raw, dict):
            continue

        event = safe_text(raw.get("event"))
        topic = safe_text(raw.get("topic"))
        ts = safe_text(raw.get("reported_at_utc"))
        haystack = normalize(f"{event} {topic}")

        if not any(normalize(term) in haystack for term in TERMS):
            continue

        age_hours = None
        active = False
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
            age_hours = (now_utc - dt).total_seconds() / 3600
            active = 0 <= age_hours <= EVENT_HISTORY_HOURS
        except Exception:
            pass

        matches.append((i, event, topic, ts, age_hours, active))

    if not matches:
        print("\n❌ No encontré ningún evento relacionado con David/Vélez/Nubank/asesor/IA.")
    else:
        print(f"\nCoincidencias encontradas: {len(matches)}")
        for i, event, topic, ts, age_hours, active in matches:
            print("-" * 100)
            print(f"Índice: {i}")
            print(f"Evento: {event}")
            print(f"Tema: {topic or '-'}")
            print(f"reported_at_utc: {ts or '-'}")
            if age_hours is None:
                print("Edad: NO CALCULABLE")
                print("¿Dentro de memoria activa?: NO DETERMINABLE")
            else:
                print(f"Edad aproximada: {age_hours:.2f} horas")
                print(f"¿Dentro de memoria activa?: {'SÍ' if active else 'NO'}")

    print("=" * 100)
    print("SOLO LECTURA: no se modificó S3, no se llamó a OpenAI y no se envió Telegram.")


if __name__ == "__main__":
    main()
