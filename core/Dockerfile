FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY magi.py nerv_agencia.py .
COPY config.yaml* ./

ENV PYTHONUNBUFFERED=1
# `docker compose run --rm magi`          → TUI interactiva
# `docker compose run --rm magi ask "..."` → consulta única
ENTRYPOINT ["python", "magi.py"]
