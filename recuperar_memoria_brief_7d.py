import json
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

BUCKET = os.getenv("AWS_S3_BUCKET", "").strip()
REGION = os.getenv("AWS_REGION", "us-east-2").strip()
STATE_KEY = os.getenv(
    "MAURICIO_NEWS_BRIEF_STATE_S3_KEY",
    "mauricio_toro/news/state/news_brief_state.json",
)
BACKUP_KEY = os.getenv(
    "MAURICIO_NEWS_BRIEF_PRE_V12_BACKUP_KEY",
    "mauricio_toro/news/state/news_brief_state.backup_pre_v12_events_20260919T180714Z.json",
)
WINDOW_HOURS = int(os.getenv("MAURICIO_BRIEF_EVENT_HISTORY_HOURS", "168"))
MAX_EVENTS = int(os.getenv("MAURICIO_BRIEF_MAX_HISTORY_EVENTS", "200"))


def safe(v):
    return "" if v is None else str(v).strip()


def key_for(text):
    text = safe(text).lower()
    text = (text.replace("á","a").replace("é","e").replace("í","i")
                .replace("ó","o").replace("ú","u").replace("ü","u").replace("ñ","n"))
    tokens = re.findall(r"[a-z0-9]+", text)
    return " ".join(tokens)


def get_json(s3, key):
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def active_event(raw, now):
    if not isinstance(raw, dict):
        return False
    event = safe(raw.get("event"))
    ts = safe(raw.get("reported_at_utc"))
    if not event or not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        age = (now - dt).total_seconds() / 3600
        return 0 <= age <= WINDOW_HOURS
    except Exception:
        return False


def merge_events(current, backup, now):
    merged = {}
    for raw in list(backup or []) + list(current or []):
        if not active_event(raw, now):
            continue
        k = key_for(raw.get("event"))
        if not k:
            continue
        previous = merged.get(k)
        if previous is None or safe(raw.get("reported_at_utc")) >= safe(previous.get("reported_at_utc")):
            merged[k] = {
                "event": safe(raw.get("event")),
                "topic": safe(raw.get("topic")),
                "reported_at_utc": safe(raw.get("reported_at_utc")),
            }
    rows = sorted(merged.values(), key=lambda x: x["reported_at_utc"])
    return rows[-MAX_EVENTS:]


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if not BUCKET:
        raise RuntimeError("Falta AWS_S3_BUCKET.")

    s3 = boto3.client("s3", region_name=REGION)
    current = get_json(s3, STATE_KEY)
    try:
        backup = get_json(s3, BACKUP_KEY)
    except ClientError as e:
        print(f"❌ No pude leer backup: {BACKUP_KEY}")
        print(e)
        return

    now = datetime.now(timezone.utc)
    before = current.get("reported_events", [])
    backup_events = backup.get("reported_events", [])
    merged = merge_events(before, backup_events, now)

    before_keys = {key_for(x.get("event")) for x in before if isinstance(x, dict)}
    recovered = [x for x in merged if key_for(x.get("event")) not in before_keys]

    print("=" * 100)
    print("RECUPERACIÓN MEMORIA BRIEF | 7 DÍAS")
    print(f"Estado actual: {len(before)} eventos")
    print(f"Backup: {len(backup_events)} eventos")
    print(f"Memoria resultante activa: {len(merged)} eventos")
    print(f"Recuperados desde backup: {len(recovered)}")
    for row in recovered:
        print(f"  + [{row['topic']}] {row['event']} | {row['reported_at_utc']}")
    print("=" * 100)

    if not args.apply:
        print("DRY RUN: no se modificó S3.")
        print("Si el resultado es correcto, después ejecuta con --apply.")
        return

    # Backup inmediato del estado actual antes de modificarlo.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety_key = f"{STATE_KEY}.backup_pre_7d_recovery_{stamp}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=safety_key,
        Body=json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    current["reported_events"] = merged
    s3.put_object(
        Bucket=BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"✅ S3 actualizado: {STATE_KEY}")
    print(f"🛟 Backup de seguridad: {safety_key}")


if __name__ == "__main__":
    main()
