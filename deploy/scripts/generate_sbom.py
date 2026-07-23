#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS_FILES = (
    "services/control-plane/requirements.txt",
    "services/runtime-agent/requirements.txt",
    "services/artifact-server/requirements.txt",
    "services/audio-cpu/requirements.txt",
    "services/mock-runtime/requirements.txt",
    "deploy/voicebox/constraints.txt",
)
PACKAGE_LOCKS = (
    "web/control-center/package-lock.json",
    "web/media-studio/package-lock.json",
)
DOCKERFILES = (
    "services/control-plane/Dockerfile",
    "services/runtime-agent/Dockerfile",
    "services/artifact-server/Dockerfile",
    "services/audio-cpu/Dockerfile",
    "services/mock-runtime/Dockerfile",
    "web/control-center/Dockerfile",
    "web/media-studio/Dockerfile",
    "deploy/open-webui/Dockerfile",
    "deploy/comfyui/Dockerfile",
    "deploy/voicebox/Dockerfile",
    "integrations/b1-model-client/Dockerfile",
)
PINNED_RELEASE_ARTIFACTS = (
    {
        "type": "application",
        "name": "rhasspy/piper",
        "version": "2023.11.14-2",
        "purl": "pkg:github/rhasspy/piper@2023.11.14-2",
        "properties": [
            {"name": "b1:source", "value": "services/audio-cpu/Dockerfile"},
            {"name": "b1:install_arg", "value": "B1_INSTALL_PIPER"},
            {"name": "b1:asset", "value": "piper_linux_x86_64.tar.gz"},
            {
                "name": "b1:download_url",
                "value": "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz",
            },
            {
                "name": "b1:sha256",
                "value": "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992",
            },
        ],
    },
    {
        "type": "application",
        "name": "Comfy-Org/ComfyUI",
        "version": "v0.3.77",
        "purl": "pkg:github/Comfy-Org/ComfyUI@v0.3.77",
        "properties": [
            {"name": "b1:source", "value": "deploy/comfyui/Dockerfile"},
            {"name": "b1:commit", "value": "59afc3984868289f808d02fa5cd180edfb2de240"},
            {"name": "b1:download_url", "value": "https://github.com/Comfy-Org/ComfyUI/archive/59afc3984868289f808d02fa5cd180edfb2de240.tar.gz"},
            {"name": "b1:sha256", "value": "0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1"},
        ],
    },
    {
        "type": "application",
        "name": "jamiepine/voicebox",
        "version": "v0.5.0",
        "purl": "pkg:github/jamiepine/voicebox@v0.5.0",
        "properties": [
            {"name": "b1:source", "value": "deploy/voicebox/Dockerfile"},
            {"name": "b1:commit", "value": "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d"},
            {"name": "b1:download_url", "value": "https://github.com/jamiepine/voicebox/archive/2bcb98d1a8b6fe05e15fbc1559e3085669e4035d.tar.gz"},
            {"name": "b1:sha256", "value": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083"},
            {"name": "b1:qwen3_tts_commit", "value": "022e286b98fbec7e1e916cb940cdf532cd9f488e"},
            {"name": "b1:linacodec_commit", "value": "c0ae7c7285e121475c27592cfbb600624b714290"},
            {"name": "b1:luxtts_commit", "value": "28ae6a61151684fffc9d1a7aa15eafa02286fe0b"},
        ],
    },
)
PYTHON_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[([A-Za-z0-9_,.-]+)\])?==([A-Za-z0-9_.!+:-]+)\s*(?:#.*)?$")
FROM_LINE = re.compile(r"^\s*FROM\s+([^\s]+)(?:\s+AS\s+([A-Za-z0-9_.-]+))?\s*$", re.IGNORECASE)


def normalize_name(value: str) -> str:
    return value.strip().replace("_", "-").lower()


def component_key(component: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(component.get("type", "")),
        str(component.get("group", "")),
        str(component.get("name", "")),
        str(component.get("version", "")),
    )


def property_record(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def parse_requirements(path: Path, relative_path: str) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    if not path.is_file():
        return components
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = PYTHON_REQUIREMENT.match(stripped)
        if not match:
            components.append(
                {
                    "type": "library",
                    "name": stripped,
                    "version": "unparsed",
                    "scope": "required",
                    "properties": [
                        property_record("b1:source", relative_path),
                        property_record("b1:line", str(line_number)),
                        property_record("b1:parser_warning", "requirement is not name==version pinned"),
                    ],
                }
            )
            continue
        name, extras, version = match.groups()
        normalized = normalize_name(name)
        properties = [property_record("b1:source", relative_path), property_record("b1:ecosystem", "python")]
        if extras:
            properties.append(property_record("b1:extras", extras))
        components.append(
            {
                "type": "library",
                "name": normalized,
                "version": version,
                "scope": "required",
                "purl": f"pkg:pypi/{normalized}@{version}",
                "properties": properties,
            }
        )
    return components


def parse_package_lock(path: Path, relative_path: str) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    if not path.is_file():
        return components
    payload = json.loads(path.read_text(encoding="utf-8"))
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        return components
    for package_path, record in packages.items():
        if not package_path or not isinstance(record, dict):
            continue
        name = record.get("name")
        if not isinstance(name, str):
            name = npm_name_from_package_path(package_path)
        version = record.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        scope = "optional" if record.get("optional") else "required"
        if record.get("dev"):
            scope = "excluded"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "scope": scope,
                "purl": f"pkg:npm/{name}@{version}",
                "properties": [
                    property_record("b1:source", relative_path),
                    property_record("b1:ecosystem", "npm"),
                    property_record("b1:package_path", package_path),
                ],
            }
        )
    return components


