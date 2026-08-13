FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG B1_INSTALL_PIPER=true
ARG B1_PIPER_RELEASE=2023.11.14-2
ARG B1_PIPER_ASSET=piper_linux_x86_64.tar.gz
ARG B1_PIPER_SHA256=a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    B1_PIPER_BINARY=/opt/piper/piper \
    B1_AUDIO_CPU_RUNTIME_VERSION=b1-audio-cpu/v0.1.0-b1 \
    B1_AUDIO_CPU_BASE_IMAGE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c \
    B1_AUDIO_CPU_PIPER_RELEASE=${B1_PIPER_RELEASE} \
    B1_AUDIO_CPU_PIPER_ASSET=${B1_PIPER_ASSET} \
    B1_AUDIO_CPU_PIPER_SHA256=${B1_PIPER_SHA256}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
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
