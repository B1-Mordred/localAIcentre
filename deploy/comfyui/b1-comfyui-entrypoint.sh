#!/usr/bin/env bash
set -euo pipefail

input_dir="${B1_COMFYUI_INPUT_DIR:-/srv/b1-ai-hub/comfyui/input}"
output_dir="${B1_COMFYUI_OUTPUT_DIR:-/srv/b1-ai-hub/comfyui/output}"
temp_dir="${B1_COMFYUI_TEMP_DIR:-/srv/b1-ai-hub/comfyui/temp}"
user_dir="${B1_COMFYUI_USER_DIR:-/srv/b1-ai-hub/comfyui/user}"
generated_extra_model_paths="${B1_COMFYUI_GENERATED_EXTRA_MODEL_PATHS:-$temp_dir/generated-extra_model_paths.yaml}"
extra_model_paths="${B1_COMFYUI_EXTRA_MODEL_PATHS_CONFIG:-$generated_extra_model_paths}"
export B1_COMFYUI_GENERATED_EXTRA_MODEL_PATHS="$generated_extra_model_paths"

mkdir -p "$input_dir" "$output_dir" "$temp_dir" "$user_dir" \
  "${XDG_CACHE_HOME:-/srv/b1-ai-hub/cache/xdg}" \
  "${HF_HOME:-/srv/b1-ai-hub/cache/huggingface}" \
  "${TORCH_HOME:-/srv/b1-ai-hub/cache/torch}"

if [ "${B1_COMFYUI_GENERATE_EXTRA_MODEL_PATHS:-true}" = "true" ]; then
  python /opt/b1/comfyui/b1-generate-extra-model-paths.py
fi

args=(
  python
  main.py
  --listen "${B1_COMFYUI_LISTEN:-0.0.0.0}"
  --port "${B1_COMFYUI_PORT:-8188}"
  --disable-auto-launch
  --log-stdout
  --extra-model-paths-config "$extra_model_paths"
  --input-directory "$input_dir"
  --output-directory "$output_dir"
  --temp-directory "$temp_dir"
  --user-directory "$user_dir"
  --reserve-vram "${B1_COMFYUI_RESERVE_VRAM_GIB:-1.0}"
  --max-upload-size "${B1_COMFYUI_MAX_UPLOAD_MB:-256}"
)

if [ "${B1_COMFYUI_DISABLE_API_NODES:-true}" = "true" ]; then
  args+=(--disable-api-nodes)
fi

if [ "${B1_COMFYUI_CACHE_NONE:-true}" = "true" ]; then
  args+=(--cache-none)
fi

if [ "${B1_COMFYUI_LOWVRAM:-true}" = "true" ]; then
  args+=(--lowvram)
fi

exec "${args[@]}"
