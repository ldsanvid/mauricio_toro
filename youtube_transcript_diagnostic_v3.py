#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
youtube_transcript_diagnostic_v3.py

Prueba AISLADA de recuperación de transcripciones para el monitor de YouTube
de Mauricio Toro.

NO:
- envía Telegram
- escribe S3
- modifica el brief
- modifica estados de producción

Uso:
    python youtube_transcript_diagnostic_v3.py
    python youtube_transcript_diagnostic_v3.py VIDEO_ID
    python youtube_transcript_diagnostic_v3.py https://www.youtube.com/watch?v=VIDEO_ID
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError:
    print(
        "\nERROR: falta youtube-transcript-api.\n"
        "Instálalo con:\n\n"
        "    pip install youtube-transcript-api\n"
    )
    raise


# Casos reales tomados del diagnóstico V2:
# - uno que Data API declaró captions
# - varios que declaró not_declared
DEFAULT_TESTS = [
    {
        "video_id": "xWhMnEDDicI",
        "label": "RCN | captions_declared en V2",
    },
    {
        "video_id": "HSZJTbDaWOw",
        "label": "RCN | emisión larga | captions_declared en V2",
    },
    {
        "video_id": "CJu3iW8tO2A",
        "label": "Caracol | LGBTIQ+ | not_declared en V2",
    },
    {
        "video_id": "XufrMYgbC0o",
        "label": "La Silla Vacía | ChatGPT | not_declared en V2",
    },
    {
        "video_id": "dQdi99G9zi4",
        "label": "Caracol Radio | IA Colombia | not_declared en V2",
    },
]

PREFERRED_LANGUAGES = ["es", "es-419", "es-ES", "en"]


@dataclass
class Result:
    video_id: str
    ok: bool
    status: str
    language: str = ""
    language_code: str = ""
    generated: Optional[bool] = None
    chars: int = 0
    snippets: int = 0
    preview: str = ""


def extract_video_id(value: str) -> str:
    value = (value or "").strip()

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value

    try:
        parsed = urlparse(value)

        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            candidate = parsed.path.strip("/").split("/")[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
                return candidate

        if parsed.hostname and "youtube.com" in parsed.hostname:
            if parsed.path == "/watch":
                candidate = parse_qs(parsed.query).get("v", [""])[0]
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
                    return candidate

            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
                candidate = parts[1]
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
                    return candidate
    except Exception:
        pass

    raise ValueError(f"No pude extraer un video_id válido de: {value}")


def clean_piece(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def fetch_best_transcript(video_id: str) -> Result:
    api = YouTubeTranscriptApi()

    try:
        transcript_list = api.list(video_id)
    except Exception as exc:
        return Result(
            video_id=video_id,
            ok=False,
            status=f"list_error:{type(exc).__name__}: {exc}",
        )

    available = list(transcript_list)

    if not available:
        return Result(
            video_id=video_id,
            ok=False,
            status="sin_transcripciones_listadas",
        )

    # Priorizamos español manual, español generado, y luego cualquier español.
    chosen = None

    for generated_wanted in (False, True):
        for lang in PREFERRED_LANGUAGES:
            for tr in available:
                if (
                    tr.language_code == lang
                    and bool(tr.is_generated) == generated_wanted
                ):
                    chosen = tr
                    break
            if chosen:
                break
        if chosen:
            break

    if chosen is None:
        for tr in available:
            if str(tr.language_code).lower().startswith("es"):
                chosen = tr
                break

    # Último fallback: cualquier transcript disponible.
    if chosen is None:
        chosen = available[0]

    try:
        fetched = chosen.fetch()
        pieces = [clean_piece(snippet.text) for snippet in fetched]
        pieces = [p for p in pieces if p]
        text = " ".join(pieces)
        text = re.sub(r"\s+", " ", text).strip()

        preview = text[:700]
        if len(text) > 700:
            preview += "…"

        return Result(
            video_id=video_id,
            ok=bool(text),
            status="transcript_ok" if text else "transcript_vacio",
            language=str(chosen.language),
            language_code=str(chosen.language_code),
            generated=bool(chosen.is_generated),
            chars=len(text),
            snippets=len(pieces),
            preview=preview,
        )

    except Exception as exc:
        return Result(
            video_id=video_id,
            ok=False,
            status=f"fetch_error:{type(exc).__name__}: {exc}",
            language=str(chosen.language),
            language_code=str(chosen.language_code),
            generated=bool(chosen.is_generated),
        )


def print_result(label: str, result: Result) -> None:
    print("-" * 100)
    print(f"VIDEO: {label}")
    print(f"ID: {result.video_id}")
    print(f"URL: https://www.youtube.com/watch?v={result.video_id}")
    print(f"RESULTADO: {'OK' if result.ok else 'NO'}")
    print(f"STATUS: {result.status}")

    if result.language_code:
        kind = (
            "automática"
            if result.generated is True
            else "manual"
            if result.generated is False
            else "desconocida"
        )
        print(
            f"TRANSCRIPT: {result.language} ({result.language_code}) | "
            f"{kind}"
        )

    if result.ok:
        print(f"TAMAÑO: {result.chars:,} caracteres | {result.snippets:,} fragmentos")
        print("\nPREVIEW:")
        print(result.preview)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "video",
        nargs="?",
        help="Video ID o URL. Si se omite, corre la batería segura de 5 videos.",
    )
    args = parser.parse_args()

    print("=" * 100)
    print("YOUTUBE TRANSCRIPT V3 | DIAGNÓSTICO AISLADO")
    print("Telegram=NO | S3=NO | Brief=NO | Estado producción=NO")
    print("=" * 100)

    if args.video:
        try:
            tests = [
                {
                    "video_id": extract_video_id(args.video),
                    "label": "video solicitado",
                }
            ]
        except ValueError as exc:
            print(exc)
            sys.exit(2)
    else:
        tests = DEFAULT_TESTS

    results = []

    for test in tests:
        result = fetch_best_transcript(test["video_id"])
        results.append(result)
        print_result(test["label"], result)

    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok

    print("=" * 100)
    print(
        f"TOTAL: {len(results)} | TRANSCRIPCIÓN OK: {ok} | "
        f"SIN TRANSCRIPCIÓN: {fail}"
    )

    if results:
        print(f"TASA DE RECUPERACIÓN: {(ok / len(results)) * 100:.1f}%")

    print("No se modificó S3. No se envió Telegram. No se tocó el brief.")
    print("=" * 100)


if __name__ == "__main__":
    main()
