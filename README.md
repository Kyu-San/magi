# MAGI SYSTEM — NERV Tokyo-3 (edición dockerizada)

Implementación local del **Sistema MAGI** de *Neon Genesis Evangelion*:
tres superordenadores independientes (**MELCHIOR-1**, **BALTHASAR-2**, **CASPER-3**)
que deliberan **en paralelo** sobre cualquier propuesta y emiten su veredicto
por mayoría, siguiendo los patrones de votación del canon.

Todo corre en contenedores Docker en tu propia máquina: inference engine,
núcleo de orquestación, TUI estilo NERV y hasta el conversor de modelos.

## Arquitectura

```
┌────────────────────────── docker network: magi-net ──────────────────────┐
│                                                                          │
│  ┌─────────────────────┐        ┌──────────────────────────────┐          │
│  │  magi-ollama        │        │  magi-core (TUI/orquestador) │          │
│  │  (ollama/ollama)    │◄───────┤  MELCHIOR · BALTHASAR ·      │          │
│  │  3 modelos en RAM   │  API   │  CASPER deliberan paralelo   │          │
│  └─────────────────────┘        └──────────────────────────────┘          │
│      ▲                         ┌──────────────────────────────┐          │
│  ┌───┴─────────────────┐       │  magi-converter (one-shot)   │          │
│  │  ./models (bind)    │       │  safetensors→GGUF (CASPER)   │          │
│  │  GGUF + Modelfile   │       └──────────────────────────────┘          │
│  └─────────────────────┘                                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

## Los tres superordenadores

| Módulo | Forma | Modelo | Origen |
|---|---|---|---|
| **MELCHIOR-1** | la científica | `m2hai-unsensored-8b` (Dolphin·Llama3) | `hf.co/m2hgamerz/m2hai-unsensored-8b` |
| **BALTHASAR-2** | la madre | `gemma4_unsensored Q4_K_M` (Gemma·4 E4B) | `hf.co/meletis24/gemma4_unsensored` |
| **CASPER-3** | la mujer | `casper-sese Q4_K_M` (Llama3·sese) | `sese-snu/Llama3-Uncensored-bysese` (convertido a mano) |

Cada módulo tiene **su propio modelo, temperatura y persona** (configurable en
`core/config.yaml` — archivo montado en el contenedor; cambiar personas no
requiere rebuild). Trilogía de pesos comunitarios elegidos por el supervisor.

## Votación: los 8 patrones

Canon del sistema: ocho desenlaces posibles. Los dos unánimes son
«la Sabiduría de Dios»; las seis combinaciones 2-1 son «las maquinaciones
de los hombres»:

- **PATRÓN 1** — 3×APROBADO → consenso unánime, se ejecuta
- **PATRÓN 2** — 3×DENEGADO → denegación unánime
- 2×APROBADO + 1 disidencia → **aprobado por mayoría** (quien disientió queda anotado)
- 1×APROBADO + 2×DENEGADO → **denegado por mayoría**
- Módulo caído → modo **SISTEMA DEGRADADO**: el MAGI resuelve con dos voces

Benchmarks medidos en tu hardware (Threadripper 1900X, CPU puro):
MELCHIOR ~6.1 tok/s · BALTHASAR ~8.6 tok/s · deliberación tri-prellela: los
tres módulos razonan al mismo tiempo, una ronda ≈ 30-90 s.

## Operar el MAGI (todo dockerizado)

```bash
cd ~/magi

# 1) estado de los módulos y modelos
sudo docker compose -f docker-compose.yml run --rm magi doctor

# 2) TUI interactiva estilo NERV (REPL)
sudo docker compose -f docker-compose.yml run --rm magi

# 2b) consultar algo puntual (salida no interactiva)
sudo docker compose -f docker-compose.yml run --rm magi ask "¿Deployar viernes a las 18:00?"

# 2c) salida plana para scripts (exit code: 0 APROBADO, 1 DENEGADO)
sudo docker compose -f docker-compose.yml run --rm magi --plain ask "¿Ralíz?"
echo $?   # ← el MAGI decide

# 3) AGENCIA — encargo real: planes → voto → EJECUCIÓN con las Tres Leyes
sudo docker compose -f docker-compose.yml run --rm magi encargo "haz X, Y, Z en tu código/config"

# 3b) auto-mejora: el MAGI reescribe su propio código con backup+rollback
sudo docker compose -f docker-compose.yml run --rm magi mejorar
#   (en la TUI: 'encargo <tarea>' o 'mejorar' hacen lo mismo)

# ── CASPER: conversión completa (solo la primera vez) ──
sudo docker compose -f docker-compose.yml --profile tools build converter
sudo docker compose -f docker-compose.yml --profile tools run --rm converter   # ~16 GB descarga
sudo docker exec magi-ollama ollama create casper-sese:Q4_K_M -f /models/casper.Modelfile

