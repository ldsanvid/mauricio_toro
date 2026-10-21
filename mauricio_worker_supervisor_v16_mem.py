"""Supervisor de bajo consumo RAM para Mauricio Toro.

Etapas estrictamente secuenciales, cada una en su propio proceso Python:
RSS+Telegram -> X (si corresponde) -> YouTube -> brief programado.

Para revertir: Start Command anterior `python -u mauricio_worker_supervisor_v15.py`.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

INTERVAL = int(os.getenv("GOOGLE_NEWS_CHECK_INTERVAL", "420"))
X_INTERVAL = int(os.getenv("X_CHECK_INTERVAL", "900"))
X_LAST_RUN_FILE = Path(os.getenv("MAURICIO_X_LAST_RUN_FILE", "/tmp/mauricio_toro_x_last_run.txt"))
RSS_SCRIPT = os.getenv("MAURICIO_V16_RSS_SCRIPT", "google_news_rss_only_v16.py").strip()
X_SCRIPT = os.getenv("MAURICIO_V16_X_SCRIPT", "x_worker_mauricio_v2.py").strip()
YOUTUBE_SCRIPT = os.getenv("MAURICIO_V16_YOUTUBE_SCRIPT", "youtube_worker_mauricio_v6.py").strip()
BRIEF_SCRIPT = os.getenv("MAURICIO_V16_BRIEF_SCRIPT", "news_brief_mauricio_ejes_v17.py").strip()
STOP = False
CHILD = None


def on_signal(signum, _frame):
    global STOP
    STOP = True
    print(f"🛑 SUPERVISOR V16 | señal={signum}", flush=True)
    if CHILD is not None and CHILD.poll() is None:
        CHILD.terminate()


def stage(label: str, script: str, *args: str) -> int:
    global CHILD
    if STOP:
        return 130
    print("=" * 80, flush=True)
    print(f"▶️ V16 | INICIO {label}: {script} {' '.join(args)}", flush=True)
    started = time.monotonic()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    CHILD = subprocess.Popen([sys.executable, "-u", script, *args], env=env)
    try:
        result = CHILD.wait()
    finally:
        CHILD = None
    print(f"🏁 V16 | FIN {label} | exit={result} | duración={int(time.monotonic()-started)}s", flush=True)
    return result


def x_due() -> bool:
    try:
        timestamp = float(X_LAST_RUN_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return True
    return time.time() - timestamp >= X_INTERVAL


def save_x_run() -> None:
    try:
        X_LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        X_LAST_RUN_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError as error:
        print(f"⚠️ V16 | X: no se pudo registrar último run: {error}", flush=True)


def one_cycle() -> None:
    # Protección: si RSS muere, no se procesan etapas dependientes con datos antiguos.
    if stage("GOOGLE NEWS + ALERTAS", RSS_SCRIPT, "--once") != 0 or STOP:
        print("⚠️ V16 | RSS falló: omitiendo X/YouTube/brief en este ciclo", flush=True)
        return

    if x_due() and not STOP:
        if stage("X", X_SCRIPT, "--once") == 0:
            save_x_run()
    else:
        print("⏭️ V16 | X: todavía no corresponde", flush=True)

    # YouTube debe ejecutarse antes del brief para incluir sus resultados recientes.
    if not STOP:
        stage("YOUTUBE", YOUTUBE_SCRIPT, "--once")
    if not STOP:
        stage("BRIEF", BRIEF_SCRIPT, "--run")


def main() -> None:
    global STOP
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print("🚀 SUPERVISOR V16 | procesos secuenciales, RAM liberada tras cada etapa", flush=True)
    while not STOP:
        try:
            one_cycle()
        except Exception as error:
            print(f"❌ SUPERVISOR V16 | error: {type(error).__name__}: {error}", flush=True)
        if STOP:
            break
        print(f"💤 SUPERVISOR V16 | descanso {INTERVAL}s", flush=True)
        remaining = INTERVAL
        while remaining > 0 and not STOP:
            step = min(5, remaining)
            time.sleep(step)
            remaining -= step
    print("✅ SUPERVISOR V16 | detenido", flush=True)


if __name__ == "__main__":
    main()
