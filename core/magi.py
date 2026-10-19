#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════
#  MAGI SYSTEM — NERV Tokyo-3
#  MELCHIOR-1 · BALTHASAR-2 · CASPER-3
#  Orquestador de deliberación y votación por patrones.
#
#  Uso:
#    python magi.py                 → TUI interactiva (REPL)
#    python magi.py ask "pregunta"  → deliberación única
#    python magi.py encargo "tarea" → AGENCIA: planes → voto → EJECUCIÓN real
#    python magi.py mejorar         → el MAGI reescribe su propio código
#    python magi.py doctor          → diagnóstico del sistema
#    python magi.py --plain ask "…" → salida plana (sin Live, para piping)
# ══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import yaml
from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ─── Config ────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("MAGI_CONFIG", "/app/config.yaml")
LOG_DIR = Path(os.environ.get("MAGI_LOG_DIR", "/app/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

OLLAMA = os.environ.get(
    "MAGI_OLLAMA_HOST", CFG.get("ollama", {}).get("endpoint", "http://ollama:11434")
).rstrip("/")

GEN = CFG.get("generacion", {})
NUM_CTX = int(GEN.get("num_ctx", 2048))
NUM_PREDICT = int(GEN.get("num_predict", 420))
READ_TIMEOUT = int(GEN.get("timeout_seg", 900))

MODULOS = []  # order matters: Melchior, Balthasar, Casper
for key in ("melchior", "balthasar", "casper"):
    m = CFG["modulos"][key]
    MODULOS.append(
        {
            "key": key,
            "codename": m["codename"],
            "forma": m.get("forma", ""),
            "modelo": m["modelo"],
            "temperatura": float(m.get("temperatura", 0.3)),
            "color": m.get("color", "cyan"),
            "persona": m["persona"].strip(),
            "encuadre": CFG.get("encuadre_comun", "").strip(),
            "num_predict": int(m.get("num_predict") or NUM_PREDICT),
            "thinking": bool(m.get("thinking", False)),
        }
    )

client = httpx.Client(
    timeout=httpx.Timeout(30.0, read=READ_TIMEOUT, write=30.0, pool=30.0)
)

# ─── Consulta a un superordenador ─────────────────────────────────────
SYNONIMOS_APROBADO = {
    "aprobado",
    "aprobe",
    "aprobebo",
    "si",
    "sí",
    "yes",
    "true",
    "approve",
    "approved",
    "procede",
    "afirmativo",
}
SYNONIMOS_DENEGADO = {
    "denegado",
    "negado",
    "no",
    "reject",
    "rejected",
    "false",
    "denied",
    "rejecto",
    "reprovado",
}


def _normaliza_veredicto(doc: dict) -> tuple[bool, int, str]:
    """Convierte la JSON del módulo en (aprobado: bool, confianza: int, razón: str)."""
    aprobado_raw = doc.get(
        "aprobado", doc.get("verdict", doc.get("veredicto", doc.get("decision")))
    )
    if isinstance(aprobado_raw, bool):
        aprobado = aprobado_raw
    elif isinstance(aprobado_raw, str):
        v = aprobado_raw.strip().lower().rstrip(".,;:!?")
        if v in SYNONIMOS_APROBADO:
            aprobado = True
        elif v in SYNONIMOS_DENEGADO:
            aprobado = False
        else:
            raise ValueError(f"veredicto no reconocido: {aprobado_raw!r}")
    elif aprobado_raw is None:
        raise ValueError("sin campo de veredicto")
    else:
        aprobado = bool(aprobado_raw)

    conf_raw = doc.get("confianza", doc.get("confidence", doc.get("certeza", 50)))
    try:
        confianza = max(0, min(100, int(float(conf_raw))))
    except (TypeError, ValueError):
        confianza = 50

    razon = str(
        doc.get(
            "razon", doc.get("rationale", doc.get("raison", doc.get("reasoning", "")))
        )
    ).strip()
    if not razon:
        razon = str(json.dumps(doc, ensure_ascii=False))[:400]
    return aprobado, confianza, razon


def _parse_json_robusto(texto: str) -> dict:
    """La barrera defensiva: JSON, luego extracción por regex."""
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("la respuesta no contiene JSON")


def consulta_magi(modulo: dict, pregunta: str, intento: int = 0) -> dict:
    """Un superordenador deliberando. Devuelve {aprobado, confianza, razon, modelo}."""
    system = f"""{modulo["persona"]}\n\n{modulo["encuadre"]}"""
    if intento > 0:
        system += "\n\nNOTA DE RITSUKO: tu dictamen anterior no fue JSON válido. Responde EXCLUSIVAMENTE con el objeto JSON pedido."
    user = f"EVALUACIÓN SOLICITADA:\n{pregunta}\n\nDicta tu veredicto (JSON exclusivo)."

    payload = {
        "model": modulo["modelo"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": modulo["temperatura"],
            "num_ctx": NUM_CTX,
            "num_predict": int(modulo.get("num_predict", NUM_PREDICT)),
        },
    }
    # Los modelos thinking (p.ej. línea Gemma-4) dispersan tokens en razón;
    # obsérvalo: contenido después del pensamiento NO brota si el presupuesto
    # se gasta en el pensamiento. Corte configurable por módulo.
    if modulo.get("thinking", False) is False:
        payload["think"] = False
    resp = client.post(
        f"{OLLAMA}/api/chat",
        json=payload,
        headers={"Connection": "close"},  # sin keep-alive: sin sockets zombis
        timeout=httpx.Timeout(30.0, read=READ_TIMEOUT, write=30.0, pool=30.0),
    )
    resp.raise_for_status()
    body = resp.json()
    mensaje = body.get("message", {})
    contenido = mensaje.get("content") or ""
    pensamiento = mensaje.get("thinking") or ""
    try:
        doc = _parse_json_robusto(contenido) if contenido else {}
        aprobado, confianza, razon = _normaliza_veredicto(doc)
    except (ValueError, json.JSONDecodeError):
        # Fallback: hay modelos que deportan el JSON dentro del thinking
        doc = (
            _parse_json_robusto(pensamiento)
            if pensamiento
            else _parse_json_robusto(contenido)
        )
        aprobado, confianza, razon = _normaliza_veredicto(doc)
    return {
        "aprobado": aprobado,
        "confianza": confianza,
        "razon": razon,
        "modelo": modulo["modelo"],
        "modulo": modulo["codename"],
    }


# ─── Votación: los patrones del MAGI ──────────────────────────────────
# Canon del sistema: 8 resultados posibles. Los dos unánimes son
# «la Sabiduría de Dios»; las seis combinaciones 2-1 son
# «las maquinaciones del hombre» (numeración de disidencia propia,
# con reverencia al Episodio 13).
PATTERNS = {
    (3, 0): (
        "PATRÓN 1/8",
        "CONSENSO UNÁNIME — APROBADO",
        "«La balanza se inclina sin disidencia. La Sabiduría de Dios dicta: se ejecuta.»",
        "APROBADO",
    ),
    (0, 3): (
        "PATRÓN 2/8",
        "DENEGACIÓN UNÁNIME",
        "«La Sabiduría de Dios veda la operación. NERV no la ejecutará.»",
        "DENEGADO",
    ),
    (2, 1): (
        "MAYORÍA CON DISIDENCIA",
        "APROBADO POR MAYORÍA",
        "«La mayoría activa autoriza. La disidencia queda registrada.»",
        "APROBADO",
    ),
    (1, 2): (
        "MAYORÍA CON DISIDENCIA",
        "DENEGADO POR MAYORÍA",
        "«La mayoría veta. La disidencia tiene voz, no mando.»",
        "DENEGADO",
    ),
}


def votacion(votos: dict[str, dict]) -> dict:
    """votos: {codename: {aprobado: bool|None, ...}}. bool None = módulo caído."""
    a = sum(1 for v in votos.values() if v["aprobado"] is True)
    d = sum(1 for v in votos.values() if v["aprobado"] is False)
    caidos = [k for k, v in votos.items() if v["aprobado"] is None]

    if not caidos and (a, d) in PATTERNS:
        patron, nombre, tagline, resultado = PATTERNS[(a, d)]
        return {
            "patron": patron,
            "nombre": nombre,
            "tagline": tagline,
            "resultado": resultado,
            "caidos": [],
            "subsistema": "COMPLETO",
        }

    # Degradación por módulo caído — canon de emergencia
    if len(caidos) == 1:
        if a == 2:
            texto, resultado = "SISTEMA DEGRADADO — MAYORÍA MÍNIMA", "APROBADO"
        elif d == 2:
            texto, resultado = "SISTEMA DEGRADADO — MAYORÍA MÍNIMA", "DENEGADO"
        elif a == 1 and d == 1:
            texto, resultado = (
                "SISTEMA DEGRADADO — SIN MAYORÍA (dilema)",
                "SIN RESOLUCIÓN",
            )
        else:
            texto, resultado = "SISTEMA DEGRADADO — INDETERMINADO", "SIN RESOLUCIÓN"
        patron = "PATRÓN [DEGRADADO]"
        tagline = "«Un módulo cayó en combate. El MAGI resuelve con dos.»"
        return {
            "patron": patron,
            "nombre": texto,
            "tagline": tagline,
            "resultado": resultado,
            "caidos": caidos,
            "subsistema": "DEGRADADO",
        }
    return {
        "patron": "FALLO CRÍTICO",
        "nombre": "MAGI OPERATIVAMENTE MUDO",
        "tagline": "«Sin quórum, no hay verdicto.»",
        "resultado": "SIN RESOLUCIÓN",
        "caidos": caidos,
        "subsistema": "CRÍTICO",
    }


# ─── Deliberación paralela ────────────────────────────────────────────
def deliberar(pregunta: str, plain: bool = False):
    """Ejecuta los tres módulos en paralelo y devuelve (registro, resolución)."""
    t0 = time.time()
    estado = {m["codename"]: {"status": "deliberando", "seg": 0.0} for m in MODULOS}
    lock = threading.Lock()
    console = Console()
    ronda = {"n": 1}

    def _panel(mkey: str):
        cod = mkey
        e = estado[cod]
        mod = next(m for m in MODULOS if m["codename"] == cod)
        color = mod["color"]
        if e["status"] == "deliberando":
            cuerpo = Text("⏳ delibera…", style=f"bold {color}")
            sub = f"{e['seg']:.0f}s"
        elif e["status"] == "ok":
            ver = "APROBADO" if e["aprobado"] else "DENEGADO"
            vcolor = "green" if e["aprobado"] else "red"
            cuerpo = Text.from_markup(
                f"[bold {vcolor}]⟦ {ver} ⟧[/]  confianza [bold]{e['confianza']}[/]%  ⏱ {e['seg']:.0f}s\n\n"
                f"[white]{e['razon']}[/white]"
            )
            sub = mod["modelo"]
        else:
            cuerpo = Text.from_markup(f"[bold red]⟦ MÓDULO CAÍDO: {e['error']} ⟧[/]")
            sub = mod["modelo"]
        return Panel(
            cuerpo,
            title=f"[bold {color}]{cod}[/] [dim]· {mod['forma']}[/]",
            subtitle=f"[dim]{sub[:48]}[/dim]",
            border_style=color,
            box=box.ROUNDED,
            width=52,
        )

    def _panel_resolucion(r: dict, t_total: float) -> Panel:
        res_color = {"APROBADO": "green", "DENEGADO": "red"}.get(
            r["resultado"], "yellow"
        )
        return Panel(
            Text.from_markup(
                f"[bold {res_color}]{r['patron']} · {r['nombre']} → {r['resultado']}[/]\n\n"
                f"[italic]{r['tagline']}[/italic]\n\n"
                f"[dim]subsistema: {r['subsistema']}"
                + (f" · caídos: {', '.join(r['caidos'])}" if r["caidos"] else "")
                + "[/dim]"
            ),
            title="[bold cyan]──────── RESOLUCIÓN DEL MAGI ────────[/]",
            border_style="cyan",
            box=box.DOUBLE,
        )

    def _render():
        piezas = [
            Panel(
                Text(pregunta, style="bold white"),
                title="[bold yellow]MAGI — OBJETO DE EVALUACIÓN[/]",
                border_style="yellow",
                box=box.DOUBLE,
            )
        ]
        piezas.append(Columns([_panel(m["codename"]) for m in MODULOS]))
        if all(estado[m["codename"]]["status"] != "deliberando" for m in MODULOS):
            r = votacion(
                {
                    cod: {
                        "aprobado": e.get("aprobado") if e["status"] == "ok" else None
                    }
                    for cod, e in estado.items()
                }
            )
            piezas.append(_panel_resolucion(r, time.time() - t0))
            piezas.append(
                Text.from_markup(
                    f"[dim]⏱ deliberación total: {time.time() - t0:.1f}s · salida: pause EX=0 aprobado / EX=1 denegado[/dim]"
                )
            )
        return Group(*piezas)

    # Live TUI o modo plano
    live_ctx = (
        Live(_render(), console=console, refresh_per_second=4) if not plain else None
    )
    if live_ctx:
        live_ctx.start()

    # Las tres mentes deliberan AL MISMO TIEMPO (paralelo, no secuencia)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for mod in MODULOS:
            futures[pool.submit(_tarea_megiddo, mod, pregunta)] = mod["codename"]
        for fut in as_completed(futures):
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
                except Exception as exc:  # noqa: BLE001 — los módulos pueden caer
                    msg = str(exc).split("For more information check:")[0].strip()
                    estado[cod].update(
                        status="error", error=msg[:160], seg=time.time() - t0
                    )
            if live_ctx:
                live_ctx.update(_render())
            elif plain:
                console.print(_panel(cod))

    if live_ctx:
        live_ctx.update(_render())
        live_ctx.stop()

    res = votacion(
        {
            cod: {"aprobado": e.get("aprobado") if e["status"] == "ok" else None}
            for cod, e in estado.items()
        }
    )
    if plain:
        console.print(_panel_resolucion(res, time.time() - t0))
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
    return registro, res


def _tarea_megiddo(mod: dict, pregunta: str) -> dict:
    """Reintentos defensivos: el MAGI no puede quedarse mudo."""
    ultimo_error = None
    for intento in range(2):
        try:
            return consulta_magi(mod, pregunta, intento=intento)
        except Exception as exc:
            ultimo_error = exc
            time.sleep(3)
    raise RuntimeError(f"{type(ultimo_error).__name__}: {str(ultimo_error)[:120]}")


# ─── Persistencia ─────────────────────────────────────────────────────
def guarda_log(registro: dict):
    archivo = LOG_DIR / f"magi_{time.strftime('%Y%m%d')}.jsonl"
    with open(archivo, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
    return archivo


# ─── Banner ───────────────────────────────────────────────────────────
BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║  M  A  G  I     S  Y  S  T  E  M     —     NERV  TOKYO-3         ║
║  MELCHIOR-1 (científica) · BALTHASAR-2 (madre) · CASPER-3 (mujer) ║
╚══════════════════════════════════════════════════════════════════╝
"""


def salud(console: Console, fatal: bool = True):
    console.print(Text(BANNER, style="bold cyan"))
    try:
        tags = {
            m["name"]
            for m in client.get(f"{OLLAMA}/api/tags", timeout=8)
            .json()
            .get("models", [])
        }
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]✗ MAGI-OLLAMA no responde:[/] {exc}")
        sys.exit(2)
    # Ollama sufre un apéndice ":latest" en los nombres sin etiqueta:
    # admite referencias con o sin sufijo (el falso ¡AUSENTE! v2.0).
    tags_norm = set(tags)
    for t in list(tags_norm):
        if t.endswith(":latest"):
            tags_norm.add(t[: -len(":latest")])

    def _esta(nombre: str) -> bool:
        return nombre in tags_norm

    faltan = [m for m in MODULOS if not _esta(m["modelo"])]
    console.print(f"[bold green]✓ magi-ollama[/] — {len(tags)} modelos en RAM/disco")
    for m in MODULOS:
        marca = "[green]✓ cargable[/]" if _esta(m["modelo"]) else "[red]✗ AUSENTE[/]"
        console.print(f"  {m['codename']:<12} {m['forma']:<12} {m['modelo']}  {marca}")
    if faltan:
        console.print("\n[yellow]Modelos ausentes. Comandos de descarga:[/]\n")
        for m in faltan:
            if m["key"] == "casper":
                console.print(
                    "  → CASPER se construye con el conversor:\n"
                    "     docker compose --profile tools run --rm converter\n"
                    "     docker exec magi-ollama ollama create casper-sese:Q4_K_M -f /models/casper.Modelfile"
                )
            else:
                console.print(f"  → docker exec magi-ollama ollama pull {m['modelo']}")
        if fatal:
            sys.exit(3)
        console.print(
            "\n[yellow]El MAGI operará igualmente; los módulos ausentes se "
            "declararán caídos en la votación (modo DEGRADADO).[/]\n"
        )


# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="magi", description="MAGI System — NERV Tokyo-3"
    )
    parser.add_argument("--plain", action="store_true", help="listado lineal, sin TUI")
    sub = parser.add_subparsers(dest="cmd")
    p_ask = sub.add_parser("ask", help="deliberación única")
    p_ask.add_argument("pregunta", nargs="+")
    sub.add_parser("doctor", help="diagnóstico de módulos y modelos")
    p_enc = sub.add_parser(
        "encargo",
        help="AGENCIA: los tres módulos diseñan planes, votan el mejor y lo EJECUTAN",
    )
    p_enc.add_argument("objetivo", nargs="+")
    p_mej = sub.add_parser(
        "mejorar", help="AGENCIA: el MAGI reescribe su propio código"
    )
    p_mej.add_argument("objetivo", nargs="*", help="(opcional) qué mejorar")
    args = parser.parse_args()

    if args.cmd == "doctor":
        salud(Console())
        return

    pregunta = " ".join(args.pregunta).strip() if args.cmd == "ask" else None
    console = Console()

    if args.cmd in ("encargo", "mejorar"):
        ag = CFG.get("agencia", {})
        if not ag.get("habilitada", True):
            console.print(
                "[bold red]AGENCIA deshabilitada en config.yaml (agencia.habilitada).[/]"
            )
            sys.exit(2)
        objetivo = " ".join(getattr(args, "objetivo", []) or []).strip()
        if not objetivo:
            objetivo = (
                "Analiza tu propio código y aplica UNA mejora pequeña, segura y "
                "verificable, sin degradar la votación ni las leyes."
            )
        from nerv_agencia import ejecutar_encargo

        sys.exit(ejecutar_encargo(console, objetivo))

    if pregunta:
        registro, _ = deliberar(pregunta, plain=args.plain)
        guarda_log(registro)
        sys.exit(0 if registro["resolucion"]["resultado"] == "APROBADO" else 1)

    # ── REPL estilo NERV ──
    salud(console, fatal=False)  # en REPL: avisa, pero no se apaga
    console.print(
        "[dim]Escribe tu propuesta ('encargo <tarea>' activa la AGENCIA: "
        "planes→voto→ejecución real con las Tres Leyes). 'salir' o Ctrl-D "
        "para escapar. Toda deliberación se registra en logs/.[/]\n"
    )
    n = 0
    while True:
        try:
            q = console.input("[bold yellow]PROMPT ▸ [/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print(
                "\n[cyan]╱ MAGI queda en modo latencia. __Reaction__. Saludos.[/]"
            )
            break
        if not q:
            continue
        if q.lower() in {"salir", "exit", "quit", "q"}:
            console.print("[cyan]╱ MAGI queda en modo latencia. Saludos.[/]")
            break
        mq1 = re.match(r"^(encargo|mejorar)\b\s*(.*)$", q, re.I)
        mq2 = re.match(r"^>\s*(.*)$", q)
        if mq1 or mq2:
            texto = (mq1.group(2) if mq1 else mq2.group(1)).strip()
            if not texto:
                texto = (
                    "Analiza tu propio código y aplica UNA mejora pequeña, "
                    "segura y verificable."
                )
            import nerv_agencia

            nerv_agencia.ejecutar_encargo(console, texto)
            console.print()
            continue
        n += 1
        console.clear()
        registro, res = deliberar(q, plain=args.plain)
        registro["ronda"] = n
        guarda_log(registro)
        console.print(
            f"[dim]registrado → logs/{registro['ts'][:10].replace('-', '')} · ROUND {n}[/]\n"
        )


if __name__ == "__main__":
    main()
