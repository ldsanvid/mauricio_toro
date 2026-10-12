import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from google_news_worker_mauricio import (
    process_source,
    safe_text,
    clean_internal_fields,
)
from news_s3_store import load_state, save_state

load_dotenv()

TEST_GUID = "mauricio-toro-e2e-test-v4-2026-08-31"
TEST_TITLE = "Mauricio Toro presenta propuesta para impulsar el emprendimiento en Colombia"
TEST_LINK = "https://example.com/prueba-mauricio-toro-e2e-v4"
TEST_SOURCE = "Medio de Prueba"
TEST_SUMMARY = (
    "Mauricio Toro presentó una propuesta de prueba para verificar "
    "el sistema de monitoreo de medios."
)

published = datetime.now(timezone.utc).replace(microsecond=0)
published_rfc822 = published.strftime("%a, %d %b %Y %H:%M:%S +0000")

xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Prueba Mauricio Toro</title>
    <link>https://example.com/</link>
    <description>Feed local de prueba</description>
    <item>
      <title>{TEST_TITLE} - {TEST_SOURCE}</title>
      <link>{TEST_LINK}</link>
      <guid>{TEST_GUID}</guid>
      <pubDate>{published_rfc822}</pubDate>
      <description>{TEST_SUMMARY}</description>
    </item>
  </channel>
</rss>
'''

print("=" * 100)
print("PRUEBA END-TO-END V4 | MAURICIO TORO")
print("=" * 100)

articles, matches, bootstrap = load_state()
before_articles = len(articles)
before_matches = len(matches)

existing_article_ids = {
    safe_text(row.get("article_id"))
    for row in articles
    if safe_text(row.get("article_id"))
}
existing_match_keys = {
    (
        safe_text(row.get("article_id")),
        safe_text(row.get("cliente_id")),
        safe_text(row.get("rss_id")),
    )
    for row in matches
    if safe_text(row.get("article_id"))
}

with tempfile.NamedTemporaryFile(
    mode="w",
    suffix=".xml",
    encoding="utf-8",
    delete=False,
) as tmp:
    tmp.write(xml)
    feed_path = tmp.name

source_cfg = {
    "cliente_id": "mauricio_toro",
    "cliente_nombre": "Mauricio Toro",
    "rss_id": "e2e_test_v4",
    "termino": "Mauricio Toro",
    "aliases": ["Mauricio Toro"],
    "source_type": "test_e2e",
    "url": feed_path,
    "enviar_telegram": True,
}

telegram_ok = False

try:
    stats = process_source(
        source_cfg=source_cfg,
        articles=articles,
        matches=matches,
        existing_article_ids=existing_article_ids,
        existing_match_keys=existing_match_keys,
        bootstrap=False,
    )

    clean_internal_fields(articles)
    save_state(articles, matches)

    telegram_ok = stats["telegram_sent"] == 1

    print("-" * 100)
    print("RESULTADO")
    print(f"Nuevas: {stats['new_articles']}")
    print(f"Matches nuevos: {stats['new_matches']}")
    print(f"Telegram enviados: {stats['telegram_sent']}")

    if (
        stats["new_articles"] == 1
        and stats["new_matches"] == 1
        and telegram_ok
    ):
        print("✅ PRUEBA END-TO-END EXITOSA")
    else:
        print("⚠️ Prueba incompleta; revisa el error mostrado arriba.")

finally:
    test_article_ids = {
        safe_text(row.get("article_id"))
        for row in articles
        if (
            safe_text(row.get("google_entry_id")) == TEST_GUID
            or safe_text(row.get("enlace")) == TEST_LINK
        )
    }

    articles[:] = [
        row for row in articles
        if safe_text(row.get("article_id")) not in test_article_ids
    ]
    matches[:] = [
        row for row in matches
        if safe_text(row.get("article_id")) not in test_article_ids
    ]

    clean_internal_fields(articles)
    save_state(articles, matches)

    try:
        Path(feed_path).unlink(missing_ok=True)
    except Exception:
        pass

    print("-" * 100)
    print(
        f"🧹 S3 restaurado | "
        f"articles: {before_articles} -> {len(articles)} | "
        f"matches: {before_matches} -> {len(matches)}"
    )
    if telegram_ok:
        print("La alerta de prueba sí quedó visible en Telegram.")
    else:
        print("No se confirmó ningún envío de Telegram.")
    print("=" * 100)
