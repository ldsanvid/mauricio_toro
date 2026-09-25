#!/usr/bin/env python3
"""
x_worker_mauricio_v2.py

Monitoreo de menciones de Mauricio Toro en X usando Recent Search.
NO descarga timelines completas.

Modos:
  --dry-run        Consulta X, imprime resultados, NO escribe S3 y NO envía Telegram.
  --bootstrap      Consulta X y guarda el punto de partida en S3, SIN enviar histórico.
  --once           Ejecución productiva única: sólo procesa resultados posteriores al estado guardado.
  --test-telegram  Consulta X y envía resultados nuevos al TELEGRAM_CHAT_ID_TEST; NO escribe S3.
  sin argumentos   Worker continuo con X_CHECK_INTERVAL (default 900 s).

Protecciones de costo:
  - búsqueda filtrada por Mauricio + cuentas seleccionadas
  - max_results configurable, default 10
  - máximo de páginas configurable, default 1
  - no usa /users/{id}/tweets
  - no hace lookup individual de cada usuario
"""

import argparse
import csv
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv()

from telegram_utils import telegram_send_message

API_URL = "https://api.x.com/2/tweets/search/recent"

BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_PROD = os.getenv("TELEGRAM_CHAT_ID_MAURICIO", "").strip()
CHAT_ID_TEST = os.getenv("TELEGRAM_CHAT_ID_TEST", "").strip()

AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", "").strip()
STATE_KEY = os.getenv(
    "MAURICIO_X_SEARCH_STATE_S3_KEY",
    "mauricio_toro/social/state/x_search_mauricio_state.json",
).strip()
POSTS_KEY = os.getenv(
    "MAURICIO_X_SEARCH_POSTS_S3_KEY",
    "mauricio_toro/social/raw/x_mentions_mauricio.csv",
).strip()

SOURCES_FILE = BASE_DIR / os.getenv(
    "X_MAURICIO_SOURCES_FILE",
    "x_sources_mauricio_v1.json",
)

CHECK_INTERVAL = int(os.getenv("X_CHECK_INTERVAL", "900"))
MAX_RESULTS = max(10, min(100, int(os.getenv("X_SEARCH_MAX_RESULTS", "10"))))
MAX_PAGES = max(1, int(os.getenv("X_SEARCH_MAX_PAGES", "1")))

POST_FIELDS = [
    "post_id", "author_id", "account_name", "username", "text",
    "created_at", "url", "downloaded_at", "telegram_sent",
    "telegram_sent_at",
]


def utc_now_iso():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def get_s3():
    if not AWS_S3_BUCKET:
        raise RuntimeError("Falta AWS_S3_BUCKET")
    return boto3.client("s3", region_name=AWS_REGION)


