import csv
import io
import os
from typing import Iterable

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", "").strip()

ARTICLES_S3_KEY = os.getenv(
    "MAURICIO_NEWS_ARTICLES_S3_KEY",
    "mauricio_toro/news/raw/news_articles.csv",
)

MATCHES_S3_KEY = os.getenv(
    "MAURICIO_NEWS_MATCHES_S3_KEY",
    "mauricio_toro/news/raw/news_matches.csv",
)

ARTICLE_FIELDS = [
    "article_id",
    "google_entry_id",
    "fecha_publicacion_utc",
    "fecha_descarga_utc",
    "titulo",
    "resumen_rss",
    "fuente",
    "enlace",
    "source_type",
    "relevante",
    "motivo_relevancia",
    "es_operativa",
    "telegram_sent",
    "telegram_sent_at",
]

MATCH_FIELDS = [
    "article_id",
    "cliente_id",
    "cliente_nombre",
    "termino",
    "rss_id",
    "source_type",
    "fecha_match_utc",
]

s3_client = boto3.client("s3", region_name=AWS_REGION)


def require_bucket() -> None:
    if not AWS_S3_BUCKET:
        raise RuntimeError("Falta AWS_S3_BUCKET en variables de entorno.")


def object_exists(key: str) -> bool:
    require_bucket()

    try:
        s3_client.head_object(Bucket=AWS_S3_BUCKET, Key=key)
        return True
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def load_csv(key: str) -> tuple[list[dict], bool]:
    """
    Devuelve (filas, existe_en_s3).
    """
    require_bucket()

    try:
        response = s3_client.get_object(
            Bucket=AWS_S3_BUCKET,
            Key=key,
        )
        raw = response["Body"].read().decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(raw)))
        return rows, True

    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return [], False
        raise


def save_csv(
    key: str,
    rows: Iterable[dict],
    fieldnames: list[str],
) -> None:
    require_bucket()

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        extrasaction="ignore",
    )
    writer.writeheader()

    for row in rows:
        writer.writerow({
            field: "" if row.get(field) is None else str(row.get(field))
            for field in fieldnames
        })

    s3_client.put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=buffer.getvalue().encode("utf-8-sig"),
        ContentType="text/csv; charset=utf-8",
    )

    print(f"☁️ S3 actualizado: s3://{AWS_S3_BUCKET}/{key}")


def load_state() -> tuple[list[dict], list[dict], bool]:
    """
    bootstrap=True cuando news_articles.csv todavía no existía.
    """
    articles, articles_exists = load_csv(ARTICLES_S3_KEY)
    matches, _ = load_csv(MATCHES_S3_KEY)
    bootstrap = not articles_exists
    return articles, matches, bootstrap


def save_state(
    articles: list[dict],
    matches: list[dict],
) -> None:
    articles_sorted = sorted(
        articles,
        key=lambda row: (
            row.get("fecha_publicacion_utc", ""),
            row.get("titulo", "").lower(),
        ),
    )

    matches_sorted = sorted(
        matches,
        key=lambda row: (
            row.get("fecha_match_utc", ""),
            row.get("cliente_id", ""),
            row.get("termino", "").lower(),
        ),
    )

    save_csv(
        ARTICLES_S3_KEY,
        articles_sorted,
        ARTICLE_FIELDS,
    )
    save_csv(
        MATCHES_S3_KEY,
        matches_sorted,
        MATCH_FIELDS,
    )
