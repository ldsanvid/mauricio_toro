import os
import requests
from dotenv import load_dotenv

load_dotenv()

bearer_token = os.getenv("X_BEARER_TOKEN")

if not bearer_token:
    raise RuntimeError("Falta X_BEARER_TOKEN")

user_id = "64791701"

url = f"https://api.x.com/2/users/{user_id}/tweets"

headers = {
    "Authorization": f"Bearer {bearer_token}"
}

params = {
    "max_results": 5,
    "exclude": "retweets,replies",
    "tweet.fields": "created_at"
}

response = requests.get(
    url,
    headers=headers,
    params=params,
    timeout=30,
)

print("STATUS:", response.status_code)
print(response.text)