def load_json_s3(key, default):
    try:
        obj = get_s3().get_object(Bucket=AWS_S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404"}:
            return default
        raise


def save_json_s3(key, data):
    get_s3().put_object(
        Bucket=AWS_S3_BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def load_posts_s3():
    try:
        obj = get_s3().get_object(Bucket=AWS_S3_BUCKET, Key=POSTS_KEY)
        text = obj["Body"].read().decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404"}:
            return []
        raise


def save_posts_s3(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=POST_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    get_s3().put_object(
        Bucket=AWS_S3_BUCKET,
        Key=POSTS_KEY,
        Body=out.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )


def load_sources():
    if not SOURCES_FILE.exists():
        raise RuntimeError(f"No existe archivo de fuentes: {SOURCES_FILE}")
    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    result = []
    for row in data:
        username = str(row.get("username", "")).strip().lstrip("@")
        if username:
            result.append({
                "username": username,
                "nombre": row.get("nombre") or username,
            })
    if not result:
        raise RuntimeError("No hay cuentas configuradas.")
    return result


def build_query(sources):
    # Dos formas inequívocas de mención. Se puede ampliar después.
    mention = '("Mauricio Toro" OR @MauroToroO)'
    froms = " OR ".join(f"from:{s['username']}" for s in sources)
    query = f"{mention} ({froms}) -is:retweet"
    if len(query) > 512:
        raise RuntimeError(
            f"Query demasiado larga ({len(query)} caracteres). "
            "Divide las fuentes en grupos."
        )
    return query


def x_headers():
    if not BEARER_TOKEN:
        raise RuntimeError("Falta X_BEARER_TOKEN")
    return {"Authorization": f"Bearer {BEARER_TOKEN}"}


def search_recent(query, since_id=""):
    """
    Devuelve (posts, users_by_id, meta_final).
    Protegido por MAX_PAGES.
    """
    all_posts = []
    users_by_id = {}
    next_token = None
    meta_final = {}

    for page in range(1, MAX_PAGES + 1):
        params = {
            "query": query,
            "max_results": MAX_RESULTS,
            "tweet.fields": "created_at,author_id",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        if since_id:
            params["since_id"] = since_id
        if next_token:
            params["next_token"] = next_token

        r = requests.get(API_URL, headers=x_headers(), params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"X API HTTP {r.status_code}: {r.text[:1000]}")
        payload = r.json()

        page_posts = payload.get("data", [])
        all_posts.extend(page_posts)

        for user in payload.get("includes", {}).get("users", []):
            users_by_id[str(user.get("id"))] = user

        meta_final = payload.get("meta", {})
        next_token = meta_final.get("next_token")

        print(
            f"🔎 X Search | página={page} | devueltos={len(page_posts)}"
            + (" | hay_más=SÍ" if next_token else " | hay_más=NO")
        )

        if not next_token:
            break

    if next_token:
        print(
            f"⚠️ COST GUARD | Se alcanzó X_SEARCH_MAX_PAGES={MAX_PAGES}. "
            "No se pedirán más páginas en esta corrida."
        )

    # Deduplicación defensiva por ID
    dedup = {}
    for post in all_posts:
        if post.get("id"):
            dedup[post["id"]] = post
    posts = list(dedup.values())
    posts.sort(key=lambda p: int(p["id"]))
    return posts, users_by_id, meta_final


def source_maps(sources):
    by_username = {s["username"].lower(): s for s in sources}
    return by_username


def row_from_post(post, users_by_id, sources):
    author_id = str(post.get("author_id", ""))
    user = users_by_id.get(author_id, {})
    username = str(user.get("username", "")).strip()
    source = source_maps(sources).get(username.lower(), {})
    name = source.get("nombre") or user.get("name") or username or author_id
    post_id = post["id"]
    url = f"https://x.com/{username}/status/{post_id}" if username else f"https://x.com/i/web/status/{post_id}"
    return {
        "post_id": post_id,
        "author_id": author_id,
        "account_name": name,
        "username": username,
        "text": post.get("text", ""),
        "created_at": post.get("created_at", ""),
        "url": url,
        "downloaded_at": utc_now_iso(),
        "telegram_sent": "false",
        "telegram_sent_at": "",
    }


def format_telegram(row):
    return "\n".join([
        "🟣 Mauricio Toro",
        "🔎 Detectado en X",
        f"🗞 {row.get('account_name') or row.get('username')}",
        "",
        f"📝 {row.get('text', '')}",
        "",
        f"🔗 {row.get('url', '')}",
    ])


def print_result(row, prefix="NUEVO"):
    print("-" * 100)
    print(f"{prefix} | @{row.get('username') or '?'} | {row['post_id']}")
    print(f"Fecha: {row.get('created_at', '')}")
    print(f"Texto: {row.get('text', '')}")
    print(f"URL: {row.get('url', '')}")


def run_cycle(mode):
    """
    mode: dry-run | bootstrap | once | test-telegram
    """
    sources = load_sources()
    query = build_query(sources)

    read_s3 = mode in {"bootstrap", "once", "test-telegram"}
    write_s3 = mode in {"bootstrap", "once"}
    send_prod = mode == "once"
    send_test = mode == "test-telegram"

    state = load_json_s3(STATE_KEY, {}) if read_s3 else {}
    existing_rows = load_posts_s3() if read_s3 else []
    existing_ids = {r.get("post_id") for r in existing_rows if r.get("post_id")}

    since_id = str(state.get("since_id", "")).strip()

    print("=" * 100)
    print(f"X MAURICIO V2 | modo={mode}")
    print(f"Fuentes={len(sources)} | max_results={MAX_RESULTS} | max_pages={MAX_PAGES}")
    print(f"since_id={'SÍ' if since_id else 'NO'}")
    print(f"S3 escritura={'SÍ' if write_s3 else 'NO'} | Telegram prod={'SÍ' if send_prod else 'NO'} | Telegram test={'SÍ' if send_test else 'NO'}")
    print(f"Query ({len(query)} chars): {query}")
    print("=" * 100)

    # Dry-run deliberadamente no usa since_id de S3: sirve para inspeccionar
    # una muestra reciente sin tocar estado.
    search_since = since_id if mode in {"once", "test-telegram"} else ""
    posts, users_by_id, meta = search_recent(query, since_id=search_since)

    rows = [row_from_post(p, users_by_id, sources) for p in posts]
    new_rows = [r for r in rows if r["post_id"] not in existing_ids]

    for row in new_rows:
        print_result(row, "DETECTADO")

    newest_id = ""
    if posts:
        newest_id = max((p["id"] for p in posts), key=int)

    if mode == "dry-run":
        print("=" * 100)
        print(f"DRY RUN | detectados={len(rows)} | S3 NO MODIFICADO | TELEGRAM NO ENVIADO")
        return

    if mode == "bootstrap":
        # Bootstrap: fija referencia y guarda resultados como históricos,
        # pero nunca manda Telegram.
        for row in new_rows:
            existing_rows.append(row)
            existing_ids.add(row["post_id"])
        if newest_id:
            state["since_id"] = newest_id
        state["bootstrapped_at"] = state.get("bootstrapped_at") or utc_now_iso()
        state["last_run_at"] = utc_now_iso()
        state["query"] = query
        save_posts_s3(existing_rows)
        save_json_s3(STATE_KEY, state)
        print("=" * 100)
        print(f"BOOTSTRAP COMPLETO | históricos_guardados={len(new_rows)} | Telegram=0")
        return

    if not since_id:
        raise RuntimeError(
            "No existe since_id productivo. Ejecuta primero: "
            "python x_worker_mauricio_v2.py --bootstrap"
        )

    target_chat = CHAT_ID_TEST if send_test else CHAT_ID_PROD
    if (send_test or send_prod) and (not BOT_TOKEN or not target_chat):
        missing = "TELEGRAM_CHAT_ID_TEST" if send_test else "TELEGRAM_CHAT_ID_MAURICIO"
        raise RuntimeError(f"Falta TELEGRAM_BOT_TOKEN o {missing}")

    # test-telegram NO escribe S3; once sí.
    for row in new_rows:
        if send_test or send_prod:
            telegram_send_message(
                bot_token=BOT_TOKEN,
                chat_id=target_chat,
                text=format_telegram(row),
            )
            row["telegram_sent"] = "true"
            row["telegram_sent_at"] = utc_now_iso()
            print(f"📨 Telegram {'TEST' if send_test else 'PROD'} enviado | {row['post_id']}")

        if write_s3:
            existing_rows.append(row)
            existing_ids.add(row["post_id"])

    if write_s3:
        # Avanzamos el cursor sólo después de procesar correctamente la corrida.
        if newest_id:
            state["since_id"] = newest_id
        state["last_run_at"] = utc_now_iso()
        state["query"] = query
        save_posts_s3(existing_rows)
        save_json_s3(STATE_KEY, state)
        print("☁️ S3 actualizado.")

    print("=" * 100)
    print(
        f"FIN | API resultados={len(rows)} | nuevos={len(new_rows)} | "
        f"Telegram={'TEST' if send_test else ('PROD' if send_prod else 'NO')}"
    )


def run_forever():
    while True:
        try:
            run_cycle("once")
        except Exception as e:
            print(f"❌ ERROR CICLO X: {e}")
        time.sleep(CHECK_INTERVAL)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--bootstrap", action="store_true")
    group.add_argument("--once", action="store_true")
    group.add_argument("--test-telegram", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        run_cycle("dry-run")
    elif args.bootstrap:
        run_cycle("bootstrap")
    elif args.once:
        run_cycle("once")
    elif args.test_telegram:
        run_cycle("test-telegram")
    else:
        run_forever()


if __name__ == "__main__":
    main()
