import os
from dotenv import load_dotenv

from telegram_utils import telegram_send_message

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID_MAURICIO", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")

if not CHAT_ID:
    raise RuntimeError("Falta TELEGRAM_CHAT_ID_MAURICIO en .env")

mensaje = """🟣 Mauricio Toro
🧪 Prueba de monitoreo

El bot de alertas está conectado correctamente.

📰 Las nuevas noticias relevantes aparecerán aquí."""

respuesta = telegram_send_message(
    bot_token=BOT_TOKEN,
    chat_id=CHAT_ID,
    text=mensaje,
)

print("✅ Mensaje enviado correctamente")
print("message_id:", respuesta.get("result", {}).get("message_id"))