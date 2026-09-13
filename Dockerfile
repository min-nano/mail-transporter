# One image, two entrypoints:
#   Cloud Run  : default CMD (gunicorn serving mailtransporter.server:app)
#   GCE watcher: python -m mailtransporter.watcher  (set via --container-command)
#
# Reproducible: the base image is pinned by digest and every dependency is
# installed from the hash-locked requirements.txt (regenerate with
# `pip-compile --no-header --generate-hashes --strip-extras -o requirements.txt pyproject.toml`
# under Python 3.12; CI fails when the two drift). Dependabot keeps both current.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

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

# gthread: one process, a few threads, so /healthz and the 409 "busy" reply stay
# responsive while a long /sync runs (the sync worker ignores --threads).
# With gthread the worker keeps signalling the arbiter from its main loop while
# requests run on threads, so --timeout is a watchdog for a wedged worker, not a
# cap on request length (Cloud Run's request timeout bounds that).
CMD exec gunicorn --bind ":${PORT}" --worker-class gthread --workers 1 --threads 4 --timeout 60 --access-logfile - mailtransporter.server:app
