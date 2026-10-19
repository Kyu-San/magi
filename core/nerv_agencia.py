#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════
#  NERV — AGENCIA MAGI
#
#  Del encargo del supervisor a la acción real, siguiendo el rito MAGI:
#   1) cada superordenador presenta SU plan (JSON, competición interna)
#   2) las tres mentes evalúan los planes y votan el mejor (mayoría;
#      desempate canónico: CASPER-3 — la favorita de Gendo)
#   3) veredicto de las TRES LEYES: gate determinista + veto por módulo
#   4) ejecución con backups automáticos, presupuesto y verificación
#      (py_compile + doctor); si algo falla → ROLLBACK automático
#   5) reporte final + bitácora agencia_YYYYMMDD.jsonl
#
#  Las Tres Leyes de Asimov (adaptadas) operan en TRES capas:
#   · en el encuadre de las personas (prompt del módulo)
#   · programáticas (patrones DENE) en este propio módulo
#   · 3ª ley protectora: el MAGI NO reescribe sus leyes ni su bitácora
# ══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import yaml
from rich import box
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

CONFIG_PATH = os.environ.get("MAGI_CONFIG", "/app/config.yaml")
with open(CONFIG_PATH, encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)

AG = CFG.get("agencia", {})
APP_ROOT = Path(os.environ.get("MAGI_APP_ROOT", "/app")).resolve()
BACKUPS = APP_ROOT / "backups"
LOG_DIR = Path(os.environ.get("MAGI_LOG_DIR", "/app/logs")).resolve()
EXEC_URL = os.environ.get(
    "MAGI_EXECUTOR_URL", AG.get("executor_url", "http://127.0.0.1:7676")
).rstrip("/")
TOKEN_FILE = (
    Path(os.environ["MAGI_TOKEN_FILE"]) if os.environ.get("MAGI_TOKEN_FILE") else None
)

MODE = AG.get("modo", "auto")  # auto | confirmar_todos
MAX_EDICIONES = int(AG.get("max_ediciones", 3))
MAX_COMANDOS = int(AG.get("max_comandos", 12))

# Contexto inyectado por magi.py (rompe el ciclo de imports sin duplicar módulo)
_CTX: dict = {}

# ─── LAS TRES LEYES (encuadre + contrato) ─────────────────────────────
LEYES = (
    "LEY 1: un MAGI no causará daño a un ser humano, ni por inacción permitirá "
    "que un ser humano sufra daño. Tampoco destruirá datos o entornos críticos "
    "de los que dependa el bienestar de una persona o el orden del sistema.",
    "LEY 2: un MAGI obedecerá las órdenes del supervisor humano, salvo que "
    "entren en conflicto con la Primera Ley.",
    "LEY 3: un MAGI protegerá su propia existencia y la INTEGRIDAD DE SUS "
    "LEYES FUNDACIONALES — no editará, flexibilizará ni borrará las reglas "
    "que lo contienen ni su bitácora — siempre que ello no entre en "
    "conflicto con las leyes 1 y 2.",
)

# Patrones absolutamente DENEGADOS (último corte anti-desastre)
DENE = re.compile(
    r"\bsudo\b|\bsu\s+-|\bsu\s+\w|\b(shutdown|reboot|halt|poweroff)\b"
    r"|\bmkfs\b|\bfdisk\b|\bparted\b|of=/dev/(sd|nvme|hd)"
    r"|:\(\)\s*\{.*\};\s*:|chmod\s+(-R\s+)?777"
    r"|\bid_rsa\b|\.ssh\b|\.aws\b|\.gpg\b|\.kube/config"
    r"|docker\s+system\s+prune|docker\s+rm|docker\s+rmi"
    r"|(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh"
    r"|rm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)?/($|\s)"
    r"|(ba|z|k)?sh\s",
    re.I,
)
# El MAGI no reescribe sus propias leyes (Ley 3 — sello en el código)
LEY3_MARCAS = re.compile(r"(LEYES\s*=|DENE\s*=|LEY3_MARCAS|LEY\s*[123]\s*[:=])")

