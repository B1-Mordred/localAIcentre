#!/usr/bin/env python3
"""Rewrite only the existing deepseek-quality inference policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


QUALITY_ALIAS = "deepseek-quality"
VALUE_OPTIONS = {
    "--reasoning-budget": "-1",
    "--temp": "1.0",
    "--top-p": "1.0",
    "--top-k": "0",
    "--min-p": "0.0",
    "--typical-p": "1.0",
    "--repeat-penalty": "1.0",
    "--presence-penalty": "0.0",
    "--frequency-penalty": "0.0",
}


def configured_args(arguments: list[Any]) -> list[str]:
    if not all(isinstance(value, str) and value for value in arguments):
        raise ValueError("quality server_args must be non-empty strings")
    result: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option in VALUE_OPTIONS:
            if index + 1 >= len(arguments):
                raise ValueError(f"{option} lacks a value")
            index += 2
            continue
        result.append(option)
        index += 1
    for option, value in VALUE_OPTIONS.items():
        result.extend((option, value))
    return result


def configure(document: dict[str, Any]) -> dict[str, Any]:
    profiles = document.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("profile document lacks profiles")
    matches = [
        (index, profile)
        for index, profile in enumerate(profiles)
        if isinstance(profile, dict) and QUALITY_ALIAS in (profile.get("aliases") or [])
    ]
    if len(matches) != 1:
        raise ValueError("profile document must contain exactly one existing deepseek-quality")
    index, profile = matches[0]
    if profile.get("model_id") != "b1-unsloth-deepseek-v4-flash-0731-quality":
        raise ValueError("deepseek-quality model identity is unexpected")
    updated_profile = dict(profile)
    updated_profile["server_args"] = configured_args(list(profile.get("server_args") or []))
    updated_profiles = list(profiles)
    updated_profiles[index] = updated_profile
    updated = dict(document)
    updated["profiles"] = updated_profiles
    for before, after in zip(profiles, updated_profiles, strict=True):
        if before is profile:
            before_without_args = {key: value for key, value in before.items() if key != "server_args"}
            after_without_args = {key: value for key, value in after.items() if key != "server_args"}
            if before_without_args != after_without_args:
                raise RuntimeError("quality identity or artifacts changed")
        elif before != after:
            raise RuntimeError("non-quality profile changed")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    updated = configure(document)
    args.output.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
