FROM oven/bun:1.3.8@sha256:371d30538b69303ced927bb5915697ac7e2fa8cb409ee332c66009de64de5aa3 AS frontend

ARG B1_VOICEBOX_VERSION=v0.5.0
ARG B1_VOICEBOX_COMMIT=2bcb98d1a8b6fe05e15fbc1559e3085669e4035d
ARG B1_VOICEBOX_TARBALL_SHA256=d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://github.com/jamiepine/voicebox/archive/${B1_VOICEBOX_COMMIT}.tar.gz" -o /tmp/voicebox.tar.gz \
    && echo "${B1_VOICEBOX_TARBALL_SHA256}  /tmp/voicebox.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/voicebox.tar.gz --strip-components=1 -C /build \
    && rm -f /tmp/voicebox.tar.gz \
    && test -f /build/backend/main.py \
    && grep -q '"version": "0.5.0"' /build/package.json

RUN sed -i '/"tauri"/d; /"landing"/d' package.json \
    && sed -i -z 's/,\n  ]/\n  ]/' package.json \
    && bun install --no-save \
    && cd web \
    && bunx --bun vite build

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS backend-builder

ARG B1_VOICEBOX_COMMIT=2bcb98d1a8b6fe05e15fbc1559e3085669e4035d
ARG B1_VOICEBOX_TARBALL_SHA256=d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083
ARG B1_QWEN3_TTS_COMMIT=022e286b98fbec7e1e916cb940cdf532cd9f488e
ARG B1_LINACODEC_COMMIT=c0ae7c7285e121475c27592cfbb600624b714290
ARG B1_LUXTTS_COMMIT=28ae6a61151684fffc9d1a7aa15eafa02286fe0b

WORKDIR /build
COPY constraints.txt /tmp/voicebox-constraints.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://github.com/jamiepine/voicebox/archive/${B1_VOICEBOX_COMMIT}.tar.gz" -o /tmp/voicebox.tar.gz \
    && echo "${B1_VOICEBOX_TARBALL_SHA256}  /tmp/voicebox.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/voicebox.tar.gz --strip-components=1 -C /build \
    && rm -f /tmp/voicebox.tar.gz

RUN python - <<'PY'
import os
from pathlib import Path

path = Path("backend/requirements.txt")
text = path.read_text(encoding="utf-8")
text = text.replace(
    "linacodec @ git+https://github.com/ysharma3501/LinaCodec.git",
    f"linacodec @ git+https://github.com/ysharma3501/LinaCodec.git@{os.environ['B1_LINACODEC_COMMIT']}",
)
text = text.replace(
    "Zipvoice @ git+https://github.com/ysharma3501/LuxTTS.git",
    f"Zipvoice @ git+https://github.com/ysharma3501/LuxTTS.git@{os.environ['B1_LUXTTS_COMMIT']}",
)
text = text.replace(
    "qwen-tts>=0.0.5",
    f"qwen-tts @ git+https://github.com/QwenLM/Qwen3-TTS.git@{os.environ['B1_QWEN3_TTS_COMMIT']}",
)
text = text.replace(
    "en_core_web_sm-3.8.0-py3-none-any.whl",
    "en_core_web_sm-3.8.0-py3-none-any.whl#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85",
)
path.write_text(text, encoding="utf-8")
PY

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --prefix=/install -r backend/requirements.txt -c /tmp/voicebox-constraints.txt \
    && python -m pip install --no-cache-dir --prefix=/install --no-deps chatterbox-tts==0.1.7 \
    && python -m pip install --no-cache-dir --prefix=/install --no-deps hume-tada==0.1.9

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ARG B1_VOICEBOX_UID=999
ARG B1_VOICEBOX_GID=999

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    DO_NOT_TRACK=1 \
    VOICEBOX_MODELS_DIR=/srv/b1-ai-hub/models \
    NUMBA_CACHE_DIR=/tmp/numba_cache \
    HF_HOME=/srv/b1-ai-hub/cache/huggingface \
    XDG_CACHE_HOME=/srv/b1-ai-hub/cache/xdg

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg libsndfile1 tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid "${B1_VOICEBOX_GID}" b1 \
    && useradd --system --uid "${B1_VOICEBOX_UID}" --gid b1 --home-dir /srv/b1-ai-hub/voicebox --shell /usr/sbin/nologin b1

COPY --from=backend-builder /install /usr/local
COPY --from=backend-builder --chown=b1:b1 /build/backend /app/backend
COPY --from=frontend --chown=b1:b1 /build/web/dist /app/frontend
COPY b1-voicebox-entrypoint.sh /usr/local/bin/b1-voicebox-entrypoint.sh

RUN mkdir -p /srv/b1-ai-hub/voicebox /srv/b1-ai-hub/models /srv/b1-ai-hub/cache \
    && chown -R b1:b1 /srv/b1-ai-hub /app/backend /app/frontend \
    && chmod 0755 /usr/local/bin/b1-voicebox-entrypoint.sh

USER b1
EXPOSE 17493

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=120s \
    CMD curl -fsS http://127.0.0.1:17493/health || exit 1

ENTRYPOINT ["tini", "--", "b1-voicebox-entrypoint.sh"]
