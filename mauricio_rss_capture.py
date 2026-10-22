"""Lectura acotada de RSS. Este módulo no usa S3, Telegram ni credenciales."""
from dataclasses import dataclass
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

MAX_RSS_BYTES = 2 * 1024 * 1024
RSS_TIMEOUT_SECONDS = 12


@dataclass
class FeedResponse:
    label: str
    url: str
    body: bytes = b''
    status: int | None = None
    error: str = ''


def query_plan(base_url: str, rss_id: str, include_recent: bool | None = None) -> list[tuple[str, str]]:
    """Conserva la consulta original y agrega ventanas sólo al RSS Mauricio.

    No modifica el JSON ni agrega filtros geográficos. Las ventanas son
    consultas complementarias: no implican que Google indexe cada publicación.
    """
    if include_recent is None:
        include_recent = os.getenv('MAURICIO_RSS_RECENT_QUERIES', 'true').strip().lower() not in {'0','false','no','off'}
    if not include_recent:
        return [('base', base_url)]
    parsed = urlsplit(base_url)
    if rss_id != 'mauricio_toro' or (parsed.hostname or '').lower() != 'news.google.com':
        return [('base', base_url)]
    params = parse_qsl(parsed.query, keep_blank_values=True)
    query = next((v for k, v in params if k == 'q'), '')
    if not query.strip():
        return [('base', base_url)]
    # Evitar operadores temporales contradictorios si ya hubiera uno.
    query = re.sub(r'(?<!\S)when:\S+', '', query, flags=re.I).strip()
    result = []
    for label, window in [('recientes_24h', '1d'), ('recientes_72h', '3d')]:
        changed = [(k, query + ' when:' + window if k == 'q' else v) for k, v in params]
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(changed), parsed.fragment))
        result.append((label, url))
    result.append(('base', base_url))
    return result


def fetch_feed(label: str, url: str) -> FeedResponse:
    """Como máximo 2 MiB por respuesta y una conexión por vez."""
    request = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; MediaMonitor/1.0)',
        'Accept': 'application/rss+xml, application/xml, text/xml;q=0.9',
    })
    try:
        with urlopen(request, timeout=RSS_TIMEOUT_SECONDS) as response:
            status = getattr(response, 'status', 200)
            body = response.read(MAX_RSS_BYTES + 1)
            if len(body) > MAX_RSS_BYTES:
                raise ValueError(f'RSS supera el máximo de {MAX_RSS_BYTES} bytes')
            if not body:
                raise ValueError('RSS vacío')
            return FeedResponse(label, url, body=body, status=status)
    except Exception as error:
        return FeedResponse(label, url, error=f'{type(error).__name__}: {error}')
