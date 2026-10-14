import argparse
from datetime import datetime, timezone

from news_s3_store import load_state, save_state

TARGET_IDS = {
    "9c88b1530d492cf8405053de6a1a02c24fe75af3e466c7a216e4b884eb795d76",
    "b646dee602cdecd0524efc7bda87a34412ab23834129f973d4a6dae6cbcc42f2",
    "fcfcb116af83bdfcdfa11053f38f13d9a43175bbd106c770b4db0e52b14edc1b",
    "73de337228cf5cb968d61bb30f965570b504c30c6fb47445c368177295639383",
    "722a9599f61fedf5bacf30a9fd4e3649c2a92fb68e1a6d99c5b88a5a57d8b5aa",
}

REASON = "mauricio_proyecto:por_alla_si_voy"


def safe_text(v):
    return "" if v is None else str(v).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    articles, matches, bootstrap = load_state()
    found = []

    for article in articles:
        article_id = safe_text(article.get("article_id"))
        if article_id not in TARGET_IDS:
            continue

        found.append(article)
        print(
            f"{'APLICAR' if args.apply else 'DRY-RUN'} | "
            f"{safe_text(article.get('fuente'))} | "
            f"{safe_text(article.get('titulo'))}"
        )
        print(
            f"  relevante: {safe_text(article.get('relevante'))} -> true"
        )
        print(
            f"  motivo: {safe_text(article.get('motivo_relevancia'))} -> {REASON}"
        )
        print(
            f"  telegram_sent: {safe_text(article.get('telegram_sent'))}"
        )

        if args.apply:
            article["relevante"] = "true"
            article["motivo_relevancia"] = REASON
            # Se mantiene telegram_sent=false para que el worker normal
            # pueda enviar una sola alerta del proyecto mediante el ledger V3.

    missing = TARGET_IDS - {safe_text(x.get("article_id")) for x in found}

    print("=" * 90)
    print(f"Encontradas: {len(found)} / {len(TARGET_IDS)}")
    if missing:
        print("Faltantes:", ", ".join(sorted(missing)))

    if not args.apply:
        print("NO se escribió nada. Para aplicar: python -u reparar_alertas_por_alla_si_voy_20261014.py --apply")
        return

    if missing:
        raise RuntimeError("No se aplicó: faltan artículos objetivo en S3.")

    save_state(articles, matches)
    print("✅ S3 actualizado. El siguiente ciclo normal enviará como máximo UNA alerta del proyecto para el día Colombia.")


if __name__ == "__main__":
    main()
