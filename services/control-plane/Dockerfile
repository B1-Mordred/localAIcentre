FROM postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3 AS postgres-client

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgnutls30 \
        libldap-2.5-0 \
        libpq5 \
        libsasl2-2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=postgres-client /usr/lib/postgresql/17/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=postgres-client /usr/lib/postgresql/17/bin/pg_restore /usr/local/bin/pg_restore

RUN pg_dump --version | grep -F "pg_dump (PostgreSQL) 17.6" \
    && pg_restore --version | grep -F "pg_restore (PostgreSQL) 17.6"

RUN groupadd --system b1 && useradd --system --gid b1 --home /app b1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

USER b1
EXPOSE 8000

CMD ["python", "-m", "app.run"]
