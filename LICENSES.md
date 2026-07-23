# Licenses

B1 AI Hub combines first-party code with pinned upstream containers and optional model/runtime integrations.

## Repository Code

Until a final project license is selected, treat first-party code in this repository as internal/private project code. Do not redistribute it outside the B1 environment without owner approval.

## Runtime and Container Licenses

The production bill of materials must record the exact image references, upstream project licenses, and source URLs for:

- Caddy
- Open WebUI
- LocalAI
- ComfyUI
- Voicebox
- Piper TTS
- Vosk STT
- PostgreSQL
- Redis
- Python and Node base images
- any approved ComfyUI custom nodes

Pinned image versions currently used by the first runnable slice are documented in `compose.yaml`. The production LocalAI override additionally pins:

- image: `localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973`
- verified linux/amd64 platform manifest: `sha256:1b27b2469dcd78b21c33034eb3503efcb07330380b9ade00c14c48b2b09b641d`
- source image repository: `https://hub.docker.com/r/localai/localai`
- upstream project: `https://github.com/mudler/LocalAI`
- upstream license: MIT as declared by the LocalAI project

The production ComfyUI override builds a B1 image from pinned upstream source because no official upstream Docker image currently satisfies the B1 Compose/security contract:

- source: `https://github.com/Comfy-Org/ComfyUI`
- version: `v0.3.77`
- commit: `59afc3984868289f808d02fa5cd180edfb2de240`
- source archive: `https://github.com/Comfy-Org/ComfyUI/archive/59afc3984868289f808d02fa5cd180edfb2de240.tar.gz`
- source archive SHA-256: `0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1`
- base image: `pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime@sha256:e05438443ae3c407e8d04447091a959dbb6757b6290b128770c3c787d4bd442b`
- upstream license: GPL-3.0 as declared by the ComfyUI project

The production Voicebox override builds a B1 image from pinned Jamie Pine Voicebox source because the upstream Dockerfile uses floating base images and unpinned Git dependency references:

- source: `https://github.com/jamiepine/voicebox`
- version: `v0.5.0`
- commit: `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`
- source archive: `https://github.com/jamiepine/voicebox/archive/2bcb98d1a8b6fe05e15fbc1559e3085669e4035d.tar.gz`
- source archive SHA-256: `d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083`
- frontend base image: `oven/bun:1.3.8@sha256:371d30538b69303ced927bb5915697ac7e2fa8cb409ee332c66009de64de5aa3`
- backend/runtime base image: `python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93`
- pinned Git dependency commits: `QwenLM/Qwen3-TTS@022e286b98fbec7e1e916cb940cdf532cd9f488e`, `ysharma3501/LinaCodec@c0ae7c7285e121475c27592cfbb600624b714290`, and `ysharma3501/LuxTTS@28ae6a61151684fffc9d1a7aa15eafa02286fe0b`
- resolved Python dependency constraints: `deploy/voicebox/constraints.txt`
- upstream license: MIT as declared by the Voicebox project

The `audio-cpu` image can install the rhasspy/piper Linux x86_64 release asset during build when `B1_INSTALL_PIPER=true`:

- release: `2023.11.14-2`
- source: `https://github.com/rhasspy/piper/releases/tag/2023.11.14-2`
- asset: `piper_linux_x86_64.tar.gz`
- SHA-256: `a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992`
- upstream license: MIT as declared by the rhasspy/piper project

Piper model weights are not bundled by this repository and must be tracked as model artifacts with their own source, checksum, and license metadata. The seed catalog includes an available `b1-piper-en-us-amy-low` recommendation sourced from `https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/low/`; its ONNX and JSON config SHA-256 hashes are recorded in the manifest, and the Hugging Face repository declares the voice collection under MIT.

The `audio-cpu` image also pins local ONNX embedding runtime dependencies:

- `onnxruntime==1.27.0`
- `tokenizers==0.23.1`
- `numpy==2.5.1`
- Debian `libgomp1` from the pinned Python slim base distribution

Embedding model weights are not bundled by this repository. The seed catalog includes an available `b1-minilm-l6-v2-onnx-q4` recommendation sourced from `https://huggingface.co/onnx-community/all-MiniLM-L6-v2-ONNX/resolve/aff7a1dc4e8a1ea593e6ea21e95c22ef0a25966f/`; its tokenizer, ONNX graph, and ONNX external-data SHA-256 hashes are recorded in the manifest, and the Hugging Face model page declares Apache-2.0.

The `audio-cpu` image also pins `vosk==0.3.45` for local CPU speech-to-text.

STT model weights are not bundled by this repository. The seed catalog includes an available `b1-vosk-small-en-us-0.15` recommendation sourced from `https://alphacephei.com/kaldi/models/vosk-model-small-en-us-0.15.zip`; the official zip SHA-256 is recorded in the manifest, Model Hub safely extracts it into the read-only runtime view, and the Vosk model listing declares Apache-2.0.

## Model Licenses

Model weights are not bundled in this repository. Model manifests must record source, revision, checksum, license, redistribution policy, attribution, and any acceptance requirements. Inference-only models must not be exposed through Model Hub blob download endpoints.
