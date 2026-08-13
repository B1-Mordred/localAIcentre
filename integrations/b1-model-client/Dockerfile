FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    B1_MODEL_CACHE=/cache

WORKDIR /app

COPY b1_model_client /app/b1_model_client

RUN groupadd --system --gid 10001 b1 \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin b1 \
    && mkdir -p /cache \
    && chown -R 10001:10001 /cache

USER 10001:10001
VOLUME ["/cache"]

ENTRYPOINT ["python", "-m", "b1_model_client"]
CMD ["daemon", "--cache", "/cache"]