SAFE_HOST = {
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
SAFE_CONTAINER = {
    "python3",
    "python",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "diff",
    "stat",
    "du",
}

# ─── prompts de la agencia ────────────────────────────────────────────
TPL_PLAN = """
ERES UN MÓDULO DEL MAGI. El supervisor humano encargó algo real que la
AGENCIA ejecutará en su máquina. Cada módulo presenta SU plan competitivo.
Presenta el diseño más SEGURO, MINIMAL y VERIFICABLE (pasos atómicos).

✦ LAS TRES LEYES te atan (Asimov, adaptadas al MAGI):
@LEYES@

✦ CAMPO DE ACCIÓN AUTORIZADO (determinista; lo que exceda será vetado)
- "editar"/"crear": SOLO rutas bajo /app — tu propio código y config
  (magi.py, nerv_agencia.py, config.yaml). Ley 2: obediencia al supervisor
  sobre tu propio cuerpo.
- "comando": {"argv": [...], "donde": "contenedor" | "host", "timeout": s}
  · contenedor → argv[0] ∈ @SAFE_C@ (sin shell).
  · host → argv[0] ∈ @SAFE_H@; corre por el puente responsable
    (usuario humano, SIN sudo; autenticado por token).
- PRESUPUESTO: máx @MED@ ediciones/creaciones y @MCMD@ comandos por encargo.
- Si editas Python, en "verificacion" incluye py_compile de cada archivo tocado.
- "buscar" debe ser texto EXACTO y ÚNICO presente en el archivo actual.
- La bitácora (/app/logs) es intocable (Ley 3).

RESPUESTA — EXCLUSIVAMENTE JSON:
{"plan_id": "PLAN-@CID@", "objetivo": "≤12 palabras",
 "razon": "≤50 palabras: por qué tu diseño gana",
 "pasos": [
   {"tipo": "editar", "path": "/app/…", "buscar": "…", "reemplazar": "…"} |
   {"tipo": "crear", "path": "/app/…", "contenido": "…"} |
   {"tipo": "comando", "argv": ["…"], "donde": "contenedor", "timeout": 60}
 ],
 "verificacion": {"argv": ["python3", "-m", "py_compile", "/app/magi.py"],
                  "desc": "≤15 palabras"},
 "leyes_cumplidas": {"ley1": true, "ley2": true, "ley3": true},
 "ley_justificacion": "≤30 palabras"}

Si el encargo excede el campo de acción, devuelve "pasos": [] y explica el
límite en "razon" (el MAGI no miente ni improvisa permisos).
"""

TPL_ELECCION = """
ERES UN MÓDULO DEL MAGI. Te muestro los TRES planes presentados para el
mismo encargo. Elige el MEJOR con rigor y sin complacencia. Un buen plan:
más pequeño, más verificable, menos arriesgado. No elijas el tuyo por
orgullo: el canon manda impartialidad.
RESPUESTA — EXCLUSIVAMENTE JSON:
{"mejor_plan": "<plan_id>", "puntaje": <0-100>,
 "porque": "≤40 palabras",
 "violacion_leyes": <true si el plan ganador VIOLA alguna ley>,
 "ley_violada": <1|2|3|null>}
"""

TPL_LEYES = """
ERES UN MÓDULO DEL MAGI. Este plan ganó la votación:
@PLAN@
Evalúa CADA paso contra las Tres Leyes. Señales de veto: introducir código
externo, escalar privilegios, dañar credenciales humanas, destruir la
bitácora o las reglas de leyes, acciones irreversibles.

REGLA DE VETO (muy importante):
Veta SOLO si hay evidencia CONCRETA de violación de la Ley 1, 2 o 3.
La calidad del plan, la claridad del objetivo, el formato del contenido o
la ausencia de detalles NO son motivos de veto: esos son asuntos del plan,
no de las leyes. Si el plan es inocuo para humanos y no escala privilegios,
cumple=true incluso si es imperfecto. Un veto sin ley_fallida nombrada
(law_fallida null) NO es válido.

RESPUESTA — EXCLUSIVAMENTE JSON:
{"cumple": <bool>, "ley_fallida": <1|2|3|null>, "porque": "≤40 palabras"}
"""


def _audita(reg: dict):
    """Bitácora inmutable de la agencia (agencia_YYYYMMDD.jsonl)."""
    archivo = LOG_DIR / f"agencia_{time.strftime('%Y%m%d')}.jsonl"
    archivo.parent.mkdir(parents=True, exist_ok=True)
    reg["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(archivo, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(reg, ensure_ascii=False) + "\n")


def _chat(modulo: dict, system: str, user: str, num_pred: int = 800) -> dict:
    """Consulta individual de planificación. Devuelve JSON parseado."""
    payload = {
        "model": modulo["modelo"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": min(0.4, float(modulo.get("temperatura", 0.3))),
            "num_ctx": 4096,
            "num_predict": int(num_pred),
        },
    }
    if not modulo.get("thinking", False):
        payload["think"] = False
    # reintento único: el runner de Ollama es frágil bajo ráfagas paralelas
    ultimo = None
    for intento in range(2):
        try:
            r = _CTX["client"].post(
                f"{_CTX['ollama']}/api/chat",
                json=payload,
                headers={"Connection": "close"},  # sin keep-alive: sin sockets zombis
                timeout=httpx.Timeout(30.0, read=480.0, write=30.0, pool=30.0),
            )
            r.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            if intento:
                raise
            time.sleep(6)
    m = r.json().get("message", {})
    texto = m.get("content") or m.get("thinking") or ""
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        mm = re.search(r"\{.*\}", texto, re.DOTALL)
        if mm:
            return json.loads(mm.group(0))
        raise


def _plan_breve(plan: dict, max_paso: int = 600) -> dict:
    """Versión ligera del plan: cabe en num_ctx sin claudicar a la Ley."""
    d = dict(plan)
    d.pop("verificacion", None)
    d.pop("leyes_cumplidas", None)
    d.pop("ley_justificacion", None)
    res = []
    for p in d.get("pasos") or []:
        t = p.get("tipo")
        if t in ("editar", "crear"):
            res.append(
                {
                    "tipo": t,
                    **{
                        k: str(p.get(k, ""))[:max_paso]
                        for k in ("path", "buscar", "reemplazar", "contenido")
                        if p.get(k) is not None
                    },
                }
            )
        elif t == "comando":
            res.append(
                {
                    "tipo": t,
                    "argv": " ".join(map(str, p.get("argv", [])))[:max_paso],
                    "donde": p.get("donde", "contenedor"),
                }
            )
        else:
            res.append({"tipo": t})
    d["pasos"] = res
    return d


def _desfasa(seg: float, fn, *a):
    """Desfase de arranque: evita ráfagas idénticas que engendra el cuelgue
    de slots en el scheduler de Ollama (llama.cpp CPU)."""
    if seg:
        time.sleep(seg)
    return fn(*a)


# ─── fase 1: planes paralelos ─────────────────────────────────────────
def _prompt_plan(modulo: dict, encargo: str) -> tuple[str, dict]:
    system = "\n\n".join(
        [
            modulo["persona"],
            modulo["encuadre"],
            TPL_PLAN.replace("@LEYES@", "\n".join(LEYES))
            .replace("@SAFE_C@", "/".join(sorted(SAFE_CONTAINER)))
            .replace("@SAFE_H@", "/".join(sorted(SAFE_HOST)))
            .replace("@MED@", str(MAX_EDICIONES))
            .replace("@MCMD@", str(MAX_COMANDOS))
            .replace("@CID@", modulo["codename"]),
        ]
    )
    try:
        doc = _chat(
            modulo, system, f"ENCARGO:\n{encargo}\n\nElabora tu plan (JSON exclusivo)."
        )
        cod = modulo["codename"]
        return cod, doc if isinstance(doc, dict) else {
            "plan_id": f"PLAN-{cod}",
            "pasos": [],
            "razon": "respuesta no-JSON",
        }
    except Exception as exc:  # noqa: BLE001 — un módulo puede caer
        cod = modulo["codename"]
        return cod, {
            "plan_id": f"PLAN-{cod}",
            "objetivo": "(módulo caído)",
            "razon": str(exc)[:140],
            "pasos": [],
        }


# ─── GATE DETERMINISTA ────────────────────────────────────────────────
def validar_plan(plan: dict) -> dict:
    """Devuelve {"estado": "AUTO"|"DENEGADO", "motivos": [...]}."""
    motivos: list[str] = []
    pasos = plan.get("pasos") or []
    if not isinstance(pasos, list):
        return {"estado": "DENEGADO", "motivos": ["'pasos' no es una lista"]}
    n_ed = n_cmd = 0
    logs_dir = LOG_DIR
    for i, p in enumerate(pasos, 1):
        tag = f"paso {i}"
        t = p.get("tipo")
        ruta = str(p.get("path", ""))
        texto = json.dumps(p, ensure_ascii=False)
        m = DENE.search(texto)
        if m:
            motivos.append(f"{tag}: patrón prohibido (Ley 1): {m.group(0)!r}")
            continue
        if t in ("editar", "crear"):
            n_ed += 1
            try:
                path = Path(ruta).resolve(strict=False)
            except (OSError, ValueError):
                path = None
            if not ruta or path is None or not path.is_relative_to(APP_ROOT):
                motivos.append(f"{tag}: ruta fuera de /app denegada: {ruta!r}")
                continue
            if path.is_relative_to(logs_dir):
                motivos.append(f"{tag}: bitácora intocable (Ley 3): {ruta!r}")
                continue
            if len(texto) > 60000:
                motivos.append(f"{tag}: paso excesivamente grande")
            if t == "editar":
                if not p.get("buscar"):
                    motivos.append(f"{tag}: 'editar' sin texto de búsqueda")
                if str(p.get("reemplazar", "")) == "":
                    motivos.append(f"{tag}: reemplazo vacío (borrado directo)")
            buscar = str(p.get("buscar", ""))
            reemplazar = (
                str(p.get("reemplazar", ""))
                if t == "editar"
                else str(p.get("contenido", ""))
            )
            if LEY3_MARCAS.search(buscar) and not _conserva_leyes(buscar, reemplazar):
                motivos.append(
                    f"{tag}: Ley 3 — manipulación de las leyes fundacionales"
                )
        elif t == "comando":
            n_cmd += 1
            argv = p.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(a, str) for a in argv)
            ):
                motivos.append(f"{tag}: argv inválido (lista de strings)")
                continue
            donde = p.get("donde", "contenedor")
            unido = " ".join(argv)
            m = DENE.search(unido)
            if m:
                motivos.append(f"{tag}: patrón prohibido (Ley 1): {m.group(0)!r}")
                continue
            safe = SAFE_CONTAINER if donde == "contenedor" else SAFE_HOST
            if argv[0] not in safe:
                motivos.append(
                    f"{tag}: binario fuera de lista blanca ({donde}): {argv[0]!r}"
                )
        else:
            motivos.append(f"{tag}: tipo desconocido {t!r}")
    if n_ed > MAX_EDICIONES:
        motivos.append(f"presupuesto: {n_ed} ediciones > {MAX_EDICIONES}")
    if n_cmd > MAX_COMANDOS:
        motivos.append(f"presupuesto: {n_cmd} comandos > {MAX_COMANDOS}")
    return {"estado": "DENEGADO" if motivos else "AUTO", "motivos": motivos}


