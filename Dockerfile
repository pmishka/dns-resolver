FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates
COPY static ./static

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 5000

CMD ["sh", "-c", "gunicorn -w ${GUNICORN_WORKERS:-1} --timeout ${GUNICORN_TIMEOUT:-180} -b 0.0.0.0:${APP_PORT:-5000} app:app"]
