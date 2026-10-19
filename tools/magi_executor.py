#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════
#  MAGI EXECUTOR — puente responsable entre el MAGI (contenedor) y el host
#
#  - Escucha SOLO en 127.0.0.1 (nunca LAN). Autenticación por token hex.
#  - Corre como usuario humano (KYUSAN); NUNCA con sudo elevado.
#  - argv SIEMPRE lista (sin shell), lista blanca de binarios permitidos
#    y patrones absolutamente denegados (última línea de defensa).
#  - Bitácora completa en logs/executions.jsonl.
#
#  Uso:  python3 tools/magi_executor.py         (o via systemctl --user)
# ══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # /home/kyusan/magi
TOKEN_FILE = BASE / ".magi_token"
AUDIT = BASE / "logs" / "executions.jsonl"
PORT = int(os.environ.get("MAGI_EXEC_PORT", "7676"))
MAX_OUT = 8000

# Binarios a los que el MAGI tiene acceso directo en el host
ALLOWED_BIN = {
    "git",
    "docker",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "file",
    "grep",
    "find",
    "curl",
    "jq",
    "cp",
    "mv",
    "mkdir",
    "touch",
    "diff",
    "stat",
    "du",
    "df",
    "tar",
    "gzip",
    "python3",
    "make",
}
# Patrones absolutamente prohibidos (Ley 1) — el ejecutor NO negocia
DENIED = re.compile(
    r"\bsudo\b|\bsu\s+-|\bsu\s+\w|\b(shutdown|reboot|halt|poweroff)\b"
    r"|\bmkfs\b|\bfdisk\b|\bparted\b|of=/dev/(sd|nvme|hd)"
    r"|:\(\)\s*\{.*\};\s*:|chmod\s+(-R\s+)?777"
    r"|\bid_rsa\b|\.ssh\b|\.aws\b|\.gpg\b|\.kube/config"
    r"|docker\s+system\s+prune|docker\s+rm|docker\s+rmi"
    r"|rm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)?/($|\s)"
    r"|(ba|z|k)?sh\s",
    re.I,
)


def _audita(reg: dict):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    reg["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(AUDIT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(reg, ensure_ascii=False) + "\n")


def _ejecuta(argv: list[str], timeout: float, cwd: str) -> dict:
    if not argv or not all(isinstance(a, str) for a in argv):
        return {"rc": 4, "stdout": "", "stderr": "argv inválido"}
    if argv[0] not in ALLOWED_BIN:
        return {
            "rc": 5,
            "stdout": "",
            "stderr": f"binario no permitido: {argv[0]!r} (lista blanca del ejecutor)",
        }
    unido = " ".join(argv)
    m = DENIED.search(unido)
    if m:
        _audita({"tipo": "denegado", "argv": argv, "motivo": m.group(0)})
        return {
            "rc": 6,
            "stdout": "",
            "stderr": f"Ley 1 — patrón denegado: {m.group(0)!r}",
        }
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(5.0, min(float(timeout or 60), 600)),
            cwd=cwd if Path(cwd).is_dir() else str(BASE),
        )
        out = r.stdout[-MAX_OUT:]
        err = r.stderr[-MAX_OUT:]
        rc = r.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", "timeout", 3
    except Exception as exc:  # noqa: BLE001
        out, err, rc = "", f"{type(exc).__name__}: {exc}", 2
    _audita({"tipo": "exec", "argv": argv, "rc": rc})
    return {"rc": rc, "stdout": out, "stderr": err}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # modo silencio (auditoría propia)
        return

    def _res(self, code: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/healthz":
            return self._res(200, {"ok": True, "servicio": "MAGI-EXECUTOR"})
        self._res(404, {"error": "ruta desconocida"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:  # noqa: BLE001
            return self._res(400, {"error": f"cuerpo inválido: {exc}"})
        if body.get("token") != TOKEN:
            _audita({"tipo": "token_rechazado"})
            return self._res(401, {"error": "token inválido"})
        if self.path == "/v1/exec":
            r = _ejecuta(
                body.get("argv") or [],
                body.get("timeout", 60),
                body.get("cwd", str(BASE)),
            )
            return self._res(200, r)
        self._res(404, {"error": "ruta desconocida"})


def main():
    global TOKEN
    if not TOKEN_FILE.exists():
        import secrets

        TOKEN_FILE.write_text(secrets.token_hex(32))
        os.chmod(TOKEN_FILE, 0o600)
        print(f"[MAGI-EXEC] token generado en {TOKEN_FILE} (permembrane 600)")
    TOKEN = TOKEN_FILE.read_text().strip()
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    httpd = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[MAGI-EXEC] a la escucha en 127.0.0.1:{PORT} · usuario: {os.getuid()}")
    print(f"[MAGI-EXEC] binarios permitidos: {', '.join(sorted(ALLOWED_BIN))}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[MAGI-EXEC] fuera de línea. Despedida.")
        sys.exit(0)


if __name__ == "__main__":
    main()
