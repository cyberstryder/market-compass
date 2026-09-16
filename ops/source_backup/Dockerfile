FROM postgres:18-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/backup && /opt/backup/bin/pip install --no-cache-dir 'psycopg[binary]>=3.2,<4' 'cryptography>=44,<47'
COPY ops/source_backup/worker.py /opt/worker.py
USER postgres
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/opt/backup/bin/python", "/opt/worker.py"]
