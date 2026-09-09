import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# Cargar .env ANTES de importar módulos
# que leen variables de entorno.
load_dotenv()


from social_s3_store import (
    load_posts,
    save_posts,
    load_account_state,
    save_account_state,
)

from telegram_utils import telegram_send_message

BASE_DIR = Path(
    __file__
).resolve().parent

SOURCES_FILE = BASE_DIR / os.getenv(
    "X_SOCIAL_SOURCES_FILE",
    "social_sources_mauricio.json",
)

CHECK_INTERVAL = int(
    os.getenv(
        "X_CHECK_INTERVAL",
        "300",
    )
)

BEARER_TOKEN = os.getenv(
    "X_BEARER_TOKEN",
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


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_sources() -> list[dict]:
    data = json.loads(
        SOURCES_FILE.read_text(
            encoding="utf-8"
        )
    )

    return [
        row
        for row in data
        if row.get(
            "activo",
            True,
        ) is True
    ]


def x_headers() -> dict:
    if not BEARER_TOKEN:
        raise RuntimeError(
            "Falta X_BEARER_TOKEN"
        )

    return {
        "Authorization": (
            f"Bearer {BEARER_TOKEN}"
        )
    }


def get_user_by_username(
    username: str,
) -> dict:

    url = (
        "https://api.x.com/2/"
        f"users/by/username/{username}"
    )

    response = requests.get(
        url,
        headers=x_headers(),
        timeout=30,
    )

    response.raise_for_status()

    return response.json()["data"]


def get_new_posts(
    user_id: str,
    since_id: str = "",
) -> list[dict]:

    url = (
        "https://api.x.com/2/"
        f"users/{user_id}/tweets"
    )

    params = {
        "max_results": 5,
        "exclude": "retweets,replies",
        "tweet.fields": "created_at",
    }

    if since_id:
        params["since_id"] = since_id

    response = requests.get(
        url,
        headers=x_headers(),
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    return payload.get(
        "data",
        [],
    )


def format_telegram(
    source: dict,
    post: dict,
) -> str:

    username = source["username"]
    post_id = post["id"]

    url = (
        f"https://x.com/"
        f"{username}/status/{post_id}"
    )

    return "\n".join([
        f"X {source.get('emoji', '🐦')}",
        f"{source['nombre']}",
        "",
        f"📝 {post.get('text', '')}",
        "",
        f"🔗 {url}",
    ])


def run_once():
    sources = load_sources()

    posts = load_posts()
    state = load_account_state()

    existing_ids = {
        row.get("post_id")
        for row in posts
        if row.get("post_id")
    }

    print("=" * 90)
    print(
        "MONITOREO X | "
        f"cuentas activas={len(sources)}"
    )
    print(
        f"Posts existentes={len(existing_ids)}"
    )
    print("=" * 90)

    for source in sources:

        account_key = source["id"]

        try:
            account_state = state.get(
                account_key,
                {},
            )

            user_id = account_state.get(
                "user_id",
                "",
            )

            if not user_id:
                user = get_user_by_username(
                    source["username"]
                )

                user_id = user["id"]

                account_state[
                    "user_id"
                ] = user_id

            since_id = account_state.get(
                "since_id",
                "",
            )

            new_posts = get_new_posts(
                user_id=user_id,
                since_id=since_id,
            )

            print(
                f"@{source['username']} | "
                f"nuevos={len(new_posts)}"
            )

            # Primera corrida:
            # guardamos referencia, pero
            # NO mandamos histórico.
            bootstrap = not bool(
                since_id
            )

            newest_id = since_id

            for post in reversed(
                new_posts
            ):
                post_id = post["id"]

                if post_id in existing_ids:
                    continue

                row = {
                    "post_id": post_id,
                    "account_id": (
                        source["id"]
                    ),
                    "account_name": (
                        source["nombre"]
                    ),
                    "username": (
                        source["username"]
                    ),
                    "text": (
                        post.get(
                            "text",
                            "",
                        )
                    ),
                    "created_at": (
                        post.get(
                            "created_at",
                            "",
                        )
                    ),
                    "url": (
                        f"https://x.com/"
                        f"{source['username']}"
                        f"/status/{post_id}"
                    ),
                    "downloaded_at": (
                        utc_now_iso()
                    ),
                    "telegram_sent": (
                        "false"
                    ),
                    "telegram_sent_at": "",
                }

                posts.append(row)
                existing_ids.add(
                    post_id
                )

                if (
                    not bootstrap
                    and source.get(
                        "enviar_telegram",
                        True,
                    )
                ):
                    message = (
                        format_telegram(
                            source,
                            post,
                        )
                    )

                    telegram_send_message(
                        bot_token=BOT_TOKEN,
                        chat_id=CHAT_ID,
                        text=message,
                    )

                    row[
                        "telegram_sent"
                    ] = "true"

                    row[
                        "telegram_sent_at"
                    ] = utc_now_iso()

                    print(
                        "📨 Telegram enviado | "
                        f"{source['nombre']}"
                    )

                newest_id = post_id

            # Si la API devolvió posts,
            # el primero es el más reciente.
            if new_posts:
                newest_id = (
                    new_posts[0]["id"]
                )

            account_state[
                "since_id"
            ] = newest_id

            account_state[
                "username"
            ] = source["username"]

            state[
                account_key
            ] = account_state

        except Exception as error:
            print(
                f"❌ ERROR "
                f"@{source['username']}: "
                f"{error}"
            )

    save_posts(posts)
    save_account_state(state)


def run_forever():
    while True:
        try:
            run_once()
        except Exception as error:
            print(
                f"❌ ERROR CICLO X: "
                f"{error}"
            )

        time.sleep(
            CHECK_INTERVAL
        )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--once",
        action="store_true",
    )

    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()