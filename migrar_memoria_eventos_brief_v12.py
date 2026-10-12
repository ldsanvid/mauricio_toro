#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Inicializa reported_events para Brief V12 a partir de los briefs ya enviados
el 18 y 19 de septiembre de 2026.

Por defecto: DRY RUN (sólo lectura).
Para aplicar: python migrar_memoria_eventos_brief_v12.py --apply

- Conserva TODO el estado existente.
- No modifica reported_titles ni reported_bullets.
- No envía Telegram.
- Crea una copia de respaldo del estado antes de escribir.
- Evita duplicar eventos si se ejecuta dos veces.
"""
import argparse
import copy
from datetime import datetime, timezone

from news_s3_store import load_json_state, save_json_state
import news_brief_mauricio_ejes_v12_fix2 as brief

STATE_KEY = brief.BRIEF_STATE_KEY

# Fechas aproximadas de comunicación real de cada acontecimiento.
SEED_EVENTS = [
    # Brief 18/09 12:00
    ("Tecnología",
     "David Vélez fue nombrado asesor ad honorem de la Presidencia de Colombia en materia de inteligencia artificial.",
     "2026-09-18T17:00:00Z"),
    ("Economía digital",
     "Las fintechs colombianas proyectaron invertir aproximadamente 4,3 billones de pesos en los siguientes 12 meses.",
     "2026-09-18T17:00:00Z"),
    ("Alcalde Galán",
     "Carlos Fernando Galán confirmó la fecha de inicio o apertura de los primeros tramos de TransMilenio por la avenida 68.",
     "2026-09-18T17:00:00Z"),
    ("Alcaldía de Bogotá",
     "Se reportó la presencia de cerca de 100 especies de fauna en la cuenca media del río Bogotá.",
     "2026-09-18T17:00:00Z"),
    ("Diversidad",
     "Colombia se retiró de la Coalición por la Igualdad de Derechos que promueve la protección de las personas LGBTIQ+.",
     "2026-09-18T17:00:00Z"),
    ("Educación",
     "Revista Raya publicó un análisis sobre acciones del presidente Abelardo De la Espriella hacia las universidades públicas de Colombia.",
     "2026-09-18T17:00:00Z"),

    # Brief 19/09 08:04
    ("Educación",
     "ICETEX reportó un incidente de seguridad que afectó varios de sus servicios y trámites en sus plataformas.",
     "2026-09-19T13:04:00Z"),
    ("Economía digital",
     "Se reportó un cambio en la preeminencia de bitcoin dentro del mercado de criptoactivos en Colombia.",
     "2026-09-19T13:04:00Z"),
    ("Economía digital",
     "La Policía Nacional reportó haber recuperado 4.800 millones de pesos en criptoactivos vinculados a fraudes.",
     "2026-09-19T13:04:00Z"),
    ("Tecnología",
     "Habi anunció que está escalando su operación mediante el uso de inteligencia artificial.",
     "2026-09-19T13:04:00Z"),

    # Brief 19/09 12:02
    ("Educación",
     "ICETEX anunció que 170.000 beneficiarios recibirán desde octubre un subsidio temporal a la tasa de interés.",
     "2026-09-19T17:02:00Z"),
    ("Educación",
     "La Gobernación de Santander firmó una alianza territorial con ICETEX, presentada como la primera de este tipo en el actual gobierno.",
     "2026-09-19T17:02:00Z"),
    ("Economía digital",
     "Colombia Fintech pidió desmontar el gravamen 4x1.000 y ampliar Bre-B para impulsar los pagos digitales.",
     "2026-09-19T17:02:00Z"),
    ("Diversidad",
     "La Defensoría del Pueblo señaló que el retiro de Colombia de la Coalición por la Igualdad de Derechos no puede reducir la protección de las personas LGBTIQ+.",
     "2026-09-19T17:02:00Z"),
    ("Alcaldía de Bogotá",
     "Se reportaron retrasos en la renovación del estadio El Campín y críticas a la APP encargada del proyecto.",
     "2026-09-19T17:02:00Z"),
]

def norm_event(text):
    # Usa la normalización real disponible en el brief.
    return " ".join(sorted(brief.normalize_title_tokens(text)))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Aplica la migración. Sin esta bandera sólo hace dry-run.")
    args = ap.parse_args()

    state, exists = load_json_state(STATE_KEY)
    if not isinstance(state, dict):
        state = {}

    existing = state.get("reported_events", [])
    if not isinstance(existing, list):
        existing = []

    existing_keys = {
        norm_event(str(x.get("event", "")))
        for x in existing if isinstance(x, dict) and x.get("event")
    }

    additions = []
    for topic, event, ts in SEED_EVENTS:
        key = norm_event(event)
        if key and key not in existing_keys:
            additions.append({
                "event": event,
                "topic": topic,
                "reported_at_utc": ts,
            })
            existing_keys.add(key)

    print("=" * 110)
    print("MIGRACIÓN MEMORIA DE EVENTOS | BRIEF V12")
    print("MODO:", "APLICAR" if args.apply else "DRY RUN / SOLO LECTURA")
    print("STATE KEY:", STATE_KEY)
    print("Estado existente:", "SÍ" if exists else "NO")
    print("Eventos ya existentes:", len(existing))
    print("Eventos semilla únicos a agregar:", len(additions))
    print("=" * 110)

    for x in additions:
        print(f"+ [{x['topic']}] {x['event']} | {x['reported_at_utc']}")

    if not args.apply:
        print("=" * 110)
        print("NO se modificó S3. Para aplicar: python migrar_memoria_eventos_brief_v12.py --apply")
        print("=" * 110)
        return

    # Backup íntegro antes de tocar el estado.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_key = STATE_KEY.rsplit(".", 1)[0] + f".backup_pre_v12_events_{stamp}.json"
    save_json_state(backup_key, copy.deepcopy(state))

    new_state = copy.deepcopy(state)
    new_state["reported_events"] = (existing + additions)[-brief.MAX_HISTORY_EVENTS:]
    save_json_state(STATE_KEY, new_state)

    # Verificación de lectura.
    verify, ok = load_json_state(STATE_KEY)
    saved = verify.get("reported_events", []) if isinstance(verify, dict) else []

    print("=" * 110)
    print("✅ MIGRACIÓN APLICADA")
    print("Backup:", backup_key)
    print("Eventos guardados:", len(saved))
    print("reported_titles preservados:", len(verify.get("reported_titles", [])) if isinstance(verify, dict) else "?")
    print("reported_bullets preservados:", len(verify.get("reported_bullets", [])) if isinstance(verify, dict) else "?")
    print("Telegram: NO")
    print("=" * 110)

if __name__ == "__main__":
    main()
