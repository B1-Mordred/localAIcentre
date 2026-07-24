FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG B1_INSTALL_PIPER=true
ARG B1_PIPER_RELEASE=2023.11.14-2
ARG B1_PIPER_ASSET=piper_linux_x86_64.tar.gz
ARG B1_PIPER_SHA256=a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    B1_PIPER_BINARY=/opt/piper/piper

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system b1 && useradd --system --gid b1 --home /app b1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/install_piper.py /tmp/install_piper.py
RUN if [ "${B1_INSTALL_PIPER}" = "true" ]; then \
      python /tmp/install_piper.py \
        --release "${B1_PIPER_RELEASE}" \
        --asset "${B1_PIPER_ASSET}" \
        --sha256 "${B1_PIPER_SHA256}" \
        --install-dir /opt \
      && /opt/piper/piper --help >/dev/null 2>&1; \
    fi \
    && rm -f /tmp/install_piper.py /tmp/piper.tar.gz

COPY app ./app

USER b1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
