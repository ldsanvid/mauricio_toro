import os
import signal
import subprocess
import sys
import time

CYCLE_INTERVAL_SECONDS = int(
    os.getenv("GOOGLE_NEWS_CHECK_INTERVAL", "420")
)

CHILD_SCRIPT = os.getenv(
    "MAURICIO_V15_CHILD_SCRIPT",
    "google_news_worker_mauricio_v15_onecycle.py",
).strip()

_stop_requested = False
_child = None


def _handle_signal(signum, frame):
    global _stop_requested, _child
    _stop_requested = True
    print(f"🛑 SUPERVISOR V15 | señal recibida={signum} | cerrando ordenadamente...", flush=True)

    if _child is not None and _child.poll() is None:
        try:
            _child.terminate()
        except Exception:
            pass


def run_child_once() -> int:
    global _child

    started = time.time()

    print("=" * 100, flush=True)
    print("SUPERVISOR V15 | NUEVO PROCESO LIMPIO", flush=True)
    print(f"Child: {CHILD_SCRIPT} --once", flush=True)
    print("=" * 100, flush=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    _child = subprocess.Popen(
        [sys.executable, "-u", CHILD_SCRIPT, "--once"],
        env=env,
    )

    return_code = _child.wait()
    _child = None

    elapsed = int(time.time() - started)

    print(
        f"🧹 SUPERVISOR V15 | proceso terminado | "
        f"exit={return_code} | duración={elapsed}s",
        flush=True,
    )

    return return_code


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print(
        f"🚀 SUPERVISOR V15 ACTIVO | "
        f"intervalo entre ciclos={CYCLE_INTERVAL_SECONDS}s",
        flush=True,
    )

    while not _stop_requested:
        try:
            return_code = run_child_once()

            if return_code != 0:
                print(
                    f"⚠️ SUPERVISOR V15 | child terminó con exit={return_code}; "
                    "se intentará de nuevo en el siguiente ciclo.",
                    flush=True,
                )

        except Exception as error:
            print(
                f"❌ SUPERVISOR V15 | error lanzando child: {error}",
                flush=True,
            )

        if _stop_requested:
            break

        print(
            f"💤 SUPERVISOR V15 | esperando {CYCLE_INTERVAL_SECONDS}s "
            "antes de crear un proceso nuevo...",
            flush=True,
        )

        slept = 0
        while slept < CYCLE_INTERVAL_SECONDS and not _stop_requested:
            step = min(5, CYCLE_INTERVAL_SECONDS - slept)
            time.sleep(step)
            slept += step

    print("✅ SUPERVISOR V15 | detenido.", flush=True)


if __name__ == "__main__":
    main()
