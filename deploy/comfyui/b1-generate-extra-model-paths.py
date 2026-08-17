from __future__ import annotations

import os
from pathlib import Path


MODEL_FOLDER_MAP = {
    "checkpoints": ("diffusion/checkpoints",),
    "diffusion_models": ("diffusion/diffusion_models", "diffusion/unet", "video"),
    "text_encoders": ("diffusion/text_encoders", "vision/text_encoders"),
    "clip_vision": ("vision/clip_vision",),
    "configs": ("diffusion/configs",),
    "controlnet": ("diffusion/controlnet",),
    "embeddings": ("embeddings",),
    "loras": ("diffusion/loras",),
    "upscale_models": ("diffusion/upscale_models",),
    "vae": ("diffusion/vae",),
    "audio_encoders": ("audio_encoders",),
    "model_patches": ("model_patches",),
}


def has_regular_files(path: Path) -> bool:
    try:
        return any(item.is_file() and not item.is_symlink() for item in path.iterdir())
    except OSError:
        return False


def installed_model_roots(model_root: Path) -> list[Path]:
    roots: list[Path] = []
    try:
        model_dirs = sorted(item for item in model_root.iterdir() if item.is_dir() and not item.is_symlink() and not item.name.startswith("."))
    except OSError:
        return roots
    for model_dir in model_dirs:
        try:
            version_dirs = sorted(item for item in model_dir.iterdir() if item.is_dir() and not item.is_symlink() and not item.name.startswith("."))
        except OSError:
            continue
        for version_dir in version_dirs:
            marker = version_dir / "manifest.b1.json"
            if marker.is_file() and not marker.is_symlink():
                roots.append(version_dir)
    return roots


def yaml_list(paths: list[str]) -> str:
    if len(paths) == 1:
        return paths[0]
    return "|\n" + "\n".join(f"    {path}" for path in paths)


def generate(model_root: Path) -> str:
    model_root = model_root.resolve(strict=False)
    roots = installed_model_roots(model_root)
    lines = ["b1:", f"  base_path: {model_root}", "  is_default: true"]
    for key, defaults in MODEL_FOLDER_MAP.items():
        paths = list(defaults)
        for root in roots:
            try:
                relative_root = root.relative_to(model_root).as_posix()
            except ValueError:
                continue
            for suffix in defaults:
                candidate = root / suffix
                if candidate.is_dir() and not candidate.is_symlink() and has_regular_files(candidate):
                    value = f"{relative_root}/{suffix}"
                    if value not in paths:
                        paths.append(value)
        rendered = yaml_list(paths)
        if rendered.startswith("|\n"):
            lines.append(f"  {key}: {rendered}")
        else:
            lines.append(f"  {key}: {rendered}")
    return "\n".join(lines) + "\n"


def main() -> None:
    model_root = Path(os.environ.get("B1_COMFYUI_MODEL_ROOT", "/srv/b1-ai-hub/models"))
    default_output = Path(os.environ.get("B1_COMFYUI_TEMP_DIR", "/srv/b1-ai-hub/comfyui/temp")) / "generated-extra_model_paths.yaml"
    output = Path(os.environ.get("B1_COMFYUI_GENERATED_EXTRA_MODEL_PATHS", str(default_output)))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(generate(model_root), encoding="utf-8")
    temporary.replace(output)


if __name__ == "__main__":
    main()
