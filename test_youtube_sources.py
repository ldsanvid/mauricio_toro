import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv(
    "YOUTUBE_API_KEY",
    "",
).strip()

SOURCES_FILE = (
    BASE_DIR
    / "youtube_sources_mauricio.json"
)

if not API_KEY:
    raise RuntimeError(
        "Falta YOUTUBE_API_KEY"
    )


def search_youtube(
    source: dict,
    config: dict,
) -> list[dict]:

    url = (
        "https://www.googleapis.com/"
        "youtube/v3/search"
    )

    # Para esta prueba miramos las
    # últimas 24 horas.
    published_after = (
        datetime.now(timezone.utc)
        - timedelta(hours=24)
    ).replace(
        microsecond=0
    ).isoformat().replace(
        "+00:00",
        "Z",
    )

    params = {
        "part": "snippet",
        "q": source["query"],
        "type": "video",
        "order": "date",
        "maxResults": config.get(
            "max_results",
            25,
        ),
        "regionCode": config.get(
            "region_code",
            "CO",
        ),
        "relevanceLanguage": config.get(
            "relevance_language",
            "es",
        ),
        "publishedAfter": published_after,
        "key": API_KEY,
    }

    response = requests.get(
        url,
        params=params,
        timeout=30,
    )

    print(
        f"\n{'=' * 100}"
    )
    print(
        f"{source['category']} | "
        f"{source['query']}"
    )
    print(
        f"STATUS: "
        f"{response.status_code}"
    )

    if response.status_code != 200:
        print(response.text)
        return []

    payload = response.json()

    return payload.get(
        "items",
        [],
    )


def main():

    config = json.loads(
        SOURCES_FILE.read_text(
            encoding="utf-8"
        )
    )

    sources = [
        source
        for source in config[
            "sources"
        ]
        if source.get(
            "active",
            True,
        )
    ]

    total_results = 0

    for source in sources:

        items = search_youtube(
            source,
            config,
        )

        total_results += len(
            items
        )

        print(
            f"RESULTADOS: {len(items)}"
        )

        for item in items:
            video_id = (
                item.get(
                    "id",
                    {},
                ).get(
                    "videoId",
                    "",
                )
            )

            snippet = item.get(
                "snippet",
                {},
            )

            print("-" * 100)
            print(
                "VIDEO ID:",
                video_id,
            )
            print(
                "TÍTULO:",
                snippet.get(
                    "title",
                    "",
                ),
            )
            print(
                "CANAL:",
                snippet.get(
                    "channelTitle",
                    "",
                ),
            )
            print(
                "FECHA:",
                snippet.get(
                    "publishedAt",
                    "",
                ),
            )
            print(
                "URL:",
                (
                    "https://www.youtube.com/"
                    f"watch?v={video_id}"
                ),
            )

    print(
        f"\n{'=' * 100}"
    )
    print(
        f"BÚSQUEDAS EJECUTADAS: "
        f"{len(sources)}"
    )
    print(
        f"RESULTADOS TOTALES: "
        f"{total_results}"
    )
    print(
        "=" * 100
    )


if __name__ == "__main__":
    main()  