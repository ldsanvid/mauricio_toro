import csv
import io
import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(
    BASE_DIR / ".env"
)


AWS_REGION = os.getenv(
    "AWS_REGION",
    "us-east-2",
)

AWS_S3_BUCKET = os.getenv(
    "AWS_S3_BUCKET",
    "",
).strip()

VIDEOS_KEY = os.getenv(
    "MAURICIO_YOUTUBE_VIDEOS_S3_KEY",
    "mauricio_toro/youtube/raw/youtube_videos.csv",
).strip()

MATCHES_KEY = os.getenv(
    "MAURICIO_YOUTUBE_MATCHES_S3_KEY",
    "mauricio_toro/youtube/raw/youtube_matches.csv",
).strip()

STATE_KEY = os.getenv(
    "MAURICIO_YOUTUBE_STATE_S3_KEY",
    "mauricio_toro/youtube/state/youtube_state.json",
).strip()


VIDEO_FIELDS = [
    "video_id",
    "title",
    "description",
    "channel_id",
    "channel_title",
    "published_at",
    "url",
    "downloaded_at",
    "relevant",
    "relevance_reason",
    "telegram_sent",
    "telegram_sent_at",
]

MATCH_FIELDS = [
    "video_id",
    "source_id",
    "category",
    "trigger_term",
    "matched_at",
]


def get_s3():
    if not AWS_S3_BUCKET:
        raise RuntimeError(
            "Falta AWS_S3_BUCKET"
        )

    return boto3.client(
        "s3",
        region_name=AWS_REGION,
    )


def load_csv(
    key: str,
) -> list[dict]:

    try:
        response = get_s3().get_object(
            Bucket=AWS_S3_BUCKET,
            Key=key,
        )

        content = (
            response["Body"]
            .read()
            .decode("utf-8-sig")
        )

        return list(
            csv.DictReader(
                io.StringIO(content)
            )
        )

    except ClientError as error:
        code = (
            error.response
            .get("Error", {})
            .get("Code")
        )

        if code in {
            "NoSuchKey",
            "404",
        }:
            return []

        raise


def save_csv(
    key: str,
    rows: list[dict],
    fields: list[str],
) -> None:

    output = io.StringIO()

    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        extrasaction="ignore",
    )

    writer.writeheader()

    for row in rows:
        writer.writerow(row)

    get_s3().put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=output.getvalue().encode(
            "utf-8"
        ),
        ContentType="text/csv",
    )


def load_youtube_state():
    videos = load_csv(
        VIDEOS_KEY
    )

    matches = load_csv(
        MATCHES_KEY
    )

    try:
        response = get_s3().get_object(
            Bucket=AWS_S3_BUCKET,
            Key=STATE_KEY,
        )

        state = json.loads(
            response["Body"]
            .read()
            .decode("utf-8")
        )

    except ClientError as error:
        code = (
            error.response
            .get("Error", {})
            .get("Code")
        )

        if code in {
            "NoSuchKey",
            "404",
        }:
            state = {}
        else:
            raise

    return videos, matches, state


def save_youtube_state(
    videos: list[dict],
    matches: list[dict],
    state: dict,
) -> None:

    save_csv(
        VIDEOS_KEY,
        videos,
        VIDEO_FIELDS,
    )

    save_csv(
        MATCHES_KEY,
        matches,
        MATCH_FIELDS,
    )

    get_s3().put_object(
        Bucket=AWS_S3_BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
        ContentType="application/json",
    )

    print(
        "☁️ YouTube videos guardados: "
        f"s3://{AWS_S3_BUCKET}/{VIDEOS_KEY}"
    )

    print(
        "☁️ YouTube matches guardados: "
        f"s3://{AWS_S3_BUCKET}/{MATCHES_KEY}"
    )

    print(
        "☁️ YouTube state guardado: "
        f"s3://{AWS_S3_BUCKET}/{STATE_KEY}"
    )