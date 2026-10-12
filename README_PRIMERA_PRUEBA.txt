MAURICIO TORO - NEWS MONITOR V2 (S3 + TELEGRAM)

ARQUITECTURA
Google News RSS Colombia
    -> google_news_worker_mauricio.py
    -> S3:
         1) news_articles.csv  (una fila por noticia única)
         2) news_matches.csv   (una fila por término/feed que detectó la noticia)
    -> Telegram para noticias nuevas

NO USA POSTGRESQL.

----------------------------------------
ARCHIVOS
----------------------------------------
google_news_sources_mauricio.json
    Configuración del cliente, términos y feeds.

google_news_worker_mauricio.py
    Worker principal.

news_s3_store.py
    Persistencia y lectura de los dos CSV en S3.

telegram_utils.py
    Helper de Telegram.

requirements_mauricio.txt
    Dependencias Python.

.env.mauricio.example
    Variables de entorno necesarias.

----------------------------------------
PRIMERA PRUEBA
----------------------------------------
1. Configura AWS_S3_BUCKET, AWS_REGION y credenciales AWS.
2. Por seguridad puedes dejar Telegram configurado desde el principio:
   el bootstrap NO manda el histórico inicial mientras
   MAURICIO_TELEGRAM_ON_BOOTSTRAP=false.

3. Instala:
   pip install -r requirements_mauricio.txt

4. Ejecuta:
   python google_news_worker_mauricio.py --once

PRIMER ARRANQUE:
- Si news_articles.csv todavía NO existe en S3:
  -> bootstrap=True
  -> guarda todas las entradas disponibles de Google News
  -> NO las manda a Telegram por defecto.

SIGUIENTE ARRANQUE:
- Lee el histórico desde S3.
- Sólo las noticias con article_id nuevo se consideran nuevas.
- Esas sí pueden disparar Telegram.

----------------------------------------
DEDUPLICACIÓN
----------------------------------------
news_articles.csv:
- una sola fila por article_id.

news_matches.csv:
- una fila por combinación:
  article_id + cliente_id + rss_id

Ejemplo:
Una misma noticia puede aparecer en:
- Mauricio Toro
- Alianza Verde
- Reforma laboral

La noticia cuenta una sola vez en articles,
pero puede tener tres matches.

----------------------------------------
S3 SUGERIDO
----------------------------------------
mauricio_toro/news/raw/news_articles.csv
mauricio_toro/news/raw/news_matches.csv

----------------------------------------
PARA AGREGAR TÉRMINOS
----------------------------------------
Sólo agrega nuevos objetos RSS dentro de:
google_news_sources_mauricio.json

No es necesario modificar el worker.


----------------------------------------
FILTRO DE RELEVANCIA (V3)
----------------------------------------
news_articles.csv incorpora:
- relevante
- motivo_relevancia
- es_operativa

Reglas actuales:
1. Rechaza explícitamente:
   - Óscar Mauricio Toro
   - Mauricio Toro-Goya
2. Exige que "Mauricio Toro" aparezca en título o resumen RSS.
3. No exige Alianza Verde, Cámara, Icetex u otra palabra contextual.
4. El histórico previo se conserva y se reetiqueta; no se borra.

Inicio operativo:
MAURICIO_MONITORING_START_DATE=2026-08-31

Telegram sólo se dispara cuando:
- article_id es nuevo
- relevante=true
- es_operativa=true
