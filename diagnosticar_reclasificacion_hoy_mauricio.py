from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import google_news_worker_mauricio_v15_onecycle_calibrado as w


CO_TZ = ZoneInfo("America/Bogota")


def safe_text(value):
    return "" if value is None else str(value).strip()


def truthy(value):
    return safe_text(value).lower() == "true"


def parse_dt(value):
    value = safe_text(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    articles, matches, _ = w.load_state()
    sources = w.load_sources()

    source_by_rss = {
        safe_text(source.get("rss_id")): source
        for source in sources
    }

    matches_by_article = defaultdict(list)
    for match in matches:
        article_id = safe_text(match.get("article_id"))
        if article_id:
            matches_by_article[article_id].append(match)

    today = datetime.now(CO_TZ).date()

    today_articles = []
    flips_false_to_true = []
    flips_true_to_false = []
    unchanged_true = 0
    unchanged_false = 0

    print("=" * 110)
    print("DIAGNÓSTICO DE RECLASIFICACIÓN | HOY | NO ESCRIBE EN S3 | NO ENVÍA TELEGRAM")
    print(f"Fecha Colombia: {today.isoformat()}")
    print("=" * 110)

    for article in articles:
        published = parse_dt(article.get("fecha_publicacion_utc"))
        if published is None:
            continue

        if published.astimezone(CO_TZ).date() != today:
            continue

        today_articles.append(article)

        article_id = safe_text(article.get("article_id"))
        title = safe_text(article.get("titulo"))
        summary = safe_text(article.get("resumen_rss"))
        body = safe_text(article.get("texto_articulo"))

        old_relevant = truthy(article.get("relevante"))
        new_relevant = False
        reasons = []

        for match in matches_by_article.get(article_id, []):
            rss_id = safe_text(match.get("rss_id"))
            source_cfg = source_by_rss.get(rss_id)

            if not source_cfg:
                continue

            relevant, reason = w.classify_relevance(
                title,
                summary,
                aliases=source_cfg.get("aliases", []),
                exclusions=source_cfg.get("exclusiones", []),
                contexto_requerido=source_cfg.get("contexto_requerido", []),
                contexto_tematico=source_cfg.get("contexto_tematico", []),
                aceptar_match_rss=source_cfg.get("aceptar_match_rss", False),
                body_text=body,
            )

            reasons.append((rss_id, relevant, reason))

            if relevant:
                new_relevant = True

        if old_relevant is False and new_relevant is True:
            flips_false_to_true.append((article, reasons))
        elif old_relevant is True and new_relevant is False:
            flips_true_to_false.append((article, reasons))
        elif old_relevant and new_relevant:
            unchanged_true += 1
        else:
            unchanged_false += 1

    print(f"Artículos publicados hoy: {len(today_articles)}")
    print(f"FALSE → TRUE con reglas calibradas: {len(flips_false_to_true)}")
    print(f"TRUE → FALSE con reglas calibradas: {len(flips_true_to_false)}")
    print(f"TRUE → TRUE: {unchanged_true}")
    print(f"FALSE → FALSE: {unchanged_false}")
    print("=" * 110)

    if flips_false_to_true:
        print("\nRESCATADOS (FALSE → TRUE)")
        print("-" * 110)
        for article, reasons in flips_false_to_true:
            print(f"🟢 {safe_text(article.get('titulo'))}")
            print(f"   Medio: {safe_text(article.get('fuente'))}")
            print(f"   Publicación UTC: {safe_text(article.get('fecha_publicacion_utc'))}")
            print(f"   Motivo anterior: {safe_text(article.get('motivo_relevancia'))}")
            for rss_id, relevant, reason in reasons:
                if relevant:
                    print(f"   ✅ {rss_id}: {reason}")
                else:
                    print(f"   · {rss_id}: {reason}")
            print()

    if flips_true_to_false:
        print("\nATENCIÓN: TRUE → FALSE")
        print("-" * 110)
        for article, reasons in flips_true_to_false:
            print(f"🔴 {safe_text(article.get('titulo'))}")
            print(f"   Medio: {safe_text(article.get('fuente'))}")
            print(f"   Motivo anterior: {safe_text(article.get('motivo_relevancia'))}")
            for rss_id, relevant, reason in reasons:
                print(f"   · {rss_id}: {reason}")
            print()

    print("=" * 110)
    print("FIN DEL DIAGNÓSTICO")
    print("No se modificó S3. No se modificó Telegram. No se modificó el estado del brief.")
    print("=" * 110)


if __name__ == "__main__":
    main()
