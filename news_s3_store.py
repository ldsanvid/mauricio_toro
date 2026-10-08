import csv
import io
import json
import os
import tempfile
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
    "story_key",
    "url_original",
    "texto_articulo",
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

    V2 memory-safe:
    Lee el objeto de S3 como stream de texto en lugar de crear primero
    una copia completa en bytes + otra copia completa como str + StringIO.
    """
    require_bucket()

    try:
        response = s3_client.get_object(
            Bucket=AWS_S3_BUCKET,
            Key=key,
        )

        body = response["Body"]
        text_stream = io.TextIOWrapper(
            body,
            encoding="utf-8-sig",
            newline="",
        )

        try:
            rows = list(csv.DictReader(text_stream))
        finally:
            text_stream.close()

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
    """
    V2 memory-safe:
    Escribe el CSV a un archivo temporal en disco y luego lo transmite
    a S3, evitando varias copias completas del CSV dentro de la RAM.
    """
    require_bucket()

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            suffix=".csv",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name

            writer = csv.DictWriter(
                temp_file,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()

            for row in rows:
                writer.writerow({
                    field: "" if row.get(field) is None else str(row.get(field))
                    for field in fieldnames
                })

        with open(temp_path, "rb") as binary_file:
            s3_client.upload_fileobj(
                binary_file,
                AWS_S3_BUCKET,
                key,
                ExtraArgs={
                    "ContentType": "text/csv; charset=utf-8",
                },
            )

        print(f"☁️ S3 actualizado: s3://{AWS_S3_BUCKET}/{key}")

    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


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
    """
    V2 memory-safe:
    Ordena las listas existentes in-place para evitar dos copias completas
    adicionales creadas por sorted(...).
    """
    articles.sort(
        key=lambda row: (
            row.get("fecha_publicacion_utc", ""),
            row.get("titulo", "").lower(),
        ),
    )

    matches.sort(
        key=lambda row: (
            row.get("fecha_match_utc", ""),
            row.get("cliente_id", ""),
            row.get("termino", "").lower(),
        ),
    )

    save_csv(
        ARTICLES_S3_KEY,
        articles,
        ARTICLE_FIELDS,
    )

    save_csv(
        MATCHES_S3_KEY,
        matches,
        MATCH_FIELDS,
    )


def load_json_state(key: str) -> tuple[dict, bool]:
    require_bucket()
    try:
        response = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=key)
        raw = response["Body"].read().decode("utf-8-sig")
        return json.loads(raw), True
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return {}, False
        raise


def save_json_state(key: str, payload: dict) -> None:
    require_bucket()
    s3_client.put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    print(f"☁️ S3 actualizado: s3://{AWS_S3_BUCKET}/{key}")
