FROM pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime@sha256:e05438443ae3c407e8d04447091a959dbb6757b6290b128770c3c787d4bd442b

ARG B1_COMFYUI_VERSION=v0.3.77
ARG B1_COMFYUI_COMMIT=59afc3984868289f808d02fa5cd180edfb2de240
ARG B1_COMFYUI_TARBALL_SHA256=0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1
ARG B1_COMFYUI_UID=999
ARG B1_COMFYUI_GID=999

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    XDG_CACHE_HOME=/srv/b1-ai-hub/cache/xdg \
    HF_HOME=/srv/b1-ai-hub/cache/huggingface \
    TORCH_HOME=/srv/b1-ai-hub/cache/torch

WORKDIR /opt/comfyui

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg libgl1 libglib2.0-0 tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid "${B1_COMFYUI_GID}" b1 \
    && useradd --system --uid "${B1_COMFYUI_UID}" --gid b1 --home-dir /srv/b1-ai-hub/comfyui --shell /usr/sbin/nologin b1

RUN curl -fsSL "https://github.com/Comfy-Org/ComfyUI/archive/${B1_COMFYUI_COMMIT}.tar.gz" -o /tmp/comfyui.tar.gz \
    && echo "${B1_COMFYUI_TARBALL_SHA256}  /tmp/comfyui.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/comfyui.tar.gz --strip-components=1 -C /opt/comfyui \
    && rm -f /tmp/comfyui.tar.gz \
    && test -f /opt/comfyui/main.py \
    && grep -q 'comfyui-frontend-package==1.32.10' /opt/comfyui/requirements.txt

RUN grep -Ev '^(torch|torchvision|torchaudio)$' requirements.txt > /tmp/comfyui-requirements.txt \
    && python -m pip install --no-cache-dir -r /tmp/comfyui-requirements.txt \
    && python -c "import importlib.metadata as metadata, torch; assert torch.__version__.startswith('2.8.0'), torch.__version__; assert metadata.version('comfyui-frontend-package') == '1.32.10'" \
    && rm -f /tmp/comfyui-requirements.txt

COPY extra_model_paths.yaml /opt/b1/comfyui/extra_model_paths.yaml
COPY b1-comfyui-entrypoint.sh /usr/local/bin/b1-comfyui-entrypoint.sh

RUN mkdir -p /srv/b1-ai-hub/comfyui/input /srv/b1-ai-hub/comfyui/output /srv/b1-ai-hub/comfyui/temp /srv/b1-ai-hub/comfyui/user /srv/b1-ai-hub/cache \
    && chown -R b1:b1 /srv/b1-ai-hub /opt/b1 \
    && chmod 0755 /usr/local/bin/b1-comfyui-entrypoint.sh

USER b1
EXPOSE 8188

ENTRYPOINT ["tini", "--", "b1-comfyui-entrypoint.sh"]
