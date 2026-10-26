"""Lectura pública acotada para Cambio y EL PAÍS. Sólo biblioteca estándar."""
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 12
RETRY_SECONDS = 30 * 60
SUPPORTED = {"elpais.com", "cambiocolombia.com"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}


def publication_url(url):
    p = urlsplit(url)
    return (p.hostname or "").lower().removeprefix("www."), p.path.rstrip("/")


def supported_url(url):
    p = urlsplit(url)
    return (p.scheme == "https" and not p.username and not p.password
            and (p.hostname or "").lower().removeprefix("www.") in SUPPORTED)


def public_versions(url):
    if not supported_url(url):
        return []
    versions = [("original", url)]
    p = urlsplit(url)
    if publication_url(url)[0] == "elpais.com":
        query = [(k, v) for k, v in parse_qsl(p.query) if k != "outputType"]
        amp = urlunsplit((p.scheme, p.netloc, p.path,
                          urlencode(query + [("outputType", "amp")]), ""))
        if amp != url:
            versions.append(("amp", amp))
    return versions


class ArticleParser(HTMLParser):
    """Sólo cuerpo visible: excluye scripts, navegación y recomendaciones."""
    def __init__(self, host):
        super().__init__(convert_charrefs=True)
        self.host = host
        self.stack = []
        self.parts = []
        self.canonical = ""
        self.body_found = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in attrs.get("rel", "").split():
            self.canonical = attrs.get("href", "")
        if tag in VOID:
            return
        classes = set(attrs.get("class", "").split())
        root = ((self.host == "cambiocolombia.com" and "article-blocks" in classes)
                or (self.host == "elpais.com" and "a_c" in classes))
        inside = root or bool(self.stack and self.stack[-1][1])
        suppressed = (tag in {"script", "style", "nav", "aside", "footer", "figcaption"}
                      or bool(self.stack and self.stack[-1][2])
                      or attrs.get("aria-hidden") == "true"
                      or "hidden" in attrs)
        self.stack.append((tag, inside, suppressed))
        self.body_found = self.body_found or root
        if inside and tag in {"p", "h2", "h3", "blockquote", "div"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                self.parts.append(" ")
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            self.parts.append(data)


def extract_article(document, requested_url, max_chars=30000):
    parser = ArticleParser(publication_url(requested_url)[0])
    parser.feed(document)
    if not parser.canonical or publication_url(parser.canonical) != publication_url(requested_url):
        return "", "canonical_no_coincide"
    if not parser.body_found:
        return "", "sin_cuerpo_identificable"
    text = re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip()
    if len(text) < 120:
        return "", "cuerpo_insuficiente"
    return text[:max_chars] if max_chars > 0 else text, "ok"


@dataclass
class ReadResult:
    text: str = ""
    status: str = "pendiente"
    version: str = ""
    attempts: list = field(default_factory=list)


def read_cambio_external(url, max_chars=30000):
    """Respaldo anónimo Jina Reader; sólo transmite la URL pública de Cambio."""
    result = ReadResult(version="jina_cambio")
    if not supported_url(url) or publication_url(url)[0] != "cambiocolombia.com":
        result.status = "respaldo_no_permitido"
        return result
    attempt = {"version": "jina_cambio", "http_status": None, "estado": ""}
    result.attempts.append(attempt)
    try:
        request = Request("https://r.jina.ai/" + url, headers={
            "Accept": "application/json",
            "X-Target-Selector": ".article-blocks",
            "X-Remove-Selector": "script,style,nav,aside,footer,figcaption",
            "X-Respond-With": "text",
        })
        with urlopen(request, timeout=20) as response:
            attempt["http_status"] = response.status
            if "json" not in response.headers.get("Content-Type", "").lower():
                raise ValueError("respaldo_no_json")
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("respaldo_supera_limite")
            payload = json.loads(raw.decode("utf-8"))
        data = payload.get("data") or {}
        if payload.get("code") != 200 or data.get("httpStatus") != 200:
            raise ValueError("respaldo_origen_no_ok")
        metadata = data.get("metadata") or {}
        for returned_url in (data.get("url", ""), metadata.get("og:url", "")):
            if (not isinstance(returned_url, str) or not supported_url(returned_url)
                    or publication_url(returned_url) != publication_url(url)):
                raise ValueError("respaldo_url_no_coincide")
        if metadata.get("og:type") != "article":
            raise ValueError("respaldo_no_es_articulo")
        text = data.get("text") or data.get("content") or ""
        if not isinstance(text, str) or not isinstance(data.get("title"), str):
            raise ValueError("respaldo_formato_invalido")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 120:
            raise ValueError("respaldo_cuerpo_insuficiente")
        result.text = text[:max_chars] if max_chars > 0 else text
        result.status = "ok"
    except HTTPError as error:
        attempt["http_status"] = error.code
        result.status = f"respaldo_http_{error.code}"
        error.close()
    except Exception as error:
        result.status = str(error) if isinstance(error, ValueError) else type(error).__name__
    attempt["estado"] = result.status
    return result


def read_public_article(url, max_chars=30000, allow_cambio_fallback=False):
    result = ReadResult()
    for label, candidate in public_versions(url):
        attempt = {"version": label, "http_status": None, "estado": ""}
        result.attempts.append(attempt)
        try:
            # No cookies, sesiones de usuario, proxy ni servicios externos.
            request = Request(candidate, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=TIMEOUT) as response:
                attempt["http_status"] = response.status
                if publication_url(response.url) != publication_url(url):
                    raise ValueError("redireccion_a_otra_publicacion")
                if "html" not in response.headers.get("Content-Type", "").lower():
                    raise ValueError("contenido_no_html")
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("respuesta_supera_limite")
                encoding = response.headers.get_content_charset() or "utf-8"
                document = data.decode(encoding, errors="replace")
            text, status = extract_article(document, url, max_chars)
            attempt["estado"] = result.status = status
            if text:
                result.text, result.version = text, label
                return result
        except HTTPError as error:
            attempt["http_status"] = error.code
            attempt["estado"] = result.status = f"http_{error.code}"
            error.close()
        except Exception as error:
            attempt["estado"] = result.status = (
                str(error) if isinstance(error, ValueError) else type(error).__name__)
    if not result.attempts:
        result.status = "medio_no_soportado"
    if (allow_cambio_fallback and publication_url(url)[0] == "cambiocolombia.com"
            and result.status == "http_403"):
        fallback = read_cambio_external(url, max_chars=max_chars)
        fallback.attempts = result.attempts + fallback.attempts
        return fallback
    return result


def retry_due(reason, now=None):
    match = re.search(r"mauricio_lectura_pendiente:[^;|]+;retry_after=(\d+)", reason or "")
    return not match or (time.time() if now is None else now) >= int(match.group(1))


def pending_reason(status, now=None):
    timestamp = time.time() if now is None else now
    status = re.sub(r"[^a-zA-Z0-9_-]", "_", status)[:80]
    return f"mauricio_lectura_pendiente:{status};retry_after={int(timestamp) + RETRY_SECONDS}"
