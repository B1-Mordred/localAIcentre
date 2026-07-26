from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


MANAGED_MARKER = "# b1-ai-hub managed localai model config"
MANAGED_PREFIX = "b1-managed-"
MANAGED_MODELS_CONFIG = "b1-managed-models.yaml"
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SAFE_CONFIG_RE = re.compile(r"[^A-Za-z0-9._+-]+")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


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


def manifest_context_size(manifest: dict[str, Any]) -> int:
    estimate = manifest.get("resource_estimate") if isinstance(manifest.get("resource_estimate"), dict) else {}
    declared = estimate.get("context_tokens")
    if not isinstance(declared, int) or declared <= 0:
        declared = env_int("B1_LOCALAI_MANAGED_DEFAULT_CONTEXT_SIZE", 2048)
    maximum = max(512, env_int("B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE", 4096))
    return max(512, min(declared, maximum))


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
    fit_target = max(256, env_int("B1_LOCALAI_MANAGED_FIT_TARGET_MIB", 1024))
    fit_ctx = max(512, env_int("B1_LOCALAI_MANAGED_FIT_MIN_CONTEXT", 1024))
    context_size = manifest_context_size(manifest)
    body = "\n".join(
        [
            MANAGED_MARKER,
            f"# model_ref: {model_id}@{version}",
            f"name: {yaml_scalar(model_id)}",
            f"backend: {yaml_scalar(backend)}",
            "parameters:",
            f"  model: {yaml_scalar(model_path)}",
            "  temperature: 0.2",
            f"context_size: {context_size}",
            f"threads: {threads}",
            "options:",
            "  - 'parallel:1'",
            "  - 'fit_params:true'",
            f"  - 'fit_target:{fit_target}'",
            f"  - 'fit_ctx:{fit_ctx}'",
            "  - 'use_jinja:true'",
            "  - 'warmup:false'",
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
