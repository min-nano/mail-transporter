# One image, two entrypoints:
#   Cloud Run  : default CMD (gunicorn serving mailtransporter.server:app)
#   GCE watcher: python -m mailtransporter.watcher  (set via --container-command)
#
# Reproducible: the base image is pinned by digest and every dependency is
# installed from the hash-locked requirements.txt (regenerate with
# `pip-compile --generate-hashes --strip-extras -o requirements.txt pyproject.toml`
# under Python 3.12). Dependabot keeps both current.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt ./
RUN pip install --require-hashes -r requirements.txt
COPY pyproject.toml README.md ./
COPY mailtransporter ./mailtransporter
RUN pip install --no-deps . && useradd --create-home --uid 10001 app
USER app

CMD exec gunicorn --bind ":${PORT}" --workers 1 --threads 4 --timeout 0 --access-logfile - mailtransporter.server:app
