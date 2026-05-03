# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Non-root user
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin broker

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

USER 10001
EXPOSE 8000

# --no-server-header strips uvicorn's Server header (defense in depth; FD also strips).
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--no-server-header", \
     "--no-date-header", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
