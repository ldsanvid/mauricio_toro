import time
import requests


def telegram_send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    max_attempts: int = 3,
):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    if len(text) > 4000:
        text = text[:4000] + "\n…"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }

    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=(10, 25),
            )

            if not response.ok:
                print(
                    "❌ Telegram error:",
                    response.status_code,
                    response.text,
                )

            response.raise_for_status()
            return response.json()

        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as error:
            last_error = error

            if attempt >= max_attempts:
                break

            wait_seconds = attempt * 2
            print(
                f"⚠️ Telegram conexión falló "
                f"(intento {attempt}/{max_attempts}). "
                f"Reintentando en {wait_seconds}s..."
            )
            time.sleep(wait_seconds)

    raise last_error
