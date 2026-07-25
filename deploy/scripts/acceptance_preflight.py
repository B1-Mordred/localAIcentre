#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from acceptance_env import (  # noqa: E402
    DEFAULT_DATA_ROOT,
    EVIDENCE_FILES,
    LIVE_FLAGS,
    default_output_path,
)


PREFLIGHT_FORMAT = "b1-ai-hub-operator-live-acceptance-preflight/v1"
MAX_JSON_FILE_BYTES = 2 * 1024 * 1024
MODEL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
EXPORT_RE = re.compile(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
DEFAULT_EXPR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}$")
VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

REQUIRED_API_KEY_VARS = (
    "B1_AI_HUB_API_KEY",
    "B1_SMOKE_API_KEY",
    "B1_SMOKE_ADMIN_API_KEY",
    "B1_WORKFLOWS_API_KEY",
    "B1_LOCALAI_ACCEPTANCE_API_KEY",
    "B1_GPU_ACCEPTANCE_API_KEY",
    "B1_RESTART_RECONCILIATION_API_KEY",
    "B1_NATIVE_COMFYUI_API_KEY",
    "B1_REMOTE_NODES_API_KEY",
    "B1_SECURITY_API_KEY",
    "B1_VOICEBOX_API_KEY",
    "B1_VOICEBOX_NATIVE_API_KEY",
    "B1_MODELHUB_TOKEN",
)
REQUIRED_URL_VARS = (
    "B1_ACCEPTANCE_API_BASE",
    "B1_AI_HUB_API_BASE",
    "B1_SMOKE_API_BASE",
    "B1_WORKFLOWS_API_BASE",
    "B1_LOCALAI_ACCEPTANCE_API_BASE",
    "B1_GPU_ACCEPTANCE_API_BASE",
    "B1_RESTART_RECONCILIATION_API_BASE",
    "B1_NATIVE_COMFYUI_API_BASE",
    "B1_SECURITY_API_BASE",
    "B1_VOICEBOX_API_BASE",
    "B1_NATIVE_COMFYUI_BASE",
    "B1_SECURITY_COMFY_BASE",
    "B1_VOICEBOX_BASE",
    "B1_MODELHUB_URL",
)
HTTPS_TOKEN_URL_VARS = REQUIRED_URL_VARS
WORKFLOW_JSON_FILES = (
    ("B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE", "ComfyUI GPU switch prompt", "prompt"),
    ("B1_NATIVE_COMFYUI_PROMPT_FILE", "native ComfyUI compatibility prompt", "prompt"),
    ("B1_WORKFLOWS_IMAGE_JOB_FILE", "image generation workflow job", "media_job"),
    ("B1_WORKFLOWS_IMAGE_EDIT_JOB_FILE", "image edit workflow job", "media_job"),
    ("B1_WORKFLOWS_VIDEO_JOB_FILE", "short video workflow job", "media_job"),
)
REQUIRED_FINAL_VALUES = (
    "B1_RESTART_RECONCILIATION_STARTED_AFTER",
    "B1_MODELHUB_SYNC_MODEL",
    "B1_MODELHUB_INFERENCE_ONLY_MODEL",
)
SAFETY_EXPECTED_VALUES = {
    "B1_ACCEPTANCE_ALLOW_INSECURE_HTTP": "false",
    "B1_MODEL_CLIENT_ALLOW_INSECURE_HTTP": "false",
    "B1_AI_HUB_ALLOW_INSECURE_HTTP": "false",
    "B1_SMOKE_ALLOW_PLACEHOLDER": "false",
    "B1_WORKFLOWS_ALLOW_PLACEHOLDER": "false",
    "B1_REMOTE_NODES_ALLOW_PLACEHOLDER": "false",
    "B1_GPU_ACCEPTANCE_ALLOW_RECOVERY_DRY_RUN": "false",
    "B1_GPU_ACCEPTANCE_SKIP_VOICEBOX": "false",
    "B1_LOCALAI_ACCEPTANCE_REQUIRE_PRODUCTION": "true",
    "B1_GPU_ACCEPTANCE_REQUIRE_PRODUCTION": "true",
    "B1_GPU_ACCEPTANCE_ENFORCE_VRAM_RESERVE": "true",
}
PLACEHOLDER_PATTERNS = (
    "REPLACE_WITH_",
    "CHANGEME",
    "CHANGE_ME",
    "TODO:",
)
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


class AcceptancePreflightError(ValueError):
    pass


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.data:
            payload["data"] = self.data
        return payload


