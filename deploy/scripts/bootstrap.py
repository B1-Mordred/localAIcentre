#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

SECRET_UID = int(os.getenv("B1_CONTAINER_SECRET_UID", "999"))
SECRET_GID = int(os.getenv("B1_CONTAINER_SECRET_GID", "999"))
APP_UID = int(os.getenv("B1_CONTAINER_APP_UID", "999"))
APP_GID = int(os.getenv("B1_CONTAINER_APP_GID", "999"))

DIRS = [
    "data/postgres",
    "data/redis",
    "data/open-webui",
    "data/control-plane",
    "data/prometheus",
    "data/grafana",
    "data/localai/configuration",
    "data/localai/backends",
    "data/localai/data",
    "data/comfyui/input",
    "data/comfyui/user",
    "data/voicebox",
    "data/voicebox/generations",
    "data/voicebox/profiles",
    "data/voicebox/captures",
    "data/voicebox/cache",
    "data/caddy",
    "models/llm",
    "models/vision",
    "models/embeddings",
    "models/diffusion/checkpoints",
    "models/diffusion/diffusion_models",
    "models/diffusion/text_encoders",
    "models/diffusion/vae",
    "models/diffusion/loras",
    "models/diffusion/controlnet",
    "models/diffusion/upscale_models",
    "models/video",
    "models/tts",
    "models/stt",
    "models/blobs",
    "models/blobs/.partial",
    "models/runtime-views/localai",
    "models/runtime-views/comfyui",
    "models/runtime-views/voicebox",
    "models/runtime-views/audio-cpu",
    "models/quarantine/runtime-views",
    "models/quarantine/blobs",
    "workflows",
    "workflows/acceptance",
    "artifacts",
    "artifacts/images",
    "artifacts/audio",
    "artifacts/video",
    "artifacts/voicebox",
    "artifacts/temporary",
    "artifacts/temporary/comfyui-output",
    "artifacts/temporary/comfyui-temp",
    "cache/localai",
    "cache/comfyui",
    "cache/voicebox",
    "secrets",
    "secrets/caddy-certs",
    "logs/caddy",
    "logs/control-plane",
    "logs/runtime-agent",
    "backups",
    "restore-tests",
]

SECRET_FILES = {
    "postgres_password": lambda: secrets.token_urlsafe(32),
    "admin_bootstrap_key": lambda: f"b1adm_{secrets.token_urlsafe(32)}",
    "master_encryption_key": lambda: secrets.token_urlsafe(48),
    "runtime_agent_token": lambda: f"b1rt_{secrets.token_urlsafe(32)}",
    "runtime_control_token": lambda: f"b1rctl_{secrets.token_urlsafe(32)}",
    "artifact_server_token": lambda: f"b1art_{secrets.token_urlsafe(32)}",
    "open_webui_api_key": lambda: f"b1k_{secrets.token_urlsafe(8)}.{secrets.token_urlsafe(32)}",
    "open_webui_secret_key": lambda: secrets.token_urlsafe(48),
    "prometheus_scrape_token": lambda: f"b1prom_{secrets.token_urlsafe(32)}",
    "grafana_admin_password": lambda: secrets.token_urlsafe(32),
}

RUNTIME_AGENT_MTLS_FILES = {
    "ca_key": "runtime_agent_mtls_ca.key",
    "ca_cert": "runtime_agent_mtls_ca.crt",
    "server_key": "runtime_agent_server.key",
    "server_cert": "runtime_agent_server.crt",
    "client_key": "runtime_agent_client.key",
    "client_cert": "runtime_agent_client.crt",
}

APP_WRITABLE_DIRS = [
    "data/open-webui",
    "data/control-plane",
    "data/prometheus",
    "data/grafana",
    "data/localai/configuration",
    "data/localai/backends",
    "data/localai/data",
    "data/comfyui/input",
    "data/comfyui/user",
    "data/voicebox",
    "data/voicebox/generations",
    "data/voicebox/profiles",
    "data/voicebox/captures",
    "data/voicebox/cache",
    "workflows",
    "artifacts",
    "artifacts/images",
    "artifacts/audio",
    "artifacts/video",
    "artifacts/voicebox",
    "artifacts/temporary",
    "artifacts/temporary/comfyui-output",
    "artifacts/temporary/comfyui-temp",
    "cache/localai",
    "cache/comfyui",
    "cache/voicebox",
    "models/blobs",
    "models/blobs/.partial",
    "models/runtime-views",
    "models/runtime-views/localai",
    "models/runtime-views/comfyui",
    "models/runtime-views/voicebox",
    "models/runtime-views/audio-cpu",
    "models/quarantine",
    "models/quarantine/runtime-views",
    "models/quarantine/blobs",
    "logs/control-plane",
    "workflows/acceptance",
    "backups",
    "restore-tests",
]

