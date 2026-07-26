#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class AcceptanceReportClientError(RuntimeError):
    pass


DEFAULT_OUTPUT_NAME = "operator-handoff-report-response.json"
MAX_JSON_BYTES = 64 * 1024


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def validate_api_base(raw: str, *, allow_insecure_http: bool = False) -> str:
    value = raw.strip()
    if not value:
        raise AcceptanceReportClientError("B1_ACCEPTANCE_API_BASE is required")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AcceptanceReportClientError("acceptance API base must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise AcceptanceReportClientError("acceptance API base must not contain credentials")
    if parsed.fragment or parsed.query:
        raise AcceptanceReportClientError("acceptance API base must not include a query string or fragment")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise AcceptanceReportClientError(
            "plain HTTP acceptance API base is disabled; set B1_ACCEPTANCE_ALLOW_INSECURE_HTTP=true only for labelled development dry runs"
        )
    return value.rstrip("/")


def safe_regular_file(path: Path, label: str, *, private: bool = False) -> Path:
    candidate = path.expanduser()
    try:
        stat = candidate.lstat()
    except FileNotFoundError as exc:
        raise AcceptanceReportClientError(f"{label} does not exist: {candidate}") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise AcceptanceReportClientError(f"{label} must be a regular file, not a symlink or special file: {candidate}")
    if private and os.name == "posix" and stat.st_mode & 0o077:
        raise AcceptanceReportClientError(f"{label} must be private to the current user, for example chmod 0600: {candidate}")
    return candidate


def read_token() -> str:
    token_file = os.getenv("B1_ACCEPTANCE_API_KEY_FILE", "").strip()
    if token_file:
        token_path = safe_regular_file(Path(token_file), "B1_ACCEPTANCE_API_KEY_FILE", private=True)
        token = token_path.read_text(encoding="utf-8").strip()
    else:
        token = os.getenv("B1_ACCEPTANCE_API_KEY", "").strip()
    if not token:
        raise AcceptanceReportClientError("B1_ACCEPTANCE_API_KEY or B1_ACCEPTANCE_API_KEY_FILE is required")
    if token.startswith("github_pat_"):
        raise AcceptanceReportClientError("GitHub personal access tokens are not B1 API keys; create a scoped B1 admin key")
    return token


def read_json_source(raw: str, file_path: str, label: str) -> dict[str, Any]:
    if raw.strip() and file_path.strip():
        raise AcceptanceReportClientError(f"set either {label}_JSON or {label}_FILE, not both")
    if file_path.strip():
        path = safe_regular_file(Path(file_path), f"{label}_FILE", private=True)
        if path.stat().st_size > MAX_JSON_BYTES:
            raise AcceptanceReportClientError(f"{label}_FILE is too large: {path}")
        content = path.read_text(encoding="utf-8")
    elif raw.strip():
        content = raw
        if len(content.encode("utf-8")) > MAX_JSON_BYTES:
            raise AcceptanceReportClientError(f"{label}_JSON is too large")
    else:
        return {}
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AcceptanceReportClientError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceReportClientError(f"{label} must be a JSON object")
    return value


def operator_evidence_payload() -> tuple[dict[str, bool], dict[str, str]]:
    evidence = read_json_source(
        os.getenv("B1_ACCEPTANCE_REPORT_OPERATOR_EVIDENCE_JSON", ""),
        os.getenv("B1_ACCEPTANCE_REPORT_OPERATOR_EVIDENCE_FILE", ""),
        "B1_ACCEPTANCE_REPORT_OPERATOR_EVIDENCE",
    )
    notes = read_json_source(
        os.getenv("B1_ACCEPTANCE_REPORT_OPERATOR_NOTES_JSON", ""),
        os.getenv("B1_ACCEPTANCE_REPORT_OPERATOR_NOTES_FILE", ""),
        "B1_ACCEPTANCE_REPORT_OPERATOR_NOTES",
    )
    if any(not isinstance(key, str) or not isinstance(value, bool) for key, value in evidence.items()):
        raise AcceptanceReportClientError("operator evidence JSON must map string keys to booleans")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in notes.items()):
        raise AcceptanceReportClientError("operator notes JSON must map string keys to strings")
    return evidence, notes


