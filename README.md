# Monitoreo de medios - Mauricio Toro

Worker de monitoreo basado en Google News RSS.

## Flujo

Google News RSS -> filtro de relevancia -> deduplicación -> S3 -> Telegram.

S3 funciona como almacenamiento persistente del histórico.

## Archivos de producción

- `google_news_worker_mauricio.py`
- `google_news_sources_mauricio.json`
- `news_s3_store.py`
- `telegram_utils.py`
- `requirements.txt`

## Prueba local

Instalar dependencias:

```bash
pip install -r requirements.txt
```

Ejecutar una sola revisión:

```bash
python google_news_worker_mauricio.py --once
```

Ejecución continua:

```bash
python google_news_worker_mauricio.py
```

## Render

Tipo de servicio:

`Background Worker`

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
python google_news_worker_mauricio.py
```

Variables de entorno requeridas:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID_MAURICIO
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_REGION
AWS_S3_BUCKET
MAURICIO_NEWS_ARTICLES_S3_KEY
MAURICIO_NEWS_MATCHES_S3_KEY
GOOGLE_NEWS_CHECK_INTERVAL
MAURICIO_NEWS_SOURCES_FILE
MAURICIO_TELEGRAM_ON_BOOTSTRAP
MAURICIO_MONITORING_START_DATE
```

Valores no secretos recomendados:

```text
AWS_REGION=us-east-2
AWS_S3_BUCKET=mauriciotoro
MAURICIO_NEWS_ARTICLES_S3_KEY=mauricio_toro/news/raw/news_articles.csv
MAURICIO_NEWS_MATCHES_S3_KEY=mauricio_toro/news/raw/news_matches.csv
GOOGLE_NEWS_CHECK_INTERVAL=300
MAURICIO_NEWS_SOURCES_FILE=google_news_sources_mauricio.json
MAURICIO_TELEGRAM_ON_BOOTSTRAP=false
MAURICIO_MONITORING_START_DATE=2026-08-31
```

No subir `.env` ni credenciales a GitHub.