# ── Recambiar un módulo por otro modelo (sin rebuild) ──
# edita core/config.yaml → modelo: "hf.co/…" y haz:
sudo docker exec magi-ollama ollama pull hf.co/<nuevo-modelo>:Q4_K_M
```

## Agencia — del oráculo a las manos (de la sesión v2.0)

`magi encargo "…"` convierte la deliberación en **acción real**:

1. **Planes** — los tres módulos proponen SU plan (JSON) en paralelo.
2. **Votación del mejor plan** — las tres mentes evalúan los tres planes y
   eligen (mayoría; desempate canónico: CASPER-3, favorita de Gendo).
3. **Tres Leyes** — gate determinista (regex de lo prohibido) + veto
   independiente de cada módulo sobre el plan ganador. Un solo veto → abort.
4. **Ejecución** — la AGENCIA actúa de verdad:
   - `editar`/`crear`: SOLO `/app` (= `~/magi/core`; es su cuerpo/código),
     con **backup automático** en `core/backups/`
     y **verificación** (`py_compile` + `magi doctor`);
     si la verificación falla → **ROLLBACK automático**.
   - `comando` "contenedor": lista blanca estricta (python3, ls, grep…).
   - `comando` "host": pasa por el **puente responsable**
     (`tools/magi_executor.py`, systemctl --user `magi-exec.service`,
     SOLO 127.0.0.1, sin sudo jamás;
     token en `~/magi/.magi_token` (0600; el puente lo genera a la primera),
     lista blanca: git, docker, cat, grep, curl, jq, cp, mv…).
5. **Reporte** — resolución APROBADO/DENEGADO + bitácora `logs/agencia_*.jsonl`.

Las **Tres Leyes de Asimov** (adaptadas al MAGI) operan en tres capas:
en el encuadre de las personas, programáticamente (`nerv_agencia.py`)
y la Ley 3 es sello: el MAGI **no puede reescribir sus propias leyes ni
su bitácora** (`logs/` queda fuera de su campo de acción determinista).

Presupuestos por encargo en `config.yaml → agencia:`
(`modo: auto | confirmar_todos`, `max_ediciones: 3`, `max_comandos: 12`).

## Estructura

```
magi/
├── docker-compose.yml          # ollama + magi-core (red del host) + converter
├── core/
│   ├── Dockerfile
│   ├── magi.py                 # orquestador + votación de patrones + TUI
│   ├── nerv_agencia.py         # AGENCIA: planes→voto→Tres Leyes→ejecución
│   └── config.yaml             # ✓ editable en caliente (personas, modelos, agencia)
├── models/
│   └── casper.Modelfile        # registro del GGUF convertido en Ollama
├── tools/
│   ├── converter/              # llama.cpp: safetensors → GGUF Q4_K_M
│   ├── magi_executor.py        # puente responsable host↔MAGI (systemd --user)
│   └── magi-exec.service       # unidad systemd del puente
└── logs/                       # bitácora JSONL (deliberaciones + agencia + exec)
```

## Bitácora

Cada deliberación se registra en `logs/magi_YYYYMMDD.jsonl`: pregunta,
los tres dictámenes con razones y confianzas, patrón de votación,
tiempos, estado del subsistema. Tu entrada de la historia sin recodificación.

## Notas de lore (con honestidad)

- La numeración de disidencias 3-8 es propia; el canon (Ep. 13) solo
  confirma que existen 8 patrones y que los dos unánimes son «la
  Sabiduría de Dios» (frase atribuida a Ritsuko Akagi).
- Gendo siempre tomaba la opinión de CASPER-3. Aquí no hay favorito:
  la diplomacia de tres mentes es tu asunto.
- Los modelos uncensored responden sin coronas morales. Tú eres el
  supervisor de uso; el MAGI solo decide.

## Troubleshooting operado en carne propia

**GGUF truncado al registrar un modelo (`ollama create`):**
síntoma: `unsupported tensor "blk.NN…" offset+size exceeds file size`
al evaluar un modelo con FROM local. Causa: `ollama create` demasiado
ambicioso cortó la copia del GGUF (>4 GB) en el almacenamiento interno.
Diagnóstico: `docker exec magi-ollama stat -c '%s' <blob>` vs el tamaño
real del archivo. El nombre del blob = sha256 del archivo fuente
(verifícalo con `sha256sum`). Reparación: sobrescribe directamente el
blob con el archivo fuente completo:

```bash
sudo cp -f models/out/<modelo>.gguf \
  /var/lib/docker/volumes/magi_ollama-data/_data/models/blobs/sha256-<hash>
```

Ollama es content-addressable: si el archivo en blobs es completo, 
`ollama run/find` apenas funciona; el manifest no se retoCA.

