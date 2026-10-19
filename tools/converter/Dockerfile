FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake build-essential python3 python3-pip python3-venv curl \
    && rm -rf /var/lib/apt/lists/*

# llama.cpp: fuente
RUN git clone --depth 1 https://github.com/ggerganov/llama.cpp /llama.cpp

# dependencias python del conversor (torch CPU, gguf, transformers…)
RUN pip3 install --no-cache-dir --break-system-packages -r /llama.cpp/requirements.txt \
    && pip3 install --no-cache-dir --break-system-packages "huggingface_hub[hf_transfer]" hf_transfer

# binario del cuantizador (cmake con -S explícito)
RUN cmake -S /llama.cpp -B /llama.cpp/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /llama.cpp/build --parallel "$(nproc)" --target llama-quantize

COPY convert_to_gguf.py /app/convert_to_gguf.py
WORKDIR /app

# Descarga → conversión F16 → cuantización Q4_K_M
ENTRYPOINT ["python3", "/app/convert_to_gguf.py"]
