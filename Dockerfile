# One image, two entrypoints:
#   Cloud Run  : default CMD (gunicorn serving mailtransporter.server:app)
#   GCE watcher: python -m mailtransporter.watcher  (set via --container-command)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mailtransporter ./mailtransporter
RUN pip install . && useradd --create-home --uid 10001 app
USER app

CMD exec gunicorn --bind ":${PORT}" --workers 1 --threads 4 --timeout 0 --access-logfile - mailtransporter.server:app