def report_payload() -> dict[str, Any]:
    evidence, notes = operator_evidence_payload()
    return {
        "label": os.getenv("B1_ACCEPTANCE_REPORT_LABEL", "operator handoff").strip() or "operator handoff",
        "notes": os.getenv("B1_ACCEPTANCE_REPORT_NOTES", "").strip(),
        "operator_evidence": evidence,
        "operator_evidence_notes": notes,
    }


def endpoint_for(api_base: str, mode: str) -> str:
    suffix = "/admin/acceptance-reports/preview" if mode == "preview" else "/admin/acceptance-reports"
    return api_base + suffix


def ssl_context_from_env() -> ssl.SSLContext | None:
    ca_file = os.getenv("B1_ACCEPTANCE_CA_FILE", "").strip()
    if not ca_file:
        return None
    ca_path = safe_regular_file(Path(ca_file), "B1_ACCEPTANCE_CA_FILE")
    return ssl.create_default_context(cafile=str(ca_path))


def post_report(mode: str, api_base: str, token: str, payload: dict[str, Any], context: ssl.SSLContext | None) -> dict[str, Any]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint_for(api_base, mode),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            context=context,
            timeout=float(os.getenv("B1_ACCEPTANCE_REPORT_TIMEOUT_SECONDS", "120")),
        ) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AcceptanceReportClientError(f"acceptance report API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AcceptanceReportClientError(f"acceptance report API request failed: {exc.reason}") from exc
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AcceptanceReportClientError("acceptance report API returned non-JSON response") from exc
    if not isinstance(decoded, dict):
        raise AcceptanceReportClientError("acceptance report API returned a non-object JSON response")
    return decoded


def output_path_for(mode: str) -> Path | None:
    explicit = os.getenv("B1_ACCEPTANCE_REPORT_OUTPUT", "").strip()
    if explicit:
        return Path(explicit)
    backup_root = os.getenv("B1_BACKUP_ROOT", "").strip()
    if not backup_root:
        return None
    name = "acceptance-report-preview.json" if mode == "preview" else DEFAULT_OUTPUT_NAME
    return Path(backup_root) / "acceptance" / name


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    target = path.expanduser()
    parent = target.parent
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise AcceptanceReportClientError(f"output must be a regular file, not a symlink or special file: {target}")
    for ancestor in [parent, *parent.parents]:
        if ancestor.exists() and ancestor.is_symlink():
            raise AcceptanceReportClientError(f"output parent directory must not be a symlink: {ancestor}")
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def should_require_ready(mode: str) -> bool:
    return env_bool("B1_ACCEPTANCE_REPORT_REQUIRE_READY", mode == "create")


def summarize_response(mode: str, response: dict[str, Any], output: Path | None) -> str:
    summary = response.get("summary") if isinstance(response.get("summary"), dict) else {}
    report = response.get("report") if isinstance(response.get("report"), dict) else {}
    report_id = summary.get("id") or report.get("id") or ""
    lines = [
        f"mode={mode}",
        f"status={summary.get('status', report.get('status', 'unknown'))}",
        f"operator_handoff_ready={summary.get('operator_handoff_ready', report.get('operator_handoff_ready', False))}",
    ]
    if report_id:
        lines.append(f"report_id={report_id}")
    blockers = report.get("acceptance_blockers") if isinstance(report.get("acceptance_blockers"), list) else []
    if blockers:
        lines.append(f"acceptance_blockers={len(blockers)}")
    if output is not None:
        lines.append(f"output={output}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or preview a B1 AI Hub acceptance handoff report.")
    parser.add_argument("mode", choices=("preview", "create"), help="preview avoids writes/audit; create stores the durable report")
    parser.add_argument("--api-base", default=os.getenv("B1_ACCEPTANCE_API_BASE", ""), help="B1 API base URL, defaults to B1_ACCEPTANCE_API_BASE")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        api_base = validate_api_base(args.api_base, allow_insecure_http=env_bool("B1_ACCEPTANCE_ALLOW_INSECURE_HTTP"))
        token = read_token()
        payload = report_payload()
        response = post_report(args.mode, api_base, token, payload, ssl_context_from_env())
        output = output_path_for(args.mode)
        if output is not None:
            write_private_json(output, response)
        print(summarize_response(args.mode, response, output))
        summary = response.get("summary") if isinstance(response.get("summary"), dict) else {}
        if should_require_ready(args.mode) and not bool(summary.get("operator_handoff_ready")):
            return 3
        return 0
    except AcceptanceReportClientError as exc:
        print(f"acceptance report error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
