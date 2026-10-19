#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════
#  MAGI SYSTEM — Web GUI (NERV Tokyo-3)
#  Servidor FastAPI que expone la deliberación del MAGI en tiempo real
#  via Server-Sent Events. Frontend: static/index.html (diseño hexagonal).
# ══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

# Asegura que magi.py esté importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

import magi
from magi import MODULOS, deliberar, salud, guarda_log, BANNER
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI(title="MAGI System — NERV Tokyo-3")
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Estado compartido de la última deliberación
_estado = {"running": False, "pregunta": None, "registro": None, "resolucion": None}
_lock = threading.Lock()
_subscribers: list = []
_sub_lock = threading.Lock()


def _broadcast(event: str, data: dict):
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait((event, data))
            except Exception:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _run_deliberacion(pregunta: str):
    global _estado
    with _lock:
        _estado = {"running": True, "pregunta": pregunta, "registro": None, "resolucion": None}

    _broadcast("inicio", {"pregunta": pregunta})

    # Deliberar con callback de progreso
    t0 = time.time()
    estado = {m["codename"]: {"status": "deliberando", "seg": 0.0} for m in MODULOS}
    lock = threading.Lock()

    def _broadcast_estado():
        payload = {
            cod: {
                "status": e["status"],
                "aprobado": e.get("aprobado"),
                "confianza": e.get("confianza"),
                "razon": e.get("razon", ""),
                "error": e.get("error", ""),
                "seg": e.get("seg", 0.0),
            }
            for cod, e in estado.items()
        }
        _broadcast("estado", payload)

    def _tarea(mod, preg):
        from magi import consulta_magi, _tarea_megiddo
        return _tarea_megiddo(mod, preg)

    with magi.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for mod in MODULOS:
            futures[pool.submit(_tarea, mod, pregunta)] = mod["codename"]
        for fut in magi.as_completed(futures):
            cod = futures[fut]
            with lock:
                try:
                    r = fut.result()
                    estado[cod].update(
                        status="ok",
                        aprobado=r["aprobado"],
                        confianza=r["confianza"],
                        razon=r["razon"],
                        seg=time.time() - t0,
                    )
                except Exception as exc:
                    msg = str(exc).split("For more information check:")[0].strip()
                    estado[cod].update(
                        status="error", error=msg[:160], seg=time.time() - t0
                    )
            _broadcast_estado()

    res = magi.votacion(
        {
            cod: {"aprobado": e.get("aprobado") if e["status"] == "ok" else None}
            for cod, e in estado.items()
        }
    )

    registro = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pregunta": pregunta,
        "votos": {
            cod: {k: v for k, v in e.items() if k != "status"} | {"status": e["status"]}
            for cod, e in estado.items()
        },
        "resolucion": res,
        "segundos_total": round(time.time() - t0, 1),
    }
    magi.guarda_log(registro)

    with _lock:
        _estado = {"running": False, "pregunta": pregunta, "registro": registro, "resolucion": res}

    _broadcast("resolucion", {"resolucion": res, "registro": registro})


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status():
    with _lock:
        return JSONResponse(_estado)


@app.post("/api/deliberate")
async def deliberate(request: Request):
    body = await request.json()
    pregunta = body.get("pregunta", "").strip()
    if not pregunta:
        return JSONResponse({"error": "pregunta vacía"}, status_code=400)
    with _lock:
        if _estado.get("running"):
            return JSONResponse({"error": "ya hay una deliberación en curso"}, status_code=409)
    t = threading.Thread(target=_run_deliberacion, args=(pregunta,), daemon=True)
    t.start()
    return JSONResponse({"ok": True, "pregunta": pregunta})


@app.get("/api/stream")
async def stream(request: Request):
    import queue
    q = queue.Queue()
    with _sub_lock:
        _subscribers.append(q)

    async def event_generator():
        # Enviar estado actual
        with _lock:
            yield f"event: estado\ndata: {json.dumps(_estado, ensure_ascii=False, default=str)}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                event, data = q.get(timeout=1.0)
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            except Exception:
                yield ": heartbeat\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/health")
def health():
    try:
        tags = magi.client.get(f"{magi.OLLAMA}/api/tags", timeout=8).json().get("models", [])
        return {"ok": True, "modelos": len(tags)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
