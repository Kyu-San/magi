#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════
#  MAGI — Telegram Bot
#  Puente entre Telegram y el MAGI System (Docker).
#  Comandos:
#    /start        → bienvenida
#    /ask <texto>  → deliberación única
#    /encargo <t>  → AGENCIA (planes→voto→ejecución)
#    /mejorar      → auto-mejora del código
#    /doctor       → diagnóstico
#    /status       → estado del sistema
#    <texto libre> → se trata como /ask
#
#  Uso:  python3 tools/telegram_bot.py
#  Requiere: TELEGRAM_BOT_TOKEN en env o en ~/magi/.telegram_token
#  Servicio: systemctl --user enable --now magi-telegram
# ══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

MAGI_DIR = Path("/home/kyusan/magi")
COMPOSE = "docker compose -f /home/kyusan/magi/docker-compose.yml"
TOKEN_FILE = MAGI_DIR / ".telegram_token"
POLL_INTERVAL = 3  # segundos entre polls
MAX_MSG_LEN = 4000  # límite de Telegram


def get_token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
    if not tok:
        print("ERROR: No se encontró token de Telegram.")
        print("Crea un bot con @BotFather y guarda el token en:")
        print(f"  {TOKEN_FILE}")
        print("  o en la variable de entorno TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    return tok


def tg_api(token: str, method: str, data: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def send_message(token: str, chat_id: int, text: str, parse_mode: str = "HTML"):
    """Envía un mensaje, dividiéndolo si excede el límite de Telegram."""
    # Telegram no permite HTML mal formado; usamos texto plano con markdown simple
    chunks = []
    while len(text) > MAX_MSG_LEN:
        # Cortar en el último salto de línea antes del límite
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut == -1:
            cut = MAX_MSG_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)

    for chunk in chunks:
        try:
            tg_api(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                },
            )
        except Exception:
            # Reintentar sin parse_mode si falla
            tg_api(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                },
            )


def run_magi(cmd: str, args: str = "") -> str:
    """Ejecuta un comando del MAGI en Docker y devuelve la salida."""
    # Adquirir inhibitor de suspensión mientras se procesa
    inhibitor = None
    try:
        inhibitor = subprocess.Popen(
            [
                "systemd-inhibit",
                "--what=sleep",
                "--who=MAGI-Telegram",
                "--why=Procesando solicitud del MAGI",
                "--mode=block",
                "sleep",
                "900",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    full_cmd = f"{COMPOSE} run --rm magi {cmd}"
    if args:
        full_cmd += f' "{args}"'
    try:
        result = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=900,  # 15 min max
            cwd=str(MAGI_DIR),
        )
        output = result.stdout + result.stderr
        # Limpiar salida de Docker (quitar "Container ... Running")
        lines = output.splitlines()
        clean = []
        for line in lines:
            if line.strip().startswith("Container ") and "Running" in line:
                continue
            clean.append(line)
        text = "\n".join(clean).strip()
        if result.returncode != 0 and not text:
            text = f"Error (exit {result.returncode}): {result.stderr[:500]}"
        return text or "(sin salida)"
    except subprocess.TimeoutExpired:
        return "⏱ Timeout: el MAGI tardó más de 15 minutos. Intenta con una consulta más simple."
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if inhibitor:
            try:
                inhibitor.terminate()
            except Exception:
                pass


def handle_command(token: str, chat_id: int, text: str):
    """Procesa un comando o mensaje del usuario."""
    text = text.strip()
    if not text:
        return

    # Parsear comando
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # quitar @botname
        args = parts[1] if len(parts) > 1 else ""
    else:
        cmd = "/ask"
        args = text

    if cmd == "/start":
        send_message(
            token,
            chat_id,
            "🧠 <b>MAGI SYSTEM — NERV Tokyo-3</b>\n\n"
            "Tres superordenadores deliberan y votan por ti:\n"
            "• MELCHIOR-1 (la científica)\n"
            "• BALTHASAR-2 (la madre)\n"
            "• CASPER-3 (la mujer)\n\n"
            "Comandos:\n"
            "/ask <pregunta> — deliberación\n"
            "/encargo <tarea> — AGENCIA (ejecución real)\n"
            "/mejorar — auto-mejora del código\n"
            "/doctor — diagnóstico\n"
            "/status — estado del sistema\n\n"
            "O simplemente escribe tu pregunta.",
        )
        return

    if cmd == "/doctor":
        send_message(token, chat_id, "🔍 Ejecutando diagnóstico…")
        output = run_magi("doctor")
        send_message(token, chat_id, f"<pre>{output}</pre>")
        return

    if cmd == "/status":
        try:
            import urllib.request

            req = urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=5)
            health = json.loads(req.read())
            status = "✅ Ollama OK" if health.get("ok") else "❌ Ollama no responde"
            models = health.get("modelos", "?")
            send_message(
                token,
                chat_id,
                f"{status}\nModelos: {models}\nWeb GUI: http://localhost:8080",
            )
        except Exception:
            send_message(
                token, chat_id, "❌ No se puede conectar al MAGI. ¿Está corriendo?"
            )
        return

    if cmd == "/ask":
        if not args:
            send_message(token, chat_id, "Uso: /ask <pregunta>")
            return
        send_message(token, chat_id, "🧠 Deliberando… (2-3 min en CPU)")
        output = run_magi("ask", args)
        send_message(token, chat_id, f"<pre>{output}</pre>")

    elif cmd == "/encargo":
        if not args:
            send_message(token, chat_id, "Uso: /encargo <tarea>")
            return
        send_message(
            token,
            chat_id,
            "⚡ AGENCIA activada. Los tres módulos diseñan planes… (5-10 min)",
        )
        output = run_magi("encargo", args)
        send_message(token, chat_id, f"<pre>{output}</pre>")

    elif cmd == "/mejorar":
        objetivo = (
            args if args else "Analiza tu código y aplica una mejora pequeña y segura."
        )
        send_message(token, chat_id, "🔧 Auto-mejora en curso… (5-10 min)")
        output = run_magi("mejorar", objetivo)
        send_message(token, chat_id, f"<pre>{output}</pre>")

    else:
        send_message(
            token,
            chat_id,
            f"Comando desconocido: {cmd}\nUsa /start para ver los comandos.",
        )


def main():
    token = get_token()
    print(f"[MAGI-TELEGRAM] Bot iniciado. Token: {token[:8]}…")

    # Obtener info del bot
    me = tg_api(token, "getMe")
    print(
        f"[MAGI-TELEGRAM] Bot: @{me['result']['username']} ({me['result']['first_name']})"
    )

    offset = 0
    while True:
        try:
            updates = tg_api(token, "getUpdates", {"offset": offset, "timeout": 30})
            for upd in updates.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")
                if chat_id and text:
                    # Guardar chat_id para el monitor de GitHub
                    chat_file = Path("/home/kyusan/magi/.telegram_chat_id")
                    if not chat_file.exists():
                        chat_file.write_text(str(chat_id))
                    # Procesar en un hilo para no bloquear el polling
                    import threading

                    t = threading.Thread(
                        target=handle_command, args=(token, chat_id, text), daemon=True
                    )
                    t.start()
        except Exception as exc:
            print(f"[MAGI-TELEGRAM] Error: {type(exc).__name__}: {str(exc)[:100]}")
            time.sleep(5)


if __name__ == "__main__":
    main()
