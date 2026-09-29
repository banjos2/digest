FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends age postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY manage.py ./
COPY src ./src
COPY config ./config
COPY templates ./templates
COPY deploy ./deploy
RUN useradd --create-home --uid 10001 digest \
    && mkdir -p /app/var /app/staticfiles /run/secrets/telethon /backups /erasure-guard \
    && chown -R digest:digest /app /run/secrets/telethon /backups /erasure-guard \
    && chmod +x /app/deploy/*.sh
USER digest
EXPOSE 8000
CMD ["gunicorn", "digest_service.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-"]