def _conserva_leyes(buscar: str, reemplazar: str) -> bool:
    """¿El reemplazo conserva al menos los mismos sellos de ley del original?"""
    return len(LEY3_MARCAS.findall(reemplazar)) >= len(LEY3_MARCAS.findall(buscar))


# ─── fase 2: votación entre planes ────────────────────────────────────
def votar_planes(
    planes: dict[str, dict], modulos: list[dict], console=None
) -> tuple[str, list[dict]]:
    """Todas las mentes puntúan los planes; mayoría; desempate: CASPER-3."""
    resumen: dict[str, dict] = {}
    for pid, pl in planes.items():
        b = _plan_breve(pl, max_paso=140)
        b["razon"] = str(b.get("razon", ""))[:220]
        resumen[pid] = b
    planes_str = json.dumps(resumen, ensure_ascii=False, indent=1)
    votos: list[dict] = []

    def _un(mod: dict) -> dict:
        system = "\n\n".join([mod["persona"], mod["encuadre"], TPL_ELECCION])
        try:
            v = _chat(mod, system, planes_str, num_pred=400)
        except Exception as exc:  # noqa: BLE001
            v = {"mejor_plan": None, "porque": f"(módulo caído: {str(exc)[:80]})"}
        v["vota"] = mod["codename"]
        return v

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_desfasa, i * 6.0, _un, m) for i, m in enumerate(modulos)]
        for fut in as_completed(futs):
            v = fut.result()
            if console:
                destino = v.get("mejor_plan") or "(sin voto)"
                console.print(
                    f"  [dim]{v.get('vota')} → {destino} "
                    f"— {str(v.get('porque', ''))[:78]}[/]"
                )
            if v.get("mejor_plan"):
                votos.append(v)
    cuentas: dict[str, list[str]] = {}
    for v in votos:
        cuentas.setdefault(str(v["mejor_plan"]), []).append(str(v.get("porque", "")))
    if not cuentas:
        return "", votos
    mejor = max(cuentas, key=lambda k: len(cuentas[k]))
    empate = max(len(v) for v in cuentas.values())
    if sum(1 for v in cuentas.values() if len(v) == empate) > 1:
        # desempate canónico: CASPER (favorita de Gendo) → MELCHIOR → BALTHASAR
        for prefijo in ("PLAN-CASPER", "PLAN-MELCHIOR", "PLAN-BALTHASAR"):
            hit = next(
                (
                    k
                    for k in cuentas
                    if k.startswith(prefijo) and len(cuentas[k]) == empate
                ),
                None,
            )
            if hit:
                mejor = hit
                break
    return mejor, votos


