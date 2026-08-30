FROM python:3.11-slim AS runtime

ARG INSTALL_VOICE=true
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-voice.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && if [ "$INSTALL_VOICE" = "true" ]; then pip install -r requirements-voice.txt; fi

COPY app ./app
COPY cards ./cards
COPY questions ./questions
COPY resources ./resources
COPY public ./public
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md
COPY references/licenses ./references/licenses

RUN addgroup --system interview \
    && adduser --system --ingroup interview --home /app interview \
    && mkdir -p /app/data \
    && chown -R interview:interview /app/data

USER interview
EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
