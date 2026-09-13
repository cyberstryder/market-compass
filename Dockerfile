FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY compass ./compass
COPY tests ./tests
COPY pine ./pine
RUN COMPASS_LOCAL=true python -m pytest -q
RUN useradd --create-home --uid 10001 compass && chown -R compass:compass /app
USER compass
CMD ["sh", "-c", "uvicorn compass.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