def npm_name_from_package_path(package_path: str) -> str | None:
    prefix = "node_modules/"
    if not package_path.startswith(prefix):
        return None
    name = package_path[len(prefix) :]
    if name.startswith("@"):
        parts = name.split("/", 2)
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None
    return name.split("/", 1)[0]


def split_image_reference(reference: str) -> tuple[str, str]:
    without_digest = reference.split("@", 1)[0]
    last_slash = without_digest.rfind("/")
    last_colon = without_digest.rfind(":")
    if last_colon > last_slash:
        return without_digest[:last_colon], without_digest[last_colon + 1 :]
    return without_digest, "unversioned"


def parse_dockerfile(path: Path, relative_path: str) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    if not path.is_file():
        return components
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = FROM_LINE.match(line)
        if not match:
            continue
        image, stage = match.groups()
        name, version = split_image_reference(image)
        properties = [
            property_record("b1:source", relative_path),
            property_record("b1:line", str(line_number)),
            property_record("b1:image_reference", image),
        ]
        if stage:
            properties.append(property_record("b1:stage", stage))
        components.append(
            {
                "type": "container",
                "name": name,
                "version": version,
                "purl": f"pkg:docker/{name}@{version}",
                "properties": properties,
            }
        )
    return components


def collect_components(root: Path) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for relative in REQUIREMENTS_FILES:
        components.extend(parse_requirements(root / relative, relative))
    for relative in PACKAGE_LOCKS:
        components.extend(parse_package_lock(root / relative, relative))
    for relative in DOCKERFILES:
        components.extend(parse_dockerfile(root / relative, relative))
    components.extend(PINNED_RELEASE_ARTIFACTS)

    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for component in components:
        key = component_key(component)
        if key in unique:
            existing_properties = unique[key].setdefault("properties", [])
            existing_properties.extend(component.get("properties", []))
            continue
        unique[key] = component
    return [unique[key] for key in sorted(unique)]


def build_sbom(root: Path) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "component": {
                "type": "application",
                "name": "b1-ai-hub",
                "version": "0.1.0",
            },
            "tools": [
                {
                    "vendor": "B1 AI Hub",
                    "name": "deploy/scripts/generate_sbom.py",
                    "version": "1",
                }
            ],
        },
        "components": collect_components(root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a lightweight CycloneDX SBOM for B1 AI Hub source dependencies.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    sbom = build_sbom(args.root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output} with {len(sbom['components'])} component(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
