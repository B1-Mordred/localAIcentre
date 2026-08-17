#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDED_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".wav",
    ".mp3",
    ".mp4",
    ".webm",
    ".sqlite",
    ".db",
}
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}\b")),
    ("openai-token", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b")),
    ("b1-api-token", re.compile(r"\bb1(?:k|adm|rt)_[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,}\b")),
    (
        "generic-env-assignment",
        re.compile(
            r"(?i)^\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|API_KEY|TOKEN)[A-Z0-9_]*"
            r"\s*=\s*[A-Za-z0-9_./+=-]{24,}\s*(?:#.*)?$"
        ),
    ),
    (
        "generic-quoted-assignment",
        re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*=\s*['\"][A-Za-z0-9_./+=-]{24,}['\"]"),
    ),
]
ALLOWLIST_PATTERNS = [
    re.compile(r"(?i)_FILE\s*="),
    re.compile(r"<redacted>", re.IGNORECASE),
    re.compile(r"example", re.IGNORECASE),
    re.compile(r"placeholder", re.IGNORECASE),
    re.compile(r"changeme", re.IGNORECASE),
    re.compile(r"dummy", re.IGNORECASE),
    re.compile(r"\ba{32,64}\b"),
    re.compile(r"\b0{32,64}\b"),
]


def is_binary(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\x00" in sample


def should_scan(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file() and not is_binary(path)


def line_allowed(line: str) -> bool:
    return any(pattern.search(line) for pattern in ALLOWLIST_PATTERNS)


def scan_file(path: Path, root: Path) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return findings
    for line_number, line in enumerate(lines, start=1):
        if line_allowed(line):
            continue
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    {
                        "rule": name,
                        "path": str(path.relative_to(root)),
                        "line": line_number,
                        "excerpt": pattern.sub("<secret>", line.strip())[:240],
                    }
                )
    return findings


def iter_scan_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if should_scan(path, root):
            yield path


def scan(root: Path) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path in iter_scan_files(root):
        findings.extend(scan_file(path, root))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan B1 AI Hub source files for accidentally committed secrets.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true", help="Emit JSON findings.")
    args = parser.parse_args()

    root = args.root.resolve()
    findings = scan(root)
    if args.json:
        print(json.dumps({"findings": findings, "count": len(findings)}, indent=2, sort_keys=True))
    elif findings:
        for finding in findings:
            print(f"{finding['path']}:{finding['line']}: {finding['rule']}: {finding['excerpt']}")
    if findings:
        return 1
    print("secret scan OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
