#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
#  Conversor CASPER: sese-snu/Llama3-Uncensored-bysese (BF16 HF)
#  → /out/casper-sese-f16.gguf → /out/casper-sese-Q4_K_M.gguf
#
#  Volumenes: /data (cache HF), /out (GGUF final)
# ─────────────────────────────────────────────────────────────────────
import subprocess
import sys
from pathlib import Path

REPO = "sese-snu/Llama3-Unsensored-bysese"
SRC = Path("/data/src")
OUT = Path("/out")
F16 = OUT / "casper-sese-f16.gguf"
Q4KM = OUT / "casper-sese-Q4_K_M.gguf"
LLAMA_CPP = Path("/llama.cpp")


def run(*cmd, check=True):
    print("▸", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(list(cmd), check=check)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if Q4KM.exists():
        print(f"✔ {Q4KM} ya existe — CASPER ya está convertido. Fin.")
        return

    # 1) Descarga del repo (16.1 GB en shards safetensors BF16)
    if not (SRC / "config.json").exists():
        run(
            sys.executable,
            "-m",
            "huggingface_hub.commands.huggingface_cli",
            "download",
            REPO,
            "--local-dir",
            str(SRC),
        )
    else:
        print(f"✔ {SRC} ya descargado")

    # 2) Safetensors HF → GGUF F16 (con chat_template.jinja → metadatos del GGUF)
    if not F16.exists():
        run(
            sys.executable,
            LLAMA_CPP / "convert_hf_to_gguf.py",
            str(SRC),
            "--outfile",
            str(F16),
            "--outtype",
            "f16",
        )

    # 3) F16 → Q4_K_M
    if not Q4KM.exists():
        run(
            LLAMA_CPP / "build" / "bin" / "llama-quantize",
            str(F16),
            str(Q4KM),
            "Q4_K_M",
        )

    print(f"✔ OK → {Q4KM}")
    print("Ahora registra el modelo en Ollama:")
    print(
        "  docker exec magi-ollama ollama create casper-sese:Q4_K_M -f /models/casper.Modelfile"
    )


if __name__ == "__main__":
    main()
