from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


MANAGED_MARKER = "# b1-ai-hub managed localai model config"
MANAGED_PREFIX = "b1-managed-"
MANAGED_MODELS_CONFIG = "b1-managed-models.yaml"
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SAFE_CONFIG_RE = re.compile(r"[^A-Za-z0-9._+-]+")
GGUF_VALUE_FORMATS: dict[int, str] = {
    0: "B",
    1: "b",
    2: "H",
    3: "h",
    4: "I",
    5: "i",
    6: "f",
    7: "?",
    10: "Q",
    11: "q",
    12: "d",
}


GEMMA4_CHAT_TEMPLATE = """\
  chat_message: |
    {{- if eq .RoleName "system" -}}<|turn>system
    {{ .Content }}<turn|>
    {{- else if eq .RoleName "assistant" -}}<|turn>model
    {{ .Content }}<turn|>
    {{- else -}}<|turn>user
    {{ .Content }}<turn|>
    {{- end -}}
  chat: |
    {{- if and .SystemPrompt (not .SuppressSystemPrompt) -}}<|turn>system
    {{ .SystemPrompt }}<turn|>
    {{- end -}}{{ .Input }}<|turn>model
  join_chat_messages_by_character: ''
"""


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def yaml_scalar(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def config_safe(value: str) -> str:
    cleaned = SAFE_CONFIG_RE.sub("-", value.strip()).strip(".-")
    return cleaned[:128] or "model"


def safe_relative_path(value: str) -> PurePosixPath | None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def read_gguf_string(handle: Any) -> str:
    length_raw = handle.read(8)
    if len(length_raw) != 8:
        raise ValueError("short gguf string length")
    length = struct.unpack("<Q", length_raw)[0]
    if length > 16 * 1024 * 1024:
        raise ValueError("gguf string metadata exceeds safety limit")
    data = handle.read(length)
    if len(data) != length:
        raise ValueError("short gguf string")
    return data.decode("utf-8", errors="replace")


def skip_gguf_value(handle: Any, value_type: int) -> Any:
    if value_type == 8:
        return read_gguf_string(handle)
    if value_type == 9:
        raw = handle.read(12)
        if len(raw) != 12:
            raise ValueError("short gguf array header")
        element_type, count = struct.unpack("<IQ", raw)
        element_format = GGUF_VALUE_FORMATS.get(element_type)
        if element_format is not None:
            handle.seek(struct.calcsize("<" + element_format) * count, os.SEEK_CUR)
            return None
        for _ in range(count):
            skip_gguf_value(handle, element_type)
        return None
    value_format = GGUF_VALUE_FORMATS.get(value_type)
    if value_format is None:
        raise ValueError(f"unsupported gguf metadata type {value_type}")
    size = struct.calcsize("<" + value_format)
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("short gguf scalar")
    return struct.unpack("<" + value_format, data)[0]


def gguf_metadata(path: Path, keys: set[str]) -> dict[str, Any]:
    if not keys:
        return {}
    found: dict[str, Any] = {}
    try:
        with path.open("rb") as handle:
            if handle.read(4) != b"GGUF":
                return found
            version_raw = handle.read(4)
            if len(version_raw) != 4:
                return found
            version = struct.unpack("<I", version_raw)[0]
            if version not in {2, 3}:
                return found
            counts_raw = handle.read(16)
            if len(counts_raw) != 16:
                return found
            _, metadata_count = struct.unpack("<QQ", counts_raw)
            for _ in range(min(metadata_count, 10_000)):
                key = read_gguf_string(handle)
                value_type_raw = handle.read(4)
                if len(value_type_raw) != 4:
                    raise ValueError("short gguf value type")
                value_type = struct.unpack("<I", value_type_raw)[0]
                if key in keys:
                    found[key] = skip_gguf_value(handle, value_type)
                    if keys.issubset(found):
                        break
                else:
                    skip_gguf_value(handle, value_type)
    except (OSError, ValueError, struct.error):
        return found
    return found


def load_runtime_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    manifest = payload.get("manifest")
    return manifest if isinstance(manifest, dict) else None


def localai_gguf_file(manifest: dict[str, Any], view_root: Path) -> str | None:
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            continue
        path_value = item.get("path")
        if not isinstance(path_value, str):
            continue
        relative = safe_relative_path(path_value)
        if relative is None:
            continue
        file_format = str(item.get("format") or "").lower()
        if file_format != "gguf" and not relative.name.lower().endswith(".gguf"):
            continue
        candidate = view_root.joinpath(*relative.parts)
        if candidate.is_file() and not candidate.is_symlink():
            return relative.as_posix()
    return None


def model_hint_text(manifest: dict[str, Any], relative_file: str) -> str:
    return " ".join(
        str(value).lower()
        for value in (
            manifest.get("id"),
            manifest.get("display_name"),
            relative_file,
        )
        if isinstance(value, str)
    )


def clamp_gpu_layers(value: int, *, allow_auto_fit: bool = False) -> int:
    if allow_auto_fit and value == -1:
        return -1
    return max(0, value)


def is_gemma4_e4b(manifest: dict[str, Any], relative_file: str, prompt_family: str | None) -> bool:
    if prompt_family != "gemma4":
        return False
    hint = model_hint_text(manifest, relative_file)
    return "gemma-4-e4b" in hint or "gemma4-e4b" in hint or "e4b" in hint


def is_laguna_xs21(manifest: dict[str, Any], relative_file: str, prompt_family: str | None) -> bool:
    if prompt_family != "laguna-xs-2.1":
        return False
    hint = model_hint_text(manifest, relative_file)
    return (
        "laguna-xs-2.1" in hint
        or "laguna-xs-21" in hint
        or "laguna-xs_2.1" in hint
        or "poolside laguna xs 2.1" in hint
    )


def manifest_context_size(manifest: dict[str, Any], relative_file: str, prompt_family: str | None) -> int:
    estimate = manifest.get("resource_estimate") if isinstance(manifest.get("resource_estimate"), dict) else {}
    declared = estimate.get("context_tokens")
    if not isinstance(declared, int) or declared <= 0:
        declared = env_int("B1_LOCALAI_MANAGED_DEFAULT_CONTEXT_SIZE", 2048)
    maximum = max(512, env_int("B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE", 4096))
    if is_gemma4_e4b(manifest, relative_file, prompt_family):
        maximum = max(512, env_int("B1_LOCALAI_MANAGED_GEMMA4_E4B_CONTEXT_SIZE", 4096))
    if is_laguna_xs21(manifest, relative_file, prompt_family):
        maximum = max(512, env_int("B1_LOCALAI_MANAGED_LAGUNA_XS_CONTEXT_SIZE", 32768))
    return max(512, min(declared, maximum))


def manifest_file_size_gib(manifest: dict[str, Any], view_root: Path, relative_file: str) -> float:
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            continue
        if item.get("path") != relative_file:
            continue
        size_bytes = item.get("size_bytes")
        if isinstance(size_bytes, int) and size_bytes > 0:
            return size_bytes / (1024**3)
    try:
        return (view_root / relative_file).stat().st_size / (1024**3)
    except OSError:
        return 0.0


def managed_gpu_layers(manifest: dict[str, Any], view_root: Path, relative_file: str, prompt_family: str | None) -> int:
    forced = os.getenv("B1_LOCALAI_MANAGED_GPU_LAYERS")
    if forced is not None:
        return clamp_gpu_layers(env_int("B1_LOCALAI_MANAGED_GPU_LAYERS", 0), allow_auto_fit=True)
    if is_gemma4_e4b(manifest, relative_file, prompt_family):
        return clamp_gpu_layers(env_int("B1_LOCALAI_MANAGED_GEMMA4_E4B_GPU_LAYERS", 32))
    if is_laguna_xs21(manifest, relative_file, prompt_family):
        return clamp_gpu_layers(env_int("B1_LOCALAI_MANAGED_LAGUNA_XS_GPU_LAYERS", -1), allow_auto_fit=True)
    size_gib = manifest_file_size_gib(manifest, view_root, relative_file)
    medium_threshold = max(0.1, env_float("B1_LOCALAI_MANAGED_MEDIUM_MODEL_SIZE_GIB", 2.0))
    large_threshold = max(medium_threshold, env_float("B1_LOCALAI_MANAGED_LARGE_MODEL_SIZE_GIB", 5.0))
    if size_gib >= large_threshold:
        return clamp_gpu_layers(env_int("B1_LOCALAI_MANAGED_LARGE_GPU_LAYERS", 24))
    if size_gib >= medium_threshold:
        return clamp_gpu_layers(env_int("B1_LOCALAI_MANAGED_MEDIUM_GPU_LAYERS", 28))
    return clamp_gpu_layers(env_int("B1_LOCALAI_MANAGED_SMALL_GPU_LAYERS", 99999999))


def localai_prompt_family(manifest: dict[str, Any], view_root: Path, relative_file: str) -> str | None:
    model_file = view_root.joinpath(*PurePosixPath(relative_file).parts)
    metadata = gguf_metadata(model_file, {"general.architecture"})
    architecture = str(metadata.get("general.architecture") or "").strip().lower()
    if architecture == "gemma4":
        return "gemma4"
    if architecture == "laguna":
        return "laguna-xs-2.1"
    model_hint = model_hint_text(manifest, relative_file)
    if "gemma-4" in model_hint or "gemma4" in model_hint:
        return "gemma4"
    if "laguna-xs-2.1" in model_hint or "laguna-xs-21" in model_hint:
        return "laguna-xs-2.1"
    return None


def localai_parameter_defaults(prompt_family: str | None) -> dict[str, Any]:
    if prompt_family == "gemma4":
        return {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
    if prompt_family == "laguna-xs-2.1":
        return {"temperature": 1.0, "top_p": 1.0, "top_k": 20}
    return {"temperature": 0.2}


def localai_template_lines(prompt_family: str | None) -> list[str]:
    if prompt_family == "gemma4":
        return ["template:", *GEMMA4_CHAT_TEMPLATE.rstrip("\n").splitlines()]
    return []


def localai_generation_control_lines(prompt_family: str | None) -> list[str]:
    if prompt_family == "gemma4":
        return [
            "chat_template_kwargs:",
            "  enable_thinking: false",
            "reasoning:",
            "  disable_reasoning: true",
            "  disable_reasoning_tag_prefill: true",
            "stopwords:",
            "  - '<turn|>'",
            "  - '<|turn>user'",
            "  - '<|turn>system'",
        ]
    if prompt_family == "laguna-xs-2.1":
        return [
            "chat_template_kwargs:",
            "  enable_thinking: true",
        ]
    return []


def localai_batch_size(prompt_family: str | None) -> int:
    if prompt_family == "laguna-xs-2.1":
        return max(32, env_int("B1_LOCALAI_MANAGED_LAGUNA_XS_BATCH", 1024))
    return max(32, env_int("B1_LOCALAI_MANAGED_BATCH", 128))


def localai_fit_target_mib(prompt_family: str | None) -> int:
    if prompt_family == "laguna-xs-2.1":
        return max(256, env_int("B1_LOCALAI_MANAGED_LAGUNA_XS_FIT_TARGET_MIB", 1024))
    return max(256, env_int("B1_LOCALAI_MANAGED_FIT_TARGET_MIB", 1024))


def localai_fit_ctx(prompt_family: str | None) -> int:
    if prompt_family == "laguna-xs-2.1":
        return max(512, env_int("B1_LOCALAI_MANAGED_LAGUNA_XS_FIT_MIN_CONTEXT", 1024))
    return max(512, env_int("B1_LOCALAI_MANAGED_FIT_MIN_CONTEXT", 1024))


def localai_extra_config_lines(prompt_family: str | None) -> list[str]:
    if prompt_family != "laguna-xs-2.1":
        return []
    return [
        f"flash_attention: {yaml_scalar(os.getenv('B1_LOCALAI_MANAGED_LAGUNA_XS_FLASH_ATTENTION', 'on'))}",
        f"cache_type_k: {yaml_scalar(os.getenv('B1_LOCALAI_MANAGED_LAGUNA_XS_CACHE_TYPE_K', 'q4_0'))}",
        f"cache_type_v: {yaml_scalar(os.getenv('B1_LOCALAI_MANAGED_LAGUNA_XS_CACHE_TYPE_V', 'q4_0'))}",
    ]


def localai_extra_options(prompt_family: str | None) -> list[str]:
    if prompt_family != "laguna-xs-2.1":
        return []
    options = [
        f"cache_ram:{max(0, env_int('B1_LOCALAI_MANAGED_LAGUNA_XS_CACHE_RAM_MIB', 1024))}",
        f"--ubatch-size:{max(1, env_int('B1_LOCALAI_MANAGED_LAGUNA_XS_UBATCH_SIZE', 256))}",
    ]
    if env_bool("B1_LOCALAI_MANAGED_LAGUNA_XS_CPU_MOE", True):
        options.append("--cpu-moe")
    return options


def managed_config_for_manifest(manifest: dict[str, Any], view_root: Path, models_root: Path) -> tuple[str, str] | None:
    model_id = manifest.get("id")
    version = manifest.get("version")
    runtimes = manifest.get("runtimes")
    if not isinstance(model_id, str) or not SAFE_COMPONENT_RE.match(model_id):
        return None
    if not isinstance(version, str) or not version.strip():
        return None
    if not isinstance(runtimes, list) or "localai" not in runtimes:
        return None
    relative_file = localai_gguf_file(manifest, view_root)
    if relative_file is None:
        return None
    try:
        relative_view = view_root.resolve(strict=True).relative_to(models_root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    model_path = PurePosixPath(*relative_view.parts, *PurePosixPath(relative_file).parts).as_posix()
    config_name = f"{MANAGED_PREFIX}{config_safe(model_id)}-{config_safe(version)}.yaml"
    backend = os.getenv("B1_LOCALAI_MANAGED_LLAMA_BACKEND", "llama").strip() or "llama"
    threads = max(1, env_int("B1_LOCALAI_MANAGED_THREADS", 4))
    prompt_family = localai_prompt_family(manifest, view_root, relative_file)
    batch = localai_batch_size(prompt_family)
    fit_target = localai_fit_target_mib(prompt_family)
    fit_ctx = localai_fit_ctx(prompt_family)
    context_size = manifest_context_size(manifest, relative_file, prompt_family)
    gpu_layers = managed_gpu_layers(manifest, view_root, relative_file, prompt_family)
    mmap = env_bool("B1_LOCALAI_MANAGED_MMAP", True)
    mmlock = env_bool("B1_LOCALAI_MANAGED_MMLOCK", False)
    low_vram = env_bool("B1_LOCALAI_MANAGED_LOW_VRAM", True)
    f16 = env_bool("B1_LOCALAI_MANAGED_F16", gpu_layers != 0)
    parameter_defaults = localai_parameter_defaults(prompt_family)
    use_jinja = "false" if prompt_family == "gemma4" else "true"
    parameter_lines = [f"  model: {yaml_scalar(model_path)}"]
    for key, value in parameter_defaults.items():
        parameter_lines.append(f"  {key}: {value}")
    body = "\n".join(
        [
            MANAGED_MARKER,
            f"# model_ref: {model_id}@{version}",
            f"# prompt_family: {prompt_family}" if prompt_family else "# prompt_family: tokenizer-template",
            f"name: {yaml_scalar(model_id)}",
            f"backend: {yaml_scalar(backend)}",
            "parameters:",
            *parameter_lines,
            f"context_size: {context_size}",
            f"threads: {threads}",
            f"batch: {batch}",
            f"gpu_layers: {gpu_layers}",
            f"mmap: {str(mmap).lower()}",
            f"mmlock: {str(mmlock).lower()}",
            f"low_vram: {str(low_vram).lower()}",
            f"f16: {str(f16).lower()}",
            *localai_extra_config_lines(prompt_family),
            "options:",
            "  - 'parallel:1'",
            "  - 'fit_params:true'",
            f"  - 'fit_target:{fit_target}'",
            f"  - 'fit_ctx:{fit_ctx}'",
            f"  - 'use_jinja:{use_jinja}'",
            "  - 'warmup:false'",
            *(f"  - {yaml_scalar(option)}" for option in localai_extra_options(prompt_family)),
            *localai_template_lines(prompt_family),
            *localai_generation_control_lines(prompt_family),
            "",
        ]
    )
    return config_name, body


def discover_managed_configs(models_root: Path) -> dict[str, str]:
    configs: dict[str, str] = {}
    if not models_root.is_dir() or models_root.is_symlink():
        return configs
    for manifest_path in sorted(models_root.glob("*/*/manifest.b1.json")):
        if ".staging" in manifest_path.parts:
            continue
        view_root = manifest_path.parent
        manifest = load_runtime_manifest(manifest_path)
        if manifest is None:
            continue
        config = managed_config_for_manifest(manifest, view_root, models_root)
        if config is None:
            continue
        name, body = config
        configs[name] = body
    return configs


def combined_models_config(configs: dict[str, str]) -> str:
    if not configs:
        return f"{MANAGED_MARKER}\n[]\n"
    lines = [
        MANAGED_MARKER,
        "# Combined LocalAI models-config file generated from B1 runtime views.",
    ]
    for name in sorted(configs):
        first_payload_line = True
        for line in configs[name].splitlines():
            if not line or line.startswith("#"):
                continue
            if first_payload_line:
                lines.append(f"- {line}")
                first_payload_line = False
            else:
                lines.append(f"  {line}")
    lines.append("")
    return "\n".join(lines)


def existing_managed_configs(config_dir: Path) -> list[Path]:
    if not config_dir.is_dir() or config_dir.is_symlink():
        return []
    configs: list[Path] = []
    for path in sorted(config_dir.glob(f"{MANAGED_PREFIX}*.yaml")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError):
            continue
        if first_line == MANAGED_MARKER:
            configs.append(path)
    return configs


def write_atomic(path: Path, content: str) -> bool:
    if path.is_file() and not path.is_symlink():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".partial", delete=False) as handle:
        tmp_path = Path(handle.name)
        handle.write(content)
    tmp_path.chmod(0o644)
    tmp_path.replace(path)
    return True


def sync_managed_configs(models_path: str | Path | None = None, config_path: str | Path | None = None) -> dict[str, Any]:
    models_root = Path(models_path or os.getenv("LOCALAI_MODELS_PATH", "/srv/b1-ai-hub/models")).resolve(strict=False)
    config_dir = Path(config_path or os.getenv("LOCALAI_CONFIG_DIR", "/srv/b1-ai-hub/localai/configuration")).resolve(strict=False)
    model_configs = discover_managed_configs(models_root)
    desired = {**model_configs, MANAGED_MODELS_CONFIG: combined_models_config(model_configs)}
    config_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    unchanged: list[str] = []
    for name, body in desired.items():
        if write_atomic(config_dir / name, body):
            written.append(name)
        else:
            unchanged.append(name)
    removed: list[str] = []
    for path in existing_managed_configs(config_dir):
        if path.name in desired:
            continue
        path.unlink()
        removed.append(path.name)
    return {
        "status": "ok",
        "models_path": str(models_root),
        "config_path": str(config_dir),
        "models_config_file": str(config_dir / MANAGED_MODELS_CONFIG),
        "managed_count": len(model_configs),
        "written": written,
        "unchanged": unchanged,
        "removed": removed,
    }


def main() -> int:
    result = sync_managed_configs()
    print(json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
