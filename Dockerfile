FROM python:3.12-slim

# Don't write .pyc files, and flush logs straight to the console
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user. It can't write to /app (owned by root), so the
# SQLite database lives in /app/data, which it owns. Mount a volume there
# to keep events across container restarts.
RUN useradd --create-home appuser \
    && mkdir /app/data \
    && chown appuser /app/data
ENV HOOKWATCH_DB_URL=sqlite:////app/data/hookwatch.db
VOLUME /app/data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