ACCEPTANCE_TEMPLATE_FILES = (
    "README.md",
    "native-comfyui-smoke-prompt.json",
    "text-to-image-api-prompt.json",
    "image-generation-job.json",
    "image-edit-job.json",
    "short-video-job.json",
)


def secure_secret_path(path: Path, mode: int) -> None:
    try:
        os.chown(path, SECRET_UID, SECRET_GID)
    except PermissionError:
        pass
    path.chmod(mode)


def secure_app_path(path: Path) -> None:
    try:
        os.chown(path, APP_UID, APP_GID)
    except PermissionError:
        pass
    path.chmod(0o775)


def secure_app_file(path: Path) -> None:
    try:
        os.chown(path, APP_UID, APP_GID)
    except PermissionError:
        pass
    path.chmod(0o664)


def write_once(path: Path, value: str) -> bool:
    if path.exists():
        secure_secret_path(path, 0o640)
        return False
    path.write_text(value + "\n", encoding="utf-8")
    secure_secret_path(path, 0o640)
    return True


def run_openssl(args: list[str]) -> None:
    try:
        subprocess.run(
            ["openssl", *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("openssl is required to generate runtime-agent mTLS certificates") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"openssl failed while generating runtime-agent mTLS certificates: {detail}") from exc


def ensure_complete_or_absent(paths: list[Path], label: str) -> bool:
    existing = [path for path in paths if path.exists()]
    if len(existing) == len(paths):
        for path in paths:
            secure_secret_path(path, 0o640)
        return True
    if existing:
        names = ", ".join(path.name for path in existing)
        raise RuntimeError(f"partial runtime-agent mTLS {label} material exists: {names}")
    return False


def generate_runtime_agent_leaf(
    secrets_dir: Path,
    *,
    key_name: str,
    cert_name: str,
    common_name: str,
    extended_key_usage: str,
    san: str | None,
) -> list[str]:
    key_path = secrets_dir / key_name
    cert_path = secrets_dir / cert_name
    if ensure_complete_or_absent([key_path, cert_path], common_name):
        return []

    ca_key = secrets_dir / RUNTIME_AGENT_MTLS_FILES["ca_key"]
    ca_cert = secrets_dir / RUNTIME_AGENT_MTLS_FILES["ca_cert"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        csr_path = tmp_path / f"{common_name}.csr"
        serial_path = tmp_path / f"{common_name}.srl"
        ext_path = tmp_path / f"{common_name}.ext"
        ext_lines = [
            "basicConstraints=CA:FALSE",
            "keyUsage=digitalSignature,keyEncipherment",
            f"extendedKeyUsage={extended_key_usage}",
        ]
        if san:
            ext_lines.append(f"subjectAltName={san}")
        ext_path.write_text("\n".join(ext_lines) + "\n", encoding="utf-8")
        run_openssl(
            [
                "req",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-sha256",
                "-subj",
                f"/CN={common_name}",
                "-keyout",
                str(key_path),
                "-out",
                str(csr_path),
            ]
        )
        run_openssl(
            [
                "x509",
                "-req",
                "-sha256",
                "-days",
                "825",
                "-in",
                str(csr_path),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-CAserial",
                str(serial_path),
                "-out",
                str(cert_path),
                "-extfile",
                str(ext_path),
            ]
        )
    secure_secret_path(key_path, 0o640)
    secure_secret_path(cert_path, 0o640)
    return [key_path.name, cert_path.name]


def ensure_runtime_agent_mtls(root: Path) -> list[str]:
    secrets_dir = root / "secrets"
    created: list[str] = []
    ca_key = secrets_dir / RUNTIME_AGENT_MTLS_FILES["ca_key"]
    ca_cert = secrets_dir / RUNTIME_AGENT_MTLS_FILES["ca_cert"]
    if not ensure_complete_or_absent([ca_key, ca_cert], "CA"):
        run_openssl(
            [
                "req",
                "-x509",
                "-newkey",
                "rsa:4096",
                "-sha256",
                "-days",
                "3650",
                "-nodes",
                "-subj",
                "/CN=B1 AI Hub Runtime Agent CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
                "-keyout",
                str(ca_key),
                "-out",
                str(ca_cert),
            ]
        )
        secure_secret_path(ca_key, 0o640)
        secure_secret_path(ca_cert, 0o640)
        created.extend([ca_key.name, ca_cert.name])

    created.extend(
        generate_runtime_agent_leaf(
            secrets_dir,
            key_name=RUNTIME_AGENT_MTLS_FILES["server_key"],
            cert_name=RUNTIME_AGENT_MTLS_FILES["server_cert"],
            common_name="runtime-agent",
            extended_key_usage="serverAuth",
            san="DNS:runtime-agent,DNS:localhost,IP:127.0.0.1",
        )
    )
    created.extend(
        generate_runtime_agent_leaf(
            secrets_dir,
            key_name=RUNTIME_AGENT_MTLS_FILES["client_key"],
            cert_name=RUNTIME_AGENT_MTLS_FILES["client_cert"],
            common_name="control-plane",
            extended_key_usage="clientAuth",
            san=None,
        )
    )
    return created


def acceptance_template_source() -> Path:
    return Path(os.getenv("B1_ACCEPTANCE_TEMPLATE_SOURCE", "/opt/b1/workflows/acceptance"))


def copy_acceptance_templates(root: Path, source: Path | None = None) -> list[str]:
    source_dir = source or acceptance_template_source()
    if not source_dir.is_dir():
        return []
    target_dir = root / "workflows" / "acceptance"
    target_dir.mkdir(parents=True, exist_ok=True)
    secure_app_path(target_dir)
    created: list[str] = []
    for filename in ACCEPTANCE_TEMPLATE_FILES:
        source_path = source_dir / filename
        target_path = target_dir / filename
        if target_path.exists():
            continue
        if not source_path.is_file() or source_path.is_symlink():
            continue
        try:
            with source_path.open("rb") as source_handle, target_path.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
        except FileExistsError:
            continue
        except Exception:
            if target_path.exists():
                target_path.unlink()
            raise
        secure_app_file(target_path)
        created.append(str(target_path))
    return created


def bootstrap(root: Path) -> dict[str, list[str]]:
    created_dirs: list[str] = []
    created_secrets: list[str] = []
    created_templates: list[str] = []
    for relative in DIRS:
        path = root / relative
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(path))
    for relative in APP_WRITABLE_DIRS:
        secure_app_path(root / relative)
    secure_secret_path(root / "secrets", 0o750)
    secure_secret_path(root / "secrets" / "caddy-certs", 0o750)
    (root / "backups").chmod(0o770)
    (root / "restore-tests").chmod(0o770)

    for filename, generator in SECRET_FILES.items():
        if write_once(root / "secrets" / filename, generator()):
            created_secrets.append(filename)
    created_secrets.extend(ensure_runtime_agent_mtls(root))

    runtime_env = root / "secrets" / "runtime.env"
    if not runtime_env.exists():
        runtime_env.write_text("# Reserved for future non-secret runtime settings.\n", encoding="utf-8")
    secure_secret_path(runtime_env, 0o640)
    created_templates.extend(copy_acceptance_templates(root))

    return {"created_dirs": created_dirs, "created_secrets": created_secrets, "created_templates": created_templates}


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap B1 AI Hub external directories and generated secrets.")
    parser.add_argument("--root", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub"))
    args = parser.parse_args()
    result = bootstrap(Path(args.root).resolve())
    print(f"B1 AI Hub bootstrap complete under {Path(args.root).resolve()}")
    print(f"created directories: {len(result['created_dirs'])}")
    print(f"created secrets: {', '.join(result['created_secrets']) if result['created_secrets'] else 'none'}")
    print(f"created acceptance templates: {len(result['created_templates'])}")


if __name__ == "__main__":
    main()
