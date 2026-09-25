# Dev/test image. Repo is bind-mounted read-only at /app; nothing is baked in but deps.
# Python 3.12 = the README's minimum supported version.
# NOTE: MetaTrader5 is Windows-only — MT5 demo tests (Phase 12) cannot run in this image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /deps
COPY pyproject.toml .
# Install runtime + dev deps straight from pyproject (no package install; PYTHONPATH=src).
RUN python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; print('\n'.join(p['dependencies'] + p['optional-dependencies']['dev']))" > requirements.txt \
 && pip install -r requirements.txt

WORKDIR /app
