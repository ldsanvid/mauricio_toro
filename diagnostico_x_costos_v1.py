import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

SOURCES_FILE = BASE_DIR / "social_sources_mauricio_v2.json"

BEARER_TOKEN = os.getenv(
    "X_BEARER_TOKEN",
    "",
).strip()


def headers():
    if not BEARER_TOKEN:
        raise RuntimeError(
            "Falta X_BEARER_TOKEN en el entorno."
        )

    return {
        "Authorization": f"Bearer {BEARER_TOKEN}"
    }


def load_sources():
    data = json.loads(
        SOURCES_FILE.read_text(
            encoding="utf-8"
        )
    )

    return [
        row
        for row in data
        if row.get("activo", True) is True
    ]


def get_user(username):
    url = (
        "https://api.x.com/2/"
        f"users/by/username/{username}"
    )

    response = requests.get(
        url,
        headers=headers(),
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    return payload["data"]


def get_recent_posts(user_id):
    url = (
        "https://api.x.com/2/"
        f"users/{user_id}/tweets"
    )

    params = {
        "max_results": 5,
        "exclude": "retweets,replies",
        "tweet.fields": "created_at",
    }

    response = requests.get(
        url,
        headers=headers(),
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    return (
        payload.get("data", []),
        payload.get("meta", {}),
    )


def main():
    sources = load_sources()

    print("=" * 100)
    print("DIAGNÓSTICO X | CUENTAS SELECCIONADAS")
    print("S3=NO | TELEGRAM=NO | OPENAI=NO | ESCRITURA=NO")
    print(f"Cuentas configuradas: {len(sources)}")
    print("=" * 100)

    total_posts = 0
    ok_accounts = 0
    failed_accounts = 0
    saturated_accounts = 0

    results = []

    for index, source in enumerate(
        sources,
        start=1,
    ):
        username = source["username"]

        try:
            user = get_user(username)

            posts, meta = get_recent_posts(
                user["id"]
            )

            count = len(posts)

            total_posts += count
            ok_accounts += 1

            # Con max_results=5, llegar a 5 nos dice
            # que necesitamos estudiar paginación/frecuencia.
            saturated = count >= 5

            if saturated:
                saturated_accounts += 1

            newest = ""
            oldest = ""

            if posts:
                newest = posts[0].get(
                    "created_at",
                    "",
                )

                oldest = posts[-1].get(
                    "created_at",
                    "",
                )

            results.append({
                "username": username,
                "nombre": source["nombre"],
                "posts": count,
                "saturated": saturated,
                "newest": newest,
                "oldest": oldest,
                "next_token": bool(
                    meta.get("next_token")
                ),
            })

            marker = (
                "⚠️ TOPE"
                if saturated
                else "✅"
            )

            print(
                f"[{index:02d}/{len(sources)}] "
                f"@{username:<20} "
                f"posts={count:<2} "
                f"{marker}"
            )

            if newest:
                print(
                    f"       más reciente: {newest}"
                )
                print(
                    f"       más antiguo:  {oldest}"
                )

            if meta.get("next_token"):
                print(
                    "       ⚠️ API indica que existen "
                    "más resultados (next_token)."
                )

        except Exception as error:
            failed_accounts += 1

            print(
                f"[{index:02d}/{len(sources)}] "
                f"@{username:<20} "
                f"❌ ERROR: {error}"
            )

    print()
    print("=" * 100)
    print("RESUMEN")
    print("=" * 100)

    print(
        f"Cuentas consultadas correctamente: "
        f"{ok_accounts}/{len(sources)}"
    )

    print(
        f"Cuentas con error: {failed_accounts}"
    )

    print(
        f"Posts devueltos: {total_posts}"
    )

    print(
        f"Cuentas que llegaron al tope de 5: "
        f"{saturated_accounts}"
    )

    print()

    print(
        "IMPORTANTE: 'posts devueltos' es la métrica "
        "que usaremos para estimar consumo."
    )

    print(
        "Este diagnóstico NO modifica S3, "
        "NO modifica since_id, NO llama a OpenAI "
        "y NO envía Telegram."
    )

    print()

    if saturated_accounts:
        print(
            "⚠️ Hay cuentas que alcanzaron los 5 posts. "
            "Antes de definir la frecuencia del worker "
            "debemos medir su volumen y/o implementar "
            "paginación."
        )
    else:
        print(
            "✅ Ninguna cuenta alcanzó el máximo de "
            "5 resultados en esta fotografía."
        )

    print("=" * 100)


if __name__ == "__main__":
    main()