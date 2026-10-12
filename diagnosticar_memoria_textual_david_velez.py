import os
import unicodedata

from dotenv import load_dotenv
from news_s3_store import load_json_state

load_dotenv()

STATE_KEY = os.getenv(
    "MAURICIO_NEWS_BRIEF_STATE_S3_KEY",
    "mauricio_toro/news/state/news_brief_state.json",
)

TERMS = ("david", "velez", "nubank", "asesor")


def safe_text(value):
    return "" if value is None else str(value).strip()


def normalize(value):
    text = safe_text(value).lower()
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def matches(value):
    text = normalize(value)
    return any(term in text for term in TERMS)


def show_list(name, rows):
    rows = rows if isinstance(rows, list) else []
    hits = []

    for i, raw in enumerate(rows, start=1):
        if isinstance(raw, dict):
            # Por si alguna versión guardó objetos en vez de strings.
            searchable = " | ".join(safe_text(v) for v in raw.values())
        else:
            searchable = safe_text(raw)

        if matches(searchable):
            hits.append((i, raw))

    print("-" * 100)
    print(f"{name}: {len(rows)} elemento(s) totales | coincidencias={len(hits)}")
    for i, raw in hits:
        print(f"[{i}] {raw}")

    return hits


def main():
    state, exists = load_json_state(STATE_KEY)

    print("=" * 100)
    print("DIAGNÓSTICO | MEMORIA TEXTUAL | DAVID VÉLEZ")
    print(f"S3 key: {STATE_KEY}")
    print(f"Estado encontrado: {'SÍ' if exists else 'NO'}")
    print("=" * 100)

    if not exists:
        print("❌ No existe el estado del brief.")
        return

    title_hits = show_list("reported_titles", state.get("reported_titles", []))
    bullet_hits = show_list("reported_bullets", state.get("reported_bullets", []))
    event_hits = show_list("reported_events", state.get("reported_events", []))

    print("=" * 100)
    if title_hits or bullet_hits or event_hits:
        print("✅ Hay memoria histórica relacionada con David/Vélez/Nubank/asesor.")
    else:
        print("❌ No hay memoria histórica relacionada con David/Vélez/Nubank/asesor.")
    print("SOLO LECTURA: no se modificó S3, no se llamó a OpenAI y no se envió Telegram.")


if __name__ == "__main__":
    main()