@dataclass
class PreflightContext:
    data_root: Path
    env: dict[str, str]
    env_file: Path | None = None
    allow_tiny_comfy_smoke: bool = False


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def normalized_bool(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in TRUE_VALUES:
        return "true"
    if raw in FALSE_VALUES:
        return "false"
    return raw


def is_placeholder_secret(value: str) -> bool:
    raw = value.strip()
    lowered = raw.lower()
    return (
        not raw
        or raw in {"...", "***", "<redacted>"}
        or "replace_with" in lowered
        or "change_me" in lowered
        or "changeme" in lowered
        or lowered.startswith("github_pat_")
    )


def expand_vars(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2) or ""
        return env.get(key, "")

    return VAR_RE.sub(replace, value)


def expand_generated_value(key: str, value: str, env: dict[str, str]) -> str:
    match = DEFAULT_EXPR_RE.fullmatch(value)
    if match:
        source_key = match.group(1)
        default = match.group(2)
        existing = env.get(source_key, "")
        if existing:
            return existing
        return expand_vars(default, env)
    return expand_vars(value, env)


def load_env_exports(path: Path, env: dict[str, str]) -> dict[str, str]:
    if not path.is_file():
        raise AcceptancePreflightError(f"acceptance env file not found: {path}")
    if path.is_symlink():
        raise AcceptancePreflightError(f"refusing to read symlinked acceptance env file: {path}")
    merged = dict(env)
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = EXPORT_RE.match(line)
        if not match:
            raise AcceptancePreflightError(f"{path}:{line_number}: unsupported env line; regenerate with make acceptance-env")
        key, raw_value = match.groups()
        try:
            tokens = shlex.split(f"{key}={raw_value}", posix=True)
        except ValueError as exc:
            raise AcceptancePreflightError(f"{path}:{line_number}: invalid shell quoting") from exc
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise AcceptancePreflightError(f"{path}:{line_number}: unsupported export assignment")
        _, value = tokens[0].split("=", 1)
        merged[key] = expand_generated_value(key, value, merged)
    return merged


def resolved_env_file(data_root: Path, explicit: str) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.getenv("B1_ACCEPTANCE_ENV", "").strip()
    if configured:
        return Path(configured).expanduser()
    default = default_output_path(data_root)
    return default if default.exists() else None


def env_value(ctx: PreflightContext, key: str) -> str:
    return ctx.env.get(key, "").strip()


def ok(name: str, detail: str, **data: Any) -> PreflightCheck:
    return PreflightCheck(name=name, status="ok", detail=detail, data=data)


def warn(name: str, detail: str, **data: Any) -> PreflightCheck:
    return PreflightCheck(name=name, status="warning", detail=detail, data=data)


def fail(name: str, detail: str, **data: Any) -> PreflightCheck:
    return PreflightCheck(name=name, status="fail", detail=detail, data=data)


def check_env_file(ctx: PreflightContext) -> PreflightCheck:
    if ctx.env_file is None:
        return warn("acceptance_env_file", "no acceptance env file was loaded; checking inherited process environment only")
    return ok("acceptance_env_file", "acceptance env file loaded", path=str(ctx.env_file))


def check_live_flags(ctx: PreflightContext) -> PreflightCheck:
    missing = []
    disabled = []
    for key in LIVE_FLAGS:
        raw = env_value(ctx, key)
        if not raw:
            missing.append(key)
        elif not truthy(raw):
            disabled.append(key)
    if missing or disabled:
        return fail("live_flags", "required live acceptance flags are not enabled", missing=missing, disabled=disabled)
    return ok("live_flags", "required live acceptance flags are enabled", count=len(LIVE_FLAGS))


def check_api_keys(ctx: PreflightContext) -> PreflightCheck:
    missing = []
    wrong_type = []
    for key in REQUIRED_API_KEY_VARS:
        value = env_value(ctx, key)
        if is_placeholder_secret(value):
            if value.lower().startswith("github_pat_"):
                wrong_type.append(key)
            else:
                missing.append(key)
    if missing or wrong_type:
        return fail(
            "api_keys",
            "required scoped API keys are missing or unsuitable",
            missing=missing,
            wrong_type=wrong_type,
        )
    return ok("api_keys", "required scoped API-key variables are populated", count=len(REQUIRED_API_KEY_VARS))


def check_safety_gates(ctx: PreflightContext) -> list[PreflightCheck]:
    mismatches = []
    for key, expected in SAFETY_EXPECTED_VALUES.items():
        observed = normalized_bool(ctx.env.get(key))
        if observed != expected:
            mismatches.append({"key": key, "expected": expected, "observed": observed or "<unset>"})
    checks = []
    if mismatches:
        checks.append(fail("handoff_safety_gates", "final handoff safety gates do not match production acceptance policy", mismatches=mismatches))
    else:
        checks.append(ok("handoff_safety_gates", "final handoff safety gates match production acceptance policy"))

    modelhub_accept = normalized_bool(ctx.env.get("B1_MODELHUB_ACCEPT_LICENSES"))
    if modelhub_accept == "true":
        checks.append(ok("modelhub_license_review", "Model Hub license acceptance flag is enabled for the reviewed sync model"))
    else:
        checks.append(
            warn(
                "modelhub_license_review",
                "B1_MODELHUB_ACCEPT_LICENSES is not enabled; the Model Hub sync test will skip if the selected model requires license acceptance",
                observed=modelhub_accept or "<unset>",
            )
        )
    return checks


def check_urls(ctx: PreflightContext) -> PreflightCheck:
    invalid = []
    insecure = []
    allow_insecure = truthy(ctx.env.get("B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"), False)
    for key in REQUIRED_URL_VARS:
        value = env_value(ctx, key)
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            invalid.append({"key": key, "value": value[:120]})
            continue
        if key in HTTPS_TOKEN_URL_VARS and parsed.scheme != "https" and not allow_insecure:
            insecure.append(key)
    if invalid or insecure:
        return fail("urls", "acceptance URLs are invalid or would send credentials over plain HTTP", invalid=invalid, insecure=insecure)
    return ok("urls", "acceptance URLs are syntactically valid and credentialed endpoints use HTTPS", count=len(REQUIRED_URL_VARS))


def check_ca_file(ctx: PreflightContext) -> PreflightCheck:
    ca_path = env_value(ctx, "B1_ACCEPTANCE_CA_FILE")
    if not ca_path:
        return warn("tls_ca_file", "no B1_ACCEPTANCE_CA_FILE configured; relying on the system trust store")
    path = Path(ca_path)
    if path.is_symlink():
        return fail("tls_ca_file", "configured Caddy CA path is a symlink", path=str(path))
    if not path.is_file():
        return fail("tls_ca_file", "configured Caddy CA file does not exist", path=str(path))
    if not os.access(path, os.R_OK):
        return fail("tls_ca_file", "configured Caddy CA file is not readable", path=str(path))
    return ok("tls_ca_file", "configured Caddy CA file is readable", path=str(path))


def writable_output_path(path: Path) -> tuple[bool, str]:
    if path.exists():
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            return False, f"cannot stat existing path: {exc}"
        if stat.S_ISLNK(mode):
            return False, "target path is a symlink"
        if not stat.S_ISREG(mode):
            return False, "target path is not a regular file"
        if not os.access(path, os.W_OK):
            return False, "existing target is not writable"
        return True, "existing file is writable"
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.is_symlink():
        return False, "nearest existing parent is a symlink"
    if not parent.is_dir():
        return False, "nearest existing parent is not a directory"
    if not os.access(parent, os.W_OK | os.X_OK):
        return False, "nearest existing parent is not writable/searchable"
    return True, "parent path can be created"


def check_evidence_outputs(ctx: PreflightContext) -> PreflightCheck:
    invalid = []
    for key in EVIDENCE_FILES:
        raw = env_value(ctx, key)
        if not raw:
            invalid.append({"key": key, "reason": "unset"})
            continue
        ok_path, reason = writable_output_path(Path(raw))
        if not ok_path:
            invalid.append({"key": key, "path": raw, "reason": reason})
    if invalid:
        return fail("evidence_outputs", "one or more evidence output paths are not writable or safe", invalid=invalid)
    return ok("evidence_outputs", "evidence output paths are writable or creatable", count=len(EVIDENCE_FILES))


def load_json_file(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise AcceptancePreflightError("path is a symlink")
    if not path.is_file():
        raise AcceptancePreflightError("file does not exist")
    size = path.stat().st_size
    if size <= 0:
        raise AcceptancePreflightError("file is empty")
    if size > MAX_JSON_FILE_BYTES:
        raise AcceptancePreflightError(f"file is larger than {MAX_JSON_FILE_BYTES} bytes")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AcceptancePreflightError("file must contain a JSON object")
    return payload


def unresolved_placeholders(value: Any, prefix: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            findings.extend(unresolved_placeholders(item, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(unresolved_placeholders(item, f"{prefix}[{index}]"))
    elif isinstance(value, str):
        upper = value.upper()
        if any(pattern in upper for pattern in PLACEHOLDER_PATTERNS):
            findings.append(prefix)
    return findings


def contains_comfy_tiny_smoke_node(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("class_type") == "B1RuntimeTinyImage":
            return True
        return any(contains_comfy_tiny_smoke_node(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_comfy_tiny_smoke_node(item) for item in value)
    return False


def validate_json_shape(payload: dict[str, Any], expected_shape: str) -> list[str]:
    failures = []
    if expected_shape == "prompt":
        prompt = payload.get("prompt")
        if not isinstance(prompt, dict) or not prompt:
            failures.append("missing non-empty prompt object")
    elif expected_shape == "media_job":
        for key in ("modality", "operation", "model", "input"):
            if key not in payload:
                failures.append(f"missing {key}")
        if not isinstance(payload.get("input"), dict):
            failures.append("input must be an object")
    return failures


def check_workflow_files(ctx: PreflightContext) -> PreflightCheck:
    failures = []
    checked = []
    for key, label, shape in WORKFLOW_JSON_FILES:
        raw = env_value(ctx, key)
        if not raw:
            failures.append({"key": key, "label": label, "reason": "unset"})
            continue
        path = Path(raw)
        try:
            payload = load_json_file(path)
        except (OSError, json.JSONDecodeError, AcceptancePreflightError) as exc:
            failures.append({"key": key, "label": label, "path": str(path), "reason": str(exc)})
            continue
        shape_failures = validate_json_shape(payload, shape)
        placeholders = unresolved_placeholders(payload)
        tiny_smoke = contains_comfy_tiny_smoke_node(payload)
        if "PROMPT_FILE" in key and tiny_smoke and not ctx.allow_tiny_comfy_smoke:
            shape_failures.append("tiny B1RuntimeTinyImage smoke prompt is not valid final handoff evidence")
        if placeholders or shape_failures:
            failures.append(
                {
                    "key": key,
                    "label": label,
                    "path": str(path),
                    "shape_failures": shape_failures,
                    "unresolved_placeholder_paths": placeholders[:20],
                }
            )
        checked.append({"key": key, "path": str(path)})
    if failures:
        return fail("workflow_inputs", "workflow/prompt JSON files are missing, unsafe, or still contain operator placeholders", failures=failures)
    return ok("workflow_inputs", "workflow/prompt JSON files are present and edited for handoff", count=len(checked))


def parse_utc_datetime(value: str) -> bool:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.astimezone(UTC) <= datetime.now(tz=UTC)


def check_final_values(ctx: PreflightContext) -> list[PreflightCheck]:
    missing = [key for key in REQUIRED_FINAL_VALUES if not env_value(ctx, key)]
    invalid_models = [
        key
        for key in ("B1_MODELHUB_SYNC_MODEL", "B1_MODELHUB_INFERENCE_ONLY_MODEL")
        if env_value(ctx, key) and not MODEL_REF_RE.fullmatch(env_value(ctx, key))
    ]
    timestamp = env_value(ctx, "B1_RESTART_RECONCILIATION_STARTED_AFTER")
    timestamp_invalid = bool(timestamp and not parse_utc_datetime(timestamp))
    checks = []
    if missing or invalid_models or timestamp_invalid:
        checks.append(
            fail(
                "operator_final_values",
                "operator-supplied final handoff values are missing or invalid",
                missing=missing,
                invalid_model_refs=invalid_models,
                timestamp_invalid=timestamp_invalid,
            )
        )
    else:
        checks.append(ok("operator_final_values", "operator-supplied restart and Model Hub values are populated"))

    browser_cookie = env_value(ctx, "B1_SECURITY_BROWSER_SESSION_COOKIE")
    browser_username = env_value(ctx, "B1_SECURITY_BROWSER_USERNAME")
    browser_password = env_value(ctx, "B1_SECURITY_BROWSER_PASSWORD")
    if browser_cookie or (browser_username and browser_password):
        checks.append(ok("security_browser_auth", "browser-session or username/password input is available for CSRF acceptance"))
    else:
        checks.append(
            fail(
                "security_browser_auth",
                "security acceptance needs B1_SECURITY_BROWSER_SESSION_COOKIE or B1_SECURITY_BROWSER_USERNAME plus B1_SECURITY_BROWSER_PASSWORD",
            )
        )

    voicebox_failures = []
    if truthy(ctx.env.get("B1_VOICEBOX_SKIP_SPEECH"), False) and not env_value(ctx, "B1_VOICEBOX_SPEECH_LIMITATION"):
        voicebox_failures.append("B1_VOICEBOX_SKIP_SPEECH requires B1_VOICEBOX_SPEECH_LIMITATION")
    if truthy(ctx.env.get("B1_VOICEBOX_SKIP_WEBSOCKET"), False) and not env_value(ctx, "B1_VOICEBOX_WEBSOCKET_LIMITATION"):
        voicebox_failures.append("B1_VOICEBOX_SKIP_WEBSOCKET requires B1_VOICEBOX_WEBSOCKET_LIMITATION")
    if voicebox_failures:
        checks.append(fail("voicebox_limitations", "Voicebox skip flags need explicit upstream limitation text", failures=voicebox_failures))
    else:
        checks.append(ok("voicebox_limitations", "Voicebox limitation fields are consistent with skip flags"))
    return checks


def run_preflight(ctx: PreflightContext) -> dict[str, Any]:
    checks: list[PreflightCheck] = [
        check_env_file(ctx),
        check_live_flags(ctx),
        check_api_keys(ctx),
        check_urls(ctx),
        check_ca_file(ctx),
        check_evidence_outputs(ctx),
        check_workflow_files(ctx),
    ]
    checks.extend(check_safety_gates(ctx))
    checks.extend(check_final_values(ctx))
    counts = {
        "ok": sum(1 for check in checks if check.status == "ok"),
        "warning": sum(1 for check in checks if check.status == "warning"),
        "fail": sum(1 for check in checks if check.status == "fail"),
    }
    status = "fail" if counts["fail"] else "warning" if counts["warning"] else "ok"
    return {
        "format": PREFLIGHT_FORMAT,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "status": status,
        "summary": counts,
        "data_root": str(ctx.data_root),
        "env_file": str(ctx.env_file) if ctx.env_file else None,
        "checks": [check.to_dict() for check in checks],
    }


def human_report(report: dict[str, Any]) -> str:
    lines = [
        f"B1 AI Hub live-acceptance preflight: {report['status']}",
        f"data_root: {report['data_root']}",
    ]
    if report.get("env_file"):
        lines.append(f"env_file: {report['env_file']}")
    lines.append(
        "summary: "
        f"{report['summary']['ok']} ok, {report['summary']['warning']} warning, {report['summary']['fail']} fail"
    )
    for check in report["checks"]:
        lines.append(f"- {check['status']}: {check['name']} - {check['detail']}")
        data = check.get("data") or {}
        if check["status"] != "ok" and data:
            lines.append("  " + json.dumps(data, sort_keys=True))
    return "\n".join(lines) + "\n"


def build_context(args: argparse.Namespace) -> PreflightContext:
    data_root = Path(args.data_root).expanduser()
    process_env = dict(os.environ)
    env = dict(process_env)
    env_file = resolved_env_file(data_root, args.env_file)
    if env_file is not None:
        env = load_env_exports(env_file, process_env)
        env.update({key: value for key, value in process_env.items() if key.startswith("B1_")})
    return PreflightContext(
        data_root=data_root,
        env=env,
        env_file=env_file,
        allow_tiny_comfy_smoke=truthy(env.get("B1_ACCEPTANCE_ALLOW_TINY_COMFY_SMOKE"), False),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight B1 AI Hub operator live-acceptance inputs without running live tests.")
    parser.add_argument("--data-root", "--root", dest="data_root", default=os.getenv("B1_DATA_ROOT", DEFAULT_DATA_ROOT))
    parser.add_argument("--env-file", default="", help="sourceable env file generated by make acceptance-env; parsed safely, not executed")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_preflight(build_context(args))
    except AcceptancePreflightError as exc:
        report = {
            "format": PREFLIGHT_FORMAT,
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "status": "fail",
            "summary": {"ok": 0, "warning": 0, "fail": 1},
            "data_root": str(Path(args.data_root).expanduser()),
            "env_file": args.env_file or None,
            "checks": [fail("acceptance_env_file", str(exc)).to_dict()],
        }
    output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else human_report(report)
    stream = sys.stdout if report["status"] != "fail" else sys.stderr
    stream.write(output)
    return 0 if report["status"] != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
