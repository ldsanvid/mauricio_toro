import os
import requests
from dotenv import load_dotenv


load_dotenv()

BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")

if not BEARER_TOKEN:
    raise RuntimeError("Falta X_BEARER_TOKEN en variables de entorno.")

URL = "https://api.x.com/2/tweets/search/recent"

# PRUEBA MUY PEQUEÑA:
# Mauricio Toro + sólo 5 cuentas.
QUERY = (
    '("Mauricio Toro" OR @MauroToroO) '
    '(from:NoticiasRCN OR from:NoticiasCaracol OR '
    'from:CaracolRadio OR from:lafm OR from:RevistaSemana) '
    '-is:retweet'
)

params = {
    "query": QUERY,
    "max_results": 10,
    "tweet.fields": "created_at,author_id",
}

headers = {
    "Authorization": f"Bearer {BEARER_TOKEN}"
}

print("=" * 100)
print("TEST X | RECENT SEARCH | MAURICIO TORO")
print("=" * 100)
print("S3=NO | TELEGRAM=NO | OPENAI=NO | ESCRITURA=NO")
print()
print("QUERY:")
print(QUERY)
print()
print("max_results=10")
print("=" * 100)

response = requests.get(
    URL,
    headers=headers,
    params=params,
    timeout=30,
)

print(f"HTTP STATUS: {response.status_code}")

if response.status_code != 200:
    print()
    print("RESPUESTA DE X:")
    print(response.text)
    raise SystemExit(1)

payload = response.json()

posts = payload.get("data", [])

print()
print(f"Posts devueltos: {len(posts)}")
print()

for i, post in enumerate(posts, 1):
    print("-" * 100)
    print(f"[{i}] ID: {post.get('id')}")
    print(f"Fecha: {post.get('created_at')}")
    print(f"Texto: {post.get('text')}")

print()
print("=" * 100)
print("META:")
print(payload.get("meta", {}))
print("=" * 100)
print()
print("FIN DEL TEST")
print("NO se modificó S3.")
print("NO se modificó since_id.")
print("NO se llamó a OpenAI.")
print("NO se envió Telegram.")
print("=" * 100)