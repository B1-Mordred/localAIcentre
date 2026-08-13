# syntax=docker/dockerfile:1.7
FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime@sha256:82e0d379a5dedd6303c89eda57bcc434c40be11f249ddfadfd5673b84351e806

ARG B1_MUSETALK_COMMIT=0a89dec45a0192b824e3cf4daf96c239440c5ed8
ARG B1_MUSETALK_TARBALL_SHA256=2e1685e4bcc58531c5118c0ba485199f22012016ed4608cab725a51d131f7ca7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=0 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC \
    XDG_CACHE_HOME=/srv/b1-ai-hub/cache/xdg \
    TORCH_HOME=/srv/b1-ai-hub/cache/torch \
    HF_HOME=/srv/b1-ai-hub/cache/huggingface \
    B1_LIPSYNC_RUNTIME_VERSION=b1-lipsync-musetalk/v0.1.0-b1 \
    B1_MUSETALK_COMMIT=${B1_MUSETALK_COMMIT} \
    B1_MUSETALK_TARBALL_SHA256=${B1_MUSETALK_TARBALL_SHA256}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libgl1 libglib2.0-0 libsm6 libxext6 tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system b1 && useradd --system --gid b1 --home /app b1

RUN mkdir -p /opt/musetalk \
    && curl -fsSL "https://github.com/TMElyralab/MuseTalk/archive/${B1_MUSETALK_COMMIT}.tar.gz" -o /tmp/musetalk.tar.gz \
    && echo "${B1_MUSETALK_TARBALL_SHA256}  /tmp/musetalk.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/musetalk.tar.gz --strip-components=1 -C /opt/musetalk \
    && rm -f /tmp/musetalk.tar.gz \
    && test -f /opt/musetalk/scripts/inference.py \
    && rm -rf /opt/musetalk/models \
    && ln -s /srv/b1-ai-hub/models /opt/musetalk/models

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
    && pip install --no-deps "mmengine==0.10.7" \
    && pip install --no-deps -f "https://download.openmmlab.com/mmcv/dist/cu117/torch2.0.0/index.html" "mmcv==2.0.1" \
    && pip install --no-deps "mmdet==3.1.0" "mmpose==1.1.0"

COPY mmlab-runtime-requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r mmlab-runtime-requirements.txt

RUN python - <<'PY'
from pathlib import Path

utils_path = Path("/opt/musetalk/musetalk/utils/utils.py")
utils_text = utils_path.read_text(encoding="utf-8")
utils_text = utils_text.replace(
    "    unet_config=os.path.join(\"models\", \"musetalkV15\", \"musetalk.json\"),\n"
    "    device=None,\n"
    "):",
    "    unet_config=os.path.join(\"models\", \"musetalkV15\", \"musetalk.json\"),\n"
    "    device=None,\n"
    "    use_float16=False,\n"
    "):",
    1,
)
utils_text = utils_text.replace(
    "    vae = VAE(\n"
    "        model_path = os.path.join(\"models\", vae_type),\n"
    "    )",
    "    vae = VAE(\n"
    "        model_path = os.path.join(\"models\", vae_type),\n"
    "        use_float16=use_float16,\n"
    "    )",
    1,
)
utils_text = utils_text.replace(
    "        unet_config=unet_config,\n"
    "        model_path=unet_model_path,\n"
    "        device=device\n"
    "    )",
    "        unet_config=unet_config,\n"
    "        model_path=unet_model_path,\n"
    "        device=device,\n"
    "        use_float16=use_float16\n"
    "    )",
    1,
)
utils_path.write_text(utils_text, encoding="utf-8")

inference_path = Path("/opt/musetalk/scripts/inference.py")
inference_text = inference_path.read_text(encoding="utf-8")
inference_text = inference_text.replace(
    "        vae_type=args.vae_type,\n"
    "        unet_config=args.unet_config,\n"
    "        device=device\n"
    "    )",
    "        vae_type=args.vae_type,\n"
    "        unet_config=args.unet_config,\n"
    "        device=device,\n"
    "        use_float16=args.use_float16\n"
    "    )",
    1,
)
inference_path.write_text(inference_text, encoding="utf-8")

vae_path = Path("/opt/musetalk/musetalk/models/vae.py")
vae_text = vae_path.read_text(encoding="utf-8")
vae_text = vae_text.replace(
    "        self.device = torch.device(\"cuda\" if torch.cuda.is_available() else \"cpu\")\n"
    "        self.vae.to(self.device)\n"
    "\n"
    "        if use_float16:\n"
    "            self.vae = self.vae.half()\n"
    "            self._use_float16 = True\n"
    "        else:\n"
    "            self._use_float16 = False",
    "        self.device = torch.device(\"cuda\" if torch.cuda.is_available() else \"cpu\")\n"
    "\n"
    "        if use_float16:\n"
    "            self.vae = self.vae.half()\n"
    "            self._use_float16 = True\n"
    "        else:\n"
    "            self._use_float16 = False\n"
    "\n"
    "        self.vae.to(self.device)",
    1,
)
vae_path.write_text(vae_text, encoding="utf-8")
PY

COPY app ./app

RUN mkdir -p /srv/b1-ai-hub/cache /srv/b1-ai-hub/models \
    && chown -R b1:b1 /srv/b1-ai-hub/cache /srv/b1-ai-hub/models /app \
    && chmod -R a+rX /opt/musetalk

USER b1
EXPOSE 8000

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
