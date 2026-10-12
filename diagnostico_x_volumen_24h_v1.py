import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SOURCES_FILE = BASE_DIR / "social_sources_mauricio_v2.json"

BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()

HOURS_BACK = 24

# X API permite solicitar múltiples resultados por página.
# Para diagnóstico queremos reducir el número de requests.
MAX_RESULTS = 100

REQUEST_TIMEOUT = 30

# Protección para no entrar accidentalmente en una paginación enorme.
MAX_PAGES_PER_ACCOUNT = 20

# Pequeña pausa entre requests.
SLEEP_BETWEEN_REQUESTS = 0.20


# =============================================================================
# HELPERS
# =============================================================================

def headers():
    if not BEARER_TOKEN:
        raise RuntimeError("Falta X_BEARER_TOKEN en el entorno.")

    return {
        "Authorization": f"Bearer {BEARER_TOKEN}"
    }


def load_sources():
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(
            f"No encontré el archivo: {SOURCES_FILE}"
        )

    data = json.loads(
        SOURCES_FILE.read_text(encoding="utf-8")
    )

    return [
        row
        for row in data
        if row.get("activo", True) is True
    ]


def parse_x_datetime(value):
    if not value:
        return None

    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def get_user(username):
    url = (
        "https://api.x.com/2/"
        f"users/by/username/{username}"
    )

    response = requests.get(
        url,
        headers=headers(),
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    payload = response.json()

    if "data" not in payload:
        raise RuntimeError(
            f"X no devolvió data para @{username}: {payload}"
        )

    return payload["data"]


def fetch_page(user_id, pagination_token=None):
    url = (
        "https://api.x.com/2/"
        f"users/{user_id}/tweets"
    )

    params = {
        "max_results": MAX_RESULTS,
        "exclude": "retweets,replies",
        "tweet.fields": "created_at",
    }

    if pagination_token:
        params["pagination_token"] = pagination_token

    response = requests.get(
        url,
        headers=headers(),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    # Diagnóstico más útil si aparece rate limit.
    if response.status_code == 429:
        reset = response.headers.get(
            "x-rate-limit-reset",
            "desconocido",
        )

        raise RuntimeError(
            "RATE LIMIT 429 | "
            f"x-rate-limit-reset={reset}"
        )

    response.raise_for_status()

    return response.json()


def get_posts_last_24h(user_id, cutoff):
    """
    Pagina hacia atrás hasta:
      - encontrar publicaciones anteriores al cutoff,
      - quedarse sin next_token,
      - o llegar al límite de seguridad.

    IMPORTANTE:
    exclude=retweets,replies ya filtra esos contenidos desde X.
    """

    collected = []

    pagination_token = None
    pages = 0
    api_requests = 0
    reached_cutoff = False

    while pages < MAX_PAGES_PER_ACCOUNT:
        payload = fetch_page(
            user_id,
            pagination_token=pagination_token,
        )

        api_requests += 1
        pages += 1

        posts = payload.get("data", [])
        meta = payload.get("meta", {})

        if not posts:
            break

        found_old_post = False

        for post in posts:
            created_at = parse_x_datetime(
                post.get("created_at")
            )

            if created_at is None:
                continue

            if created_at >= cutoff:
                collected.append(post)
            else:
                found_old_post = True
                reached_cutoff = True

        # Si esta página ya entró en publicaciones anteriores
        # a nuestra ventana, no necesitamos seguir paginando.
        if found_old_post:
            break

        pagination_token = meta.get("next_token")

        if not pagination_token:
            break

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    hit_safety_limit = (
        pages >= MAX_PAGES_PER_ACCOUNT
        and pagination_token is not None
        and not reached_cutoff
    )

    return {
        "posts": collected,
        "pages": pages,
        "api_requests": api_requests,
        "reached_cutoff": reached_cutoff,
        "hit_safety_limit": hit_safety_limit,
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    sources = load_sources()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_BACK)

    print("=" * 110)
    print("DIAGNÓSTICO X | VOLUMEN REAL ÚLTIMAS 24 HORAS")
    print("S3=NO | TELEGRAM=NO | OPENAI=NO | ESCRITURA=NO")
    print(f"Inicio UTC: {now.isoformat()}")
    print(f"Cutoff UTC: {cutoff.isoformat()}")
    print(f"Cuentas: {len(sources)}")
    print(f"max_results por página: {MAX_RESULTS}")
    print("=" * 110)

    total_posts = 0
    total_timeline_requests = 0
    total_user_lookup_requests = 0

    ok_accounts = 0
    failed_accounts = 0

    results = []

    for index, source in enumerate(
        sources,
        start=1,
    ):
        username = source["username"]
        nombre = source.get("nombre", username)

        print()
        print(
            f"[{index:02d}/{len(sources)}] "
            f"@{username}"
        )

        try:
            user = get_user(username)
            total_user_lookup_requests += 1

            result = get_posts_last_24h(
                user["id"],
                cutoff,
            )

            posts = result["posts"]

            count = len(posts)

            total_posts += count
            total_timeline_requests += (
                result["api_requests"]
            )

            ok_accounts += 1

            newest = None
            oldest = None

            if posts:
                dates = [
                    parse_x_datetime(
                        p.get("created_at")
                    )
                    for p in posts
                    if p.get("created_at")
                ]

                dates = [
                    d for d in dates
                    if d is not None
                ]

                if dates:
                    newest = max(dates)
                    oldest = min(dates)

            results.append({
                "username": username,
                "nombre": nombre,
                "posts_24h": count,
                "pages": result["pages"],
                "timeline_requests": (
                    result["api_requests"]
                ),
                "newest": newest,
                "oldest": oldest,
                "reached_cutoff": (
                    result["reached_cutoff"]
                ),
                "safety_limit": (
                    result["hit_safety_limit"]
                ),
            })

            print(
                f"    Posts 24h: {count}"
            )

            print(
                f"    Páginas API: "
                f"{result['pages']}"
            )

            if newest:
                print(
                    f"    Más reciente: "
                    f"{newest.isoformat()}"
                )

            if oldest:
                print(
                    f"    Más antiguo dentro "
                    f"de 24h: {oldest.isoformat()}"
                )

            if result["hit_safety_limit"]:
                print(
                    "    ⚠️ Se alcanzó el límite "
                    "de seguridad de paginación."
                )
            elif result["reached_cutoff"]:
                print(
                    "    ✅ Se alcanzó correctamente "
                    "el límite de 24h."
                )
            else:
                print(
                    "    ℹ️ La API se quedó sin más "
                    "resultados antes del cutoff."
                )

        except Exception as error:
            failed_accounts += 1

            print(
                f"    ❌ ERROR: {error}"
            )

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    # -------------------------------------------------------------------------
    # TABLA ORDENADA
    # -------------------------------------------------------------------------

    print()
    print("=" * 110)
    print("VOLUMEN POR CUENTA | ORDENADO DE MAYOR A MENOR")
    print("=" * 110)

    ordered = sorted(
        results,
        key=lambda x: x["posts_24h"],
        reverse=True,
    )

    print(
        f"{'CUENTA':<24}"
        f"{'POSTS 24H':>12}"
        f"{'PÁGINAS':>10}"
        f"{'REQ TIMELINE':>15}"
    )

    print("-" * 65)

    for row in ordered:
        print(
            f"@{row['username']:<23}"
            f"{row['posts_24h']:>12}"
            f"{row['pages']:>10}"
            f"{row['timeline_requests']:>15}"
        )

    # -------------------------------------------------------------------------
    # RESUMEN
    # -------------------------------------------------------------------------

    print()
    print("=" * 110)
    print("RESUMEN")
    print("=" * 110)

    print(
        f"Cuentas OK: "
        f"{ok_accounts}/{len(sources)}"
    )

    print(
        f"Cuentas con error: "
        f"{failed_accounts}"
    )

    print(
        f"Posts originales recuperados "
        f"en 24h: {total_posts}"
    )

    print(
        f"Requests timeline: "
        f"{total_timeline_requests}"
    )

    print(
        f"Requests lookup usuario: "
        f"{total_user_lookup_requests}"
    )

    print(
        f"Requests API totales de esta prueba: "
        f"{total_timeline_requests + total_user_lookup_requests}"
    )

    if ok_accounts:
        average = total_posts / ok_accounts

        print(
            f"Promedio posts/cuenta/24h: "
            f"{average:.1f}"
        )

    projected_month = total_posts * 30

    print(
        f"Proyección simple a 30 días "
        f"al mismo ritmo: "
        f"{projected_month:,} posts"
    )

    print()
    print(
        "NOTA: la proyección mensual es sólo una "
        "extrapolación del volumen observado hoy; "
        "no es todavía una estimación de facturación."
    )

    print()
    print(
        "NO se modificó S3."
    )

    print(
        "NO se modificó ningún since_id."
    )

    print(
        "NO se llamó a OpenAI."
    )

    print(
        "NO se envió Telegram."
    )

    print("=" * 110)


if __name__ == "__main__":
    main()