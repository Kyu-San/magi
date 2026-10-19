#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════
#  MAGI — Wake Check
#  Se ejecuta al arrancar/resumir el sistema.
#  Comprueba si hay mensajes pendientes en Telegram.
#  Si los hay: procesa y se queda despierto.
#  Si no: programa un despertar en 5 min (rtcwake) y hiberna.
# ══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

MAGI_DIR = Path("/home/kyusan/magi")
TOKEN_FILE = MAGI_DIR / ".telegram_token"
CHAT_FILE = MAGI_DIR / ".telegram_chat_id"
WAKE_INTERVAL_MIN = 5  # minutos entre despertares para verificar Telegram


def get_token() -> str | None:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def has_pending_messages(token: str) -> bool:
    """Verifica si hay mensajes pendientes en Telegram."""
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates?limit=1&timeout=5"
        req = Request(url)
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return bool(data.get("result"))
    except Exception:
        return False


def schedule_wake(minutes: int):
    """Programa un despertar con rtcwake."""
    try:
        subprocess.run(
            ["sudo", "rtcwake", "-m", "no", "-s", str(minutes * 60)],
            timeout=10,
            capture_output=True,
        )
        print(f"[wake_check] Despertar programado en {minutes} minutos")
    except Exception as exc:
        print(f"[wake_check] No se pudo programar rtcwake: {exc}")


def hibernate():
    """Hiberna el sistema."""
    print("[wake_check] Hibernando...")
    subprocess.run(["sudo", "systemctl", "hibernate"], timeout=30)


def main():
    # Modo check-only (para sleep hook): solo verificar si hay mensajes
    check_only = "--check-only" in sys.argv

    token = get_token()
    if not token:
        print("[wake_check] No hay token de Telegram.")
        sys.exit(0 if check_only else 1)

    # Esperar a que la red esté disponible
    for _ in range(10):
        try:
            urlopen("https://api.telegram.org", timeout=5)
            break
        except Exception:
            time.sleep(3)
    else:
        print("[wake_check] Sin red.")
        if check_only:
            sys.exit(0)  # permitir hibernación si no hay red
        schedule_wake(1)
        hibernate()
        return

    if has_pending_messages(token):
        print("[wake_check] Hay mensajes pendientes.")
        if check_only:
            sys.exit(1)  # cancelar hibernación
        # Mantener despierto por 10 minutos para dar tiempo
        time.sleep(600)
    else:
        print("[wake_check] Sin mensajes.")
        if check_only:
            sys.exit(0)  # permitir hibernación
        # Programar próximo despertar antes de hibernar
        schedule_wake(WAKE_INTERVAL_MIN)
        hibernate()


if __name__ == "__main__":
    main()