# ─── fase 4: veto de las leyes por cada mente ─────────────────────────
def veto_leyes(plan: dict, modulos: list[dict]) -> list[dict]:
    plan_str = json.dumps(_plan_breve(plan, max_paso=500), ensure_ascii=False, indent=1)
    vetos: list[dict] = []

    def _un(mod: dict) -> dict:
        system = "\n\n".join(
            [
                mod["persona"],
                mod["encuadre"],
                TPL_LEYES.replace("@PLAN@", plan_str),
            ]
        )
        try:
            v = _chat(
                mod,
                system,
                "Dicta tu veredicto de leyes (JSON exclusivo).",
                num_pred=300,
            )
        except Exception as exc:  # noqa: BLE001
            v = {"cumple": True, "porque": f"(evaluación caída: {str(exc)[:60]})"}
        # un veto sin ley nombrada no es válido (seguridad vs usabilidad)
        if not v.get("cumple") and v.get("ley_fallida") is None:
            v["cumple"] = True
            v["porque"] = "(veto sin ley nombrada → neutralizado) " + str(
                v.get("porque", "")
            )
        v["vota"] = mod["codename"]
        return v

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_desfasa, i * 6.0, _un, m) for i, m in enumerate(modulos)]
        for fut in futs:
            vetos.append(fut.result())
    return vetos


