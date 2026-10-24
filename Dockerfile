# syntax=docker/dockerfile:1
# Multi-stage build.
#   base    - interpreter + runtime dependencies only
#   test    - base + tests + dev dependencies; runs the suite (CI: --target test)
#   runtime - base + compass package only, non-root; the default/final stage
#
# `docker build .` (no --target) produces the runtime image without running
# the suite, so Railway and CI do not pay for a second pytest pass.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY compass ./compass
COPY tests ./tests
# tests/test_image_release.py and tests/test_database_backup_worker.py load
# ops/ by path; ops/ is still excluded from the runtime stage.
COPY ops ./ops
RUN COMPASS_LOCAL=true python -m pytest -q -p no:cacheprovider

FROM base AS runtime
ARG COMPASS_REVISION=unknown
ENV COMPASS_REVISION=$COMPASS_REVISION
COPY compass ./compass
RUN useradd --create-home --uid 10001 compass && chown -R compass:compass /app
USER compass
CMD ["sh", "-c", "exec uvicorn compass.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
