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

POSTS_KEY = os.getenv(
    "MAURICIO_X_POSTS_S3_KEY",
    "mauricio_toro/social/raw/x_posts.csv",
).strip()

STATE_KEY = os.getenv(
    "MAURICIO_X_STATE_S3_KEY",
    "mauricio_toro/social/state/x_accounts_state.json",
).strip()


POST_FIELDS = [
    "post_id",
    "account_id",
    "account_name",
    "username",
    "text",
    "created_at",
    "url",
    "downloaded_at",
    "telegram_sent",
    "telegram_sent_at",
]


def get_s3():
    return boto3.client(
        "s3",
        region_name=AWS_REGION,
    )


def load_posts() -> list[dict]:
    if not AWS_S3_BUCKET:
        raise RuntimeError(
            "Falta AWS_S3_BUCKET"
        )

    s3 = get_s3()

    try:
        response = s3.get_object(
            Bucket=AWS_S3_BUCKET,
            Key=POSTS_KEY,
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


def save_posts(
    posts: list[dict],
) -> None:

    output = io.StringIO()

    writer = csv.DictWriter(
        output,
        fieldnames=POST_FIELDS,
        extrasaction="ignore",
    )

    writer.writeheader()

    for row in posts:
        writer.writerow(row)

    get_s3().put_object(
        Bucket=AWS_S3_BUCKET,
        Key=POSTS_KEY,
        Body=output.getvalue().encode(
            "utf-8"
        ),
        ContentType="text/csv",
    )

    print(
        f"☁️ X posts guardados: "
        f"s3://{AWS_S3_BUCKET}/{POSTS_KEY}"
    )


def load_account_state() -> dict:
    s3 = get_s3()

    try:
        response = s3.get_object(
            Bucket=AWS_S3_BUCKET,
            Key=STATE_KEY,
        )

        return json.loads(
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
            return {}

        raise


def save_account_state(
    state: dict,
) -> None:

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
        f"☁️ X state guardado: "
        f"s3://{AWS_S3_BUCKET}/{STATE_KEY}"
    )