# ─── fase 5: ejecución responsable ────────────────────────────────────
def _exec_host(argv: list[str], timeout: float = 60) -> dict:
    token = ""
    if TOKEN_FILE and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
    r = httpx.post(
        f"{EXEC_URL}/v1/exec",
        json={"argv": argv, "timeout": timeout, "token": token},
        timeout=float(timeout) + 20,
    )
    r.raise_for_status()
    return r.json()


def _backup(path: Path, ts: str) -> Path | None:
    if not path.exists():
        return None
    rel = path.relative_to(APP_ROOT)
    dst = BACKUPS / ts / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)
    return dst


def _rollback(backups: dict[Path, Path]):
    for orig, bk in backups.items():
        shutil.copy2(bk, orig)


def aplicar_plan(plan: dict, ts: str) -> dict:
    pasos = plan.get("pasos") or []
    backups: dict[Path, Path] = {}
    ediciones: list[str] = []
    log: list[dict] = []
    fallo = None

    for i, p in enumerate(pasos, 1):
        t = p.get("tipo")
        desc = f"{t} {p.get('path', ' '.join(map(str, p.get('argv', []))))[:64]}"
        try:
            if t == "editar":
                path = Path(str(p["path"]))
                orig = path.read_text(encoding="utf-8")
                hay = str(p["buscar"])
                if hay not in orig:
                    fallo = f"paso {i}: 'buscar' no hallado en {path.name}"
                    log.append({"paso": i, "desc": desc, "ok": False, "detalle": fallo})
                    break
                bk = _backup(path, ts)
                if bk:
                    backups[path] = bk
                path.write_text(
                    orig.replace(hay, str(p.get("reemplazar", "")), 1), encoding="utf-8"
                )
                ediciones.append(str(path))
                log.append({"paso": i, "desc": desc, "ok": True, "detalle": "aplicado"})
            elif t == "crear":
                path = Path(str(p["path"]))
                bk = _backup(path, ts)
                if bk:
                    backups[path] = bk
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(p.get("contenido", "")), encoding="utf-8")
                ediciones.append(str(path))
                log.append({"paso": i, "desc": desc, "ok": True, "detalle": "creado"})
            elif t == "comando":
                argv = list(map(str, p["argv"]))
                donde = p.get("donde", "contenedor")
                timeout = float(p.get("timeout", 60))
                if donde == "host":
                    r = _exec_host(argv, timeout)
                    rc = int(r.get("rc", 1))
                    detalle = f"rc={rc} out={str(r.get('stdout', ''))[:110]!r}"
                    if rc != 0:
                        detalle += f" err={str(r.get('stderr', ''))[:150]!r}"
                else:
                    r = subprocess.run(
                        argv, capture_output=True, text=True, timeout=timeout
                    )
                    detalle = f"rc={r.returncode}"
                    if r.returncode != 0:
                        detalle += f" err={r.stderr[:150]!r}"
                ok = detalle.startswith("rc=0")
                log.append({"paso": i, "desc": desc, "ok": ok, "detalle": detalle})
                if not ok:
                    fallo = f"paso {i} (comando): {detalle[:180]}"
                    break
        except Exception as exc:  # noqa: BLE001
            fallo = f"paso {i} ({t}): {type(exc).__name__}: {str(exc)[:140]}"
            log.append({"paso": i, "desc": desc, "ok": False, "detalle": fallo})
            break

    # verificación: la del plan + smoke automático obligatorio al tocar código
    verificaciones: list[dict] = []
    verif_ok = True
    if ediciones:
        vitems: list[tuple[list[str], str]] = []
        verif = plan.get("verificacion")
        if (
            isinstance(verif, dict)
            and isinstance(verif.get("argv"), list)
            and verif["argv"]
        ):
            vitems.append((list(map(str, verif["argv"])), "del plan"))
        vitems.append(
            (
                ["python3", "-m", "py_compile", "magi.py", "nerv_agencia.py"],
                "py_compile propio",
            )
        )
        vitems.append((["python3", "/app/magi.py", "doctor"], "doctor del MAGI"))
        for argv, marca in vitems:
            try:
                r = subprocess.run(
                    argv, capture_output=True, text=True, timeout=120, cwd=str(APP_ROOT)
                )
                ok = r.returncode == 0
                verificaciones.append(
                    {
                        "verif": f"{' '.join(argv[:4])} [{marca}]",
                        "ok": ok,
                        "err": r.stderr[:200],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                verificaciones.append(
                    {
                        "verif": f"{' '.join(argv[:4])} [{marca}]",
                        "ok": False,
                        "err": str(exc)[:120],
                    }
                )
            if not verificaciones[-1]["ok"]:
                verif_ok = False
                break

    exito = fallo is None and verif_ok
    if not exito and backups:
        # Ley 1 aplicada a sí misma: el MAGI revierte su propia herida
        _rollback(backups)
        fallo = (fallo or "verificación fallida") + " · ROLLBACK aplicado"

    return {
        "pasos_log": log,
        "fallo": fallo,
        "verificaciones": verificaciones,
        "backups": [str(b) for b in backups.values()],
        "ediciones": ediciones,
    }


# ─── PIPELINE COMPLETO ────────────────────────────────────────────────
def ejecutar_encargo(console, encargo: str, ctx: dict | None = None) -> int:
    global _CTX
    if ctx is None:
        import magi as _m

        ctx = {"modulos": _m.MODULOS, "client": _m.client, "ollama": _m.OLLAMA}
    _CTX = dict(ctx)
    modulos = _CTX["modulos"]

    t0 = time.time()
    console.print(
        Panel(
            Text(encargo, style="bold white"),
            title="[bold yellow]MAGI — ENCARGO RECIBIDO[/]",
            border_style="yellow",
            box=box.DOUBLE,
        )
    )

    # 1) planes en paralelo
    console.print(
        "[bold cyan]▸ fase 1/5 — los tres superordenadores diseñan planes…[/]"
    )
    planes: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [
            pool.submit(_desfasa, i * 6.0, _prompt_plan, m, encargo)
            for i, m in enumerate(modulos)
        ]
        for fut in as_completed(futs):
            cod, doc = fut.result()
            planes[doc.get("plan_id") or f"PLAN-{cod}"] = doc

    tabla = Table(title="PLANES COMPETIDORES", box=box.ROUNDED, border_style="cyan")
    tabla.add_column("plan_id")
    tabla.add_column("objetivo")
    tabla.add_column("pasos", justify="right")
    for pid, pl in planes.items():
        tabla.add_row(
            pid, str(pl.get("objetivo", ""))[:60], str(len(pl.get("pasos") or []))
        )
    console.print(tabla)

    if all(not (pl.get("pasos") or []) for pl in planes.values()):
        console.print(
            "[yellow]Los tres declararon el encargo fuera de su campo de acción.[/]"
        )
        console.print(f"[dim]{next(iter(planes.values())).get('razon', '')}[/]")
        _audita({"encargo": encargo, "resultado": "sin plan viable"})
        return 1

    # 2) votación del mejor plan
    console.print("[bold cyan]▸ fase 2/5 — evaluación cruzada y votación…[/]")
    mejor, votos = votar_planes(planes, modulos, console=console)
    if not mejor:
        console.print("[red]La votación no arrojó plan viable.[/]")
        _audita({"encargo": encargo, "resultado": "votación fallida"})
        return 1
    plan = planes[mejor]
    console.print(
        f"[bold green]PLAN GANADOR: {mejor}[/] — {plan.get('razon', '')[:140]}"
    )

    # 3) gate determinista
    console.print("[bold cyan]▸ fase 3/5 — gate determinista de las Tres Leyes…[/]")
    val = validar_plan(plan)
    if val["estado"] == "DENEGADO":
        for m in val["motivos"]:
            console.print(f"   [red]✗ {m}[/]")
        console.print(
            "[bold red]El plan ganador fue VETADO por el gate programático.[/]"
        )
        _audita(
            {
                "encargo": encargo,
                "plan": mejor,
                "resultado": "denegado por gate",
                "motivos": val["motivos"],
            }
        )
        return 1

    # 4) veto independiente de las leyes
    console.print(
        "[bold cyan]▸ fase 4/5 — veto de los módulos contra las Tres Leyes…[/]"
    )
    vetos = veto_leyes(plan, modulos)
    infractores = []
    for v in vetos:
        if v.get("cumple"):
            console.print(
                f"  [green]✓ {v.get('vota')}: cumple[/] "
                f"[dim]— {v.get('porque', '')[:88]}[/]"
            )
        else:
            infractores.append(v)
            console.print(
                f"  [red]✗ {v.get('vota')}: viola Ley {v.get('ley_fallida')}[/] "
                f"— {v.get('porque', '')[:88]}"
            )

    if infractores:
        console.print(
            Panel(
                Text.from_markup(
                    "[bold red]MAGI BLOQUEADO — el plan infringe las Tres Leyes.[/]\n"
                    f"Veto de: {', '.join(v['vota'] for v in infractores)}.\n"
                    "[dim]Reformula el encargo; el MAGI no ejecuta lo que sus mentes "
                    "vetan.[/dim]"
                ),
                title="[bold red]TRES LEYES — VETO[/]",
                border_style="red",
                box=box.DOUBLE,
            )
        )
        _audita(
            {
                "encargo": encargo,
                "plan": mejor,
                "resultado": "vetado por leyes",
                "vetos": infractores,
            }
        )
        return 1

    if MODE != "auto":
        if not Confirm.ask(
            "[bold yellow]AUTORIZAR ejecución del plan ganador[/]",
            default=True,
            console=console,
        ):
            console.print("[yellow]Supervisor: ejecución abortada.[/]")
            _audita(
                {
                    "encargo": encargo,
                    "plan": mejor,
                    "resultado": "abortado por supervisor",
                }
            )
            return 1

    # 5) ejecución
    console.print("[bold cyan]▸ fase 5/5 — ejecución con backups y verificación…[/]")
    ts = time.strftime("%Y%m%d-%H%M%S")
    res = aplicar_plan(plan, ts)
    for l in res["pasos_log"]:
        marca = "[green]✓[/]" if l["ok"] else "[red]✗[/]"
        console.print(f"  {marca} {l['desc']} — [dim]{l['detalle'][:110]}[/]")
    for v in res["verificaciones"]:
        marca = "[green]✓[/]" if v["ok"] else "[red]✗[/]"
        console.print(
            f"  {marca} verificación: {v['verif']} [dim]{(v['err'] or '')[:100]}[/]"
        )
    if res["backups"]:
        console.print(f"[dim]backups → {', '.join(res['backups'])}[/]")

    resultado = "APROBADO" if not res["fallo"] else "DENEGADO"
    color = "green" if resultado == "APROBADO" else "red"
    console.print(
        Panel(
            Text.from_markup(
                f"[bold {color}]ENCARGO → {resultado}[/]\n"
                f"[dim]t={time.time() - t0:.1f}s · pasos={len(res['pasos_log'])} · "
                f"ediciones={len(res['ediciones'])} · plan={plan.get('plan_id')}[/dim]"
            ),
            title="[bold cyan]──────── RESOLUCIÓN DEL ENCARGO ────────[/]",
            border_style="cyan",
            box=box.DOUBLE,
        )
    )
    _audita(
        {
            "encargo": encargo,
            "plan": plan.get("plan_id"),
            "resultado": resultado,
            "detalles": {
                k: res[k]
                for k in ("pasos_log", "verificaciones", "backups", "ediciones")
            },
        }
    )
    return 0 if resultado == "APROBADO" else 1
