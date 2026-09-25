#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
youtube_worker_mauricio_v5.py

V4 sobre V3:
- Match léxico con límites de palabra para todos los términos.
- Clasifica por separado título, descripción y transcripción.
- Limpia boilerplate típico de descripciones.
- Términos amplios en descripción no bastan por sí solos.
- Mauricio Toro se detecta independientemente de los ejes.
- Mantiene timestamp/contexto, S3 y Telegram de V3.
"""
import argparse, html, json, os, re, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CONFIG_FILE = BASE_DIR / os.getenv("YOUTUBE_CHANNELS_FILE", "youtube_sources_mauricio_v2.json")
API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", "").strip()
YOUTUBE_STATE_KEY = os.getenv("MAURICIO_YOUTUBE_STATE_S3_KEY","mauricio_toro/youtube/state/youtube_state_v1.json")
YOUTUBE_ITEMS_KEY = os.getenv("MAURICIO_YOUTUBE_ITEMS_S3_KEY","mauricio_toro/youtube/brief/youtube_items.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID_MAURICIO", "").strip()
           or os.getenv("TELEGRAM_CHAT_ID_ALERTAS", "").strip()
           or os.getenv("TELEGRAM_CHAT_ID_DEFAULT", "").strip())
MAX_TRANSCRIPT_CHARS_STORED = int(os.getenv("YOUTUBE_MAX_TRANSCRIPT_CHARS_STORED", "18000"))
MAX_ITEMS_STORED = int(os.getenv("YOUTUBE_MAX_ITEMS_STORED", "250"))
PREFERRED_LANGUAGES = ["es", "es-419", "es-ES", "en"]

# En descripción, estos términos aislados son demasiado amplios.
WEAK_DESCRIPTION_TERMS = {
    "ia", "bogotá", "bogota", "tecnología", "tecnologia",
    "educación", "educacion", "universidad", "emprendimiento",
}

# Bogotá por sí sola es ubicación, no tema. Estos conceptos convierten la
# mención geográfica en un asunto urbano / distrital potencialmente relevante.
BOGOTA_CITY_CONTEXT = [
    "alcaldía", "alcaldia", "distrito", "distrital", "galán", "galan",
    "secretaría", "secretaria", "transmilenio", "metro", "movilidad",
    "infraestructura", "obra", "obras", "cable aéreo", "cable aereo",
    "río bogotá", "rio bogota", "ambiente", "ambiental", "car ",
    "vivienda", "espacio público", "espacio publico", "servicios públicos",
    "servicios publicos", "educación", "educacion", "salud pública",
    "salud publica", "seguridad ciudadana", "política pública",
    "politica publica"
]

s3 = boto3.client("s3", region_name=AWS_REGION)

def clean(v):
    v = html.unescape("" if v is None else str(v))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v)).strip()

def norm(v):
    return clean(v).casefold()

def contains(text, term):
    """Evita SENA→senador y otras coincidencias por subcadena."""
    t, q = norm(text), norm(term)
    if not q:
        return False
    return bool(re.search(r"(?<!\w)" + re.escape(q) + r"(?!\w)", t, re.UNICODE))

def api_get(resource, **params):
    if not API_KEY:
        raise RuntimeError("Falta YOUTUBE_API_KEY")
    params["key"] = API_KEY
    r = requests.get("https://www.googleapis.com/youtube/v3/" + resource, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def handle(url):
    p = urlparse(url).path.strip("/")
    return p[1:] if p.startswith("@") else ""

def resolve_channel(ch):
    h = handle(ch["url"])
    if h:
        data = api_get("channels", part="snippet,contentDetails", forHandle=h)
    else:
        ss = api_get("search", part="snippet", q=ch["name"], type="channel", maxResults=5).get("items", [])
        if not ss:
            return None
        cid = ss[0].get("id", {}).get("channelId")
        data = api_get("channels", part="snippet,contentDetails", id=cid)
    items = data.get("items", [])
    if not items:
        return None
    x = items[0]
    return {"channel_id": x["id"], "title": clean(x.get("snippet", {}).get("title", "")),
            "uploads": x.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")}

def uploads(pid, hours, limit):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    data = api_get("playlistItems", part="snippet,contentDetails", playlistId=pid, maxResults=min(50, limit))
    rows = []
    for x in data.get("items", []):
        sn = x.get("snippet", {})
        pub = sn.get("publishedAt", "")
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except Exception:
            dt = None
        if dt and dt < cutoff:
            continue
        vid = x.get("contentDetails", {}).get("videoId")
        if vid:
            rows.append({"video_id": vid, "title": clean(sn.get("title", "")),
                         "description": clean(sn.get("description", "")), "published_at": pub,
                         "url": "https://www.youtube.com/watch?v=" + vid})
    return rows[:limit]

def fetch_transcript(video_id):
    api = YouTubeTranscriptApi()
    try:
        available = list(api.list(video_id))
    except Exception as exc:
        return {"ok": False, "status": f"list_error:{type(exc).__name__}", "snippets": [], "text": ""}
    chosen = None
    for generated_wanted in (False, True):
        for lang in PREFERRED_LANGUAGES:
            for tr in available:
                if tr.language_code == lang and bool(tr.is_generated) == generated_wanted:
                    chosen = tr
                    break
            if chosen: break
        if chosen: break
    if chosen is None:
        chosen = next((tr for tr in available if str(tr.language_code).lower().startswith("es")), None)
    if chosen is None and available:
        chosen = available[0]
    if chosen is None:
        return {"ok": False, "status": "sin_transcripcion", "snippets": [], "text": ""}
    try:
        fetched = chosen.fetch()
        snippets = []
        for sn in fetched:
            txt = clean(getattr(sn, "text", ""))
            if txt:
                snippets.append({"text": txt, "start": float(getattr(sn, "start", 0.0) or 0.0),
                                 "duration": float(getattr(sn, "duration", 0.0) or 0.0)})
        text = clean(" ".join(x["text"] for x in snippets))
        return {"ok": bool(text), "status": "transcript_ok" if text else "transcript_vacio",
                "snippets": snippets, "text": text, "language": str(chosen.language),
                "language_code": str(chosen.language_code), "generated": bool(chosen.is_generated)}
    except Exception as exc:
        return {"ok": False, "status": f"fetch_error:{type(exc).__name__}", "snippets": [], "text": ""}

def clean_description_for_matching(description):
    text = clean(description)
    # Elimina URLs/hashtags y cola promocional típica.
    text = re.sub(r"https?://\S+|www\.\S+|#\S+", " ", text, flags=re.I)
    text = re.sub(
        r"(?i)\b(síguenos|siguenos|suscríbete|suscribete|visita nuestro|"
        r"encuéntranos|encuentranos|más información|mas informacion)\b.*$",
        " ", text
    )
    return clean(text)

def matching_terms(text, terms):
    return [t for t in terms if contains(text, t)]

def _has_any(text, terms):
    return any(contains(text, t) for t in terms)

def _bogota_context_ok(title, description, transcript):
    combined = clean(title + " " + description + " " + transcript)
    # Expresiones de gobierno distrital son suficientes.
    if _has_any(combined, ["alcaldía de bogotá", "alcaldia de bogota",
                           "distrito capital", "secretaría distrital",
                           "secretaria distrital", "carlos fernando galán",
                           "carlos fernando galan"]):
        return True
    # Si aparece Bogotá, exigimos además contexto urbano/público.
    if not contains(combined, "Bogotá"):
        return False
    return _has_any(combined, BOGOTA_CITY_CONTEXT)

def detect_axes_v4(title, description, transcript, axes):
    desc = clean_description_for_matching(description)
    detected, evidence = [], {}
    for axis, terms in axes.items():
        th = matching_terms(title, terms)
        dh = matching_terms(desc, terms)
        xh = matching_terms(transcript, terms)
        specific_dh = [t for t in dh if norm(t) not in WEAK_DESCRIPTION_TERMS]
        desc_ok = bool(specific_dh) or len({norm(t) for t in dh}) >= 2
        qualifies = bool(th or xh or desc_ok)

        # V5: Bogotá/Alcaldía requiere contexto de ciudad/gobierno; Bogotá
        # como mera ubicación geográfica ya no basta.
        if axis == "Bogotá / Alcaldía" and qualifies:
            qualifies = _bogota_context_ok(title, desc, transcript)

        if qualifies:
            detected.append(axis)
            evidence[axis] = {"title": th, "description": dh, "transcript": xh}
    return detected, evidence

def _dedupe_signature(item):
    """Firma simple para evitar que video normal + short ocupen dos lugares."""
    title = norm(item.get("title", ""))
    title = re.sub(r"\bshorts?\b|\bel tiempo\b|#\w+", " ", title)
    title = re.sub(r"[^\wáéíóúüñ]+", " ", title, flags=re.UNICODE)
    stop = {"de","la","el","en","y","a","del","los","las","un","una","tras","sus","está","esta"}
    words = [w for w in title.split() if len(w) > 2 and w not in stop]
    return set(words)

def dedupe_items(items, threshold=0.72):
    kept = []
    for item in items:
        sig = _dedupe_signature(item)
        duplicate = False
        for prev in kept:
            psig = _dedupe_signature(prev)
            if not sig or not psig:
                continue
            similarity = len(sig & psig) / max(1, len(sig | psig))
            if similarity >= threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept

def first_timestamp(snippets, terms):
    for sn in snippets:
        if any(contains(sn.get("text", ""), term) for term in terms):
            return max(0, int(float(sn.get("start", 0) or 0)))
    return None

def context_around_timestamp(snippets, second, radius=5):
    if second is None or not snippets:
        return ""
    hit = min(range(len(snippets)), key=lambda i: abs(float(snippets[i].get("start", 0)) - second))
    lo, hi = max(0, hit-radius), min(len(snippets), hit+radius+1)
    return clean(" ".join(x["text"] for x in snippets[lo:hi]))[:900]

def timestamp_url(video_id, second):
    base = f"https://www.youtube.com/watch?v={video_id}"
    return base if second is None else f"{base}&t={int(second)}s"

def s3_load_json(key, default):
    if not AWS_S3_BUCKET:
        return default
    try:
        obj = s3.get_object(Bucket=AWS_S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") in {"NoSuchKey","404","NotFound"}:
            return default
        raise
    except Exception:
        return default

def s3_save_json(key, data):
    if not AWS_S3_BUCKET:
        raise RuntimeError("Falta AWS_S3_BUCKET")
    s3.put_object(Bucket=AWS_S3_BUCKET, Key=key,
                  Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json; charset=utf-8")

def telegram_send(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram no configurado")
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False}, timeout=30)
    r.raise_for_status()
    return r.json()

def format_colombia_datetime(value):
    """Convierte timestamps ISO/UTC de YouTube a hora local de Colombia."""
    value = clean(value)
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_co = dt.astimezone(ZoneInfo("America/Bogota"))
        return dt_co.strftime("%Y-%m-%d %H:%M:%S Colombia")
    except Exception:
        return value

def build_alert(channel_name, video, second, context):
    url = timestamp_url(video["video_id"], second)
    when = format_colombia_datetime(video.get("published_at", ""))
    lines = ["🟣 Mauricio Toro", "🔎 Detectado: Mauricio Toro",
             f"🎥 {video['title']}", f"🗞 {channel_name}"]
    if when: lines.append(f"📅 {when}")
    if context: lines.append(f"📝 {context}")
    lines.append(f"🔗 {url}")
    return "\n".join(lines)

def run(dry_run=False, lookback_hours=None):
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    hours = lookback_hours or cfg.get("lookback_hours", 24)
    state = s3_load_json(YOUTUBE_STATE_KEY, {"processed": {}, "alerted": {}})
    old_items = s3_load_json(YOUTUBE_ITEMS_KEY, [])
    by_id = {x.get("video_id"): x for x in old_items if x.get("video_id")}
    stats = {"videos":0,"transcripts":0,"relevant":0,"mauricio":0,"alerts":0}

    print("="*100)
    print("YOUTUBE WORKER V5 | " + ("DRY RUN SEGURO" if dry_run else "PRODUCCIÓN"))
    print(f"Lookback: {hours}h | Telegram={'NO' if dry_run else 'SÍ'} | S3={'NO' if dry_run else 'SÍ'}")
    print("="*100)

    for ch in cfg["channels"]:
        if not ch.get("active", True): continue
        print(f"\n📺 {ch['name']}")
        try:
            resolved = resolve_channel(ch)
            if not resolved or not resolved["uploads"]:
                print("   ❌ No se pudo resolver."); continue
            videos = uploads(resolved["uploads"], hours, cfg.get("max_results_per_channel", 15))
            print(f"   Videos recientes: {len(videos)}")
            for v in videos:
                stats["videos"] += 1

                # V4 sólo reutiliza cache si ya fue clasificado por V4.
                cached = by_id.get(v["video_id"])
                if cached and cached.get("classifier_version") == 5 and cached.get("transcript_status") == "transcript_ok":
                    item = cached
                    print(f"   ↩ {v['title'][:70]} | cache V5")
                else:
                    tr = fetch_transcript(v["video_id"])
                    stats["transcripts"] += int(tr.get("ok", False))
                    transcript = clean(tr.get("text", ""))
                    axes, evidence = detect_axes_v4(v["title"], v["description"], transcript, cfg["axes"])

                    person_terms = cfg["priority_person"]["terms"]
                    p_title = matching_terms(v["title"], person_terms)
                    p_desc = matching_terms(clean_description_for_matching(v["description"]), person_terms)
                    p_transcript = matching_terms(transcript, person_terms)
                    mauricio = bool(p_title or p_desc or p_transcript)

                    second = first_timestamp(tr.get("snippets", []), person_terms) if p_transcript else None
                    context = context_around_timestamp(tr.get("snippets", []), second) if second is not None else ""

                    axis_terms = sum((cfg["axes"].get(a, []) for a in axes), [])
                    brief_second = first_timestamp(tr.get("snippets", []), axis_terms) if axes and transcript else None

                    item = {
                        "video_id": v["video_id"], "platform":"youtube", "content_type":"video",
                        "title":v["title"], "channel":ch["name"], "channel_id":resolved["channel_id"],
                        "published_at":v["published_at"], "url":v["url"],
                        "brief_url":timestamp_url(v["video_id"], brief_second),
                        "axes":axes, "axis_evidence":evidence, "classifier_version":5,
                        "mauricio_toro":mauricio,
                        "mauricio_match":{"title":p_title,"description":p_desc,"transcript":p_transcript},
                        "mauricio_timestamp_seconds":second, "mauricio_context":context,
                        "transcript_status":tr.get("status",""),
                        "transcript_language":tr.get("language_code",""),
                        "transcript_generated":tr.get("generated"),
                        "transcript_text":tr.get("text","")[:MAX_TRANSCRIPT_CHARS_STORED],
                        "processed_at_utc":datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z"),
                    }
                    by_id[v["video_id"]] = item

                if item.get("axes"):
                    stats["relevant"] += 1
                    print(f"   ✓ {v['title'][:70]} | {', '.join(item['axes'])}")
                if item.get("mauricio_toro"):
                    stats["mauricio"] += 1
                    if not state.get("alerted", {}).get(v["video_id"]):
                        print(f"   🚨 Mauricio Toro | t={item.get('mauricio_timestamp_seconds')}")
                        if not dry_run:
                            telegram_send(build_alert(ch["name"], v,
                                item.get("mauricio_timestamp_seconds"), item.get("mauricio_context","")))
                            state.setdefault("alerted", {})[v["video_id"]] = datetime.now(timezone.utc).isoformat()
                            stats["alerts"] += 1
                state.setdefault("processed", {})[v["video_id"]] = item.get("processed_at_utc","")
        except Exception as exc:
            print(f"   ❌ ERROR: {type(exc).__name__}: {exc}")

    useful = [x for x in by_id.values() if x.get("axes") or x.get("mauricio_toro")]
    useful.sort(key=lambda x: x.get("published_at",""), reverse=True)
    useful = dedupe_items(useful)
    useful = useful[:MAX_ITEMS_STORED]
    if not dry_run:
        s3_save_json(YOUTUBE_ITEMS_KEY, useful)
        s3_save_json(YOUTUBE_STATE_KEY, state)

    print("\n"+"="*100)
    print(f"TOTAL | videos={stats['videos']} | transcript_ok={stats['transcripts']} | "
          f"relevantes={stats['relevant']} | mauricio={stats['mauricio']} | alertas={stats['alerts']}")
    print("DRY RUN: no se modificó S3 ni Telegram." if dry_run else f"S3: {YOUTUBE_ITEMS_KEY}")
    print("="*100)
    return stats

def main():
    p=argparse.ArgumentParser()
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run",action="store_true")
    mode.add_argument("--once",action="store_true")
    p.add_argument("--lookback-hours",type=int)
    a=p.parse_args()
    run(dry_run=a.dry_run,lookback_hours=a.lookback_hours)

if __name__=="__main__":
    main()
