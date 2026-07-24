FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

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
