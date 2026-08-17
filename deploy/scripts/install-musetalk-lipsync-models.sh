#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${B1_DATA_ROOT:-/srv/b1-ai-hub}"
MODEL_ROOT="${B1_LIPSYNC_MODEL_ROOT:-${DATA_ROOT}/models/runtime-views/lipsync}"
TMP_ROOT="${TMPDIR:-/tmp}/b1-musetalk-lipsync-downloads"

mkdir -p "${MODEL_ROOT}" "${TMP_ROOT}"

download_hf() {
  local repo="$1"
  local source_path="$2"
  local destination="$3"
  local sha256="$4"
  local url="https://huggingface.co/${repo}/resolve/main/${source_path}"

  mkdir -p "$(dirname "${destination}")"
  if [[ -f "${destination}" ]] && echo "${sha256}  ${destination}" | sha256sum -c - >/dev/null 2>&1; then
    echo "ok ${destination}"
    return
  fi
  local partial="${destination}.partial"
  curl --fail --location --continue-at - --output "${partial}" "${url}"
  echo "${sha256}  ${partial}" | sha256sum -c -
  mv "${partial}" "${destination}"
}

download_url() {
  local url="$1"
  local destination="$2"
  local sha256="$3"

  mkdir -p "$(dirname "${destination}")"
  if [[ -f "${destination}" ]] && echo "${sha256}  ${destination}" | sha256sum -c - >/dev/null 2>&1; then
    echo "ok ${destination}"
    return
  fi
  local partial="${destination}.partial"
  curl --fail --location --continue-at - --output "${partial}" "${url}"
  echo "${sha256}  ${partial}" | sha256sum -c -
  mv "${partial}" "${destination}"
}

download_gdrive() {
  local file_id="$1"
  local destination="$2"
  local sha256="$3"

  mkdir -p "$(dirname "${destination}")"
  if [[ -f "${destination}" ]] && echo "${sha256}  ${destination}" | sha256sum -c - >/dev/null 2>&1; then
    echo "ok ${destination}"
    return
  fi
  local venv="${TMP_ROOT}/gdown-venv"
  if [[ ! -x "${venv}/bin/python" ]]; then
    python3 -m venv "${venv}"
    "${venv}/bin/pip" install --disable-pip-version-check --no-cache-dir gdown==5.2.0
  fi
  local partial="${destination}.partial"
  "${venv}/bin/python" -m gdown "https://drive.google.com/uc?id=${file_id}" -O "${partial}"
  echo "${sha256}  ${partial}" | sha256sum -c -
  mv "${partial}" "${destination}"
}

download_hf "TMElyralab/MuseTalk" "musetalkV15/musetalk.json" "${MODEL_ROOT}/musetalkV15/musetalk.json" "5b6923aee04d71692e0e9846c471e0a4ea07a4f686d39545e472bd4ba17e1b47"
download_hf "TMElyralab/MuseTalk" "musetalkV15/unet.pth" "${MODEL_ROOT}/musetalkV15/unet.pth" "7ebf6c98c181e20838e4c0054e96e944ac60d5d692cc01db42839fe11b787007"
download_hf "stabilityai/sd-vae-ft-mse" "config.json" "${MODEL_ROOT}/sd-vae/config.json" "92d3dfb746fca211a2c9e019e285f8597412211728dce3c5bcf4eda0f2d62e7e"
download_hf "stabilityai/sd-vae-ft-mse" "diffusion_pytorch_model.bin" "${MODEL_ROOT}/sd-vae/diffusion_pytorch_model.bin" "1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc"
download_hf "openai/whisper-tiny" "config.json" "${MODEL_ROOT}/whisper/config.json" "ffdccec4f3211f4c63310f2b7098f309fe70f3952cedc5e4d11e43f5b2379b98"
download_hf "openai/whisper-tiny" "pytorch_model.bin" "${MODEL_ROOT}/whisper/pytorch_model.bin" "9607f98a2b22d9e229ae43c52ecea79dcede9e0c5cfae67e8da6eda86d8aac1d"
download_hf "openai/whisper-tiny" "preprocessor_config.json" "${MODEL_ROOT}/whisper/preprocessor_config.json" "9b5cd03a36fbb8a627c64d98a5b5b126ead95a77720723944487311f0110b666"
download_hf "yzd-v/DWPose" "dw-ll_ucoco_384.pth" "${MODEL_ROOT}/dwpose/dw-ll_ucoco_384.pth" "0d9408b13cd863c4e95a149dd31232f88f2a12aa6cf8964ed74d7d97748c7a07"
download_hf "ByteDance/LatentSync" "latentsync_syncnet.pt" "${MODEL_ROOT}/syncnet/latentsync_syncnet.pt" "38fa63bad3ed2332f647c40a5dc616cb0e233db8579f698f62af4c41965c4da5"
download_gdrive "154JgKpzCPW82qINcVieuPH3fZ2e0P812" "${MODEL_ROOT}/face-parse-bisent/79999_iter.pth" "468e13ca13a9b43cc0881a9f99083a430e9c0a38abd935431d1c28ee94b26567"
download_url "https://download.pytorch.org/models/resnet18-5c106cde.pth" "${MODEL_ROOT}/face-parse-bisent/resnet18-5c106cde.pth" "5c106cde386e87d4033832f2996f5493238eda96ccf559d1d62760c4de0613f8"
download_url "https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth" "${MODEL_ROOT}/torch/hub/checkpoints/s3fd-619a316812.pth" "619a31681264d3f7f7fc7a16a42cbbe8b23f31a256f75a366e5a1bcd59b33543"

find "${MODEL_ROOT}" -type f -exec chmod a+r {} +
find "${MODEL_ROOT}" -type d -exec chmod a+rx {} +
echo "MuseTalk lipsync model files are installed in ${MODEL_ROOT}"
