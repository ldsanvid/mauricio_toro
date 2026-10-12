import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("YOUTUBE_API_KEY")

if not api_key:
    raise RuntimeError("Falta YOUTUBE_API_KEY")

url = "https://www.googleapis.com/youtube/v3/search"

params = {
    "part": "snippet",
    "q": "\"Abelardo de la Espriella\"",
    "type": "video",
    "order": "date",
    "maxResults": 10,
    "regionCode": "CO",
    "relevanceLanguage": "es",
    "key": api_key,
}

response = requests.get(
    url,
    params=params,
    timeout=30,
)

print("STATUS:", response.status_code)

data = response.json()

if response.status_code != 200:
    print(data)
    raise SystemExit()

for item in data.get("items", []):
    video_id = item["id"]["videoId"]
    snippet = item["snippet"]

    print("-" * 80)
    print("VIDEO ID:", video_id)
    print("TÍTULO:", snippet.get("title"))
    print("CANAL:", snippet.get("channelTitle"))
    print("FECHA:", snippet.get("publishedAt"))
    print(
        "URL:",
        f"https://www.youtube.com/watch?v={video_id}"
    )