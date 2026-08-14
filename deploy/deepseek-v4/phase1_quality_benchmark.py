#!/usr/bin/env python3
"""Run exactly the DeepSeek quality Phase-1A 3x4x3 public-API matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA = "b1.deepseek_quality.phase1a.run.v1"
MODEL_ALIAS = "deepseek-quality"
MODEL_ID = "b1-unsloth-deepseek-v4-flash-0731-quality"
MODEL_REVISION = "fbbb5b93fb787c21338159b0af3318bb3f4d9768"
CLEAN_SAMPLING: dict[str, int | float] = {
    "temperature": 1.0, "top_p": 1.0, "top_k": 0, "min_p": 0.0,
    "typical_p": 1.0, "repeat_penalty": 1.0,
    "presence_penalty": 0.0, "frequency_penalty": 0.0,
}
LEGACY_SAMPLING: dict[str, int | float] = {
    "temperature": 0.8, "top_p": 0.95, "top_k": 40, "min_p": 0.05,
    "typical_p": 1.0, "repeat_penalty": 1.0,
    "presence_penalty": 0.0, "frequency_penalty": 0.0,
}
CONFIGURATIONS = {
    "A": {"reasoning_level": "legacy", "budget": 384, "sampling": LEGACY_SAMPLING},
    "B": {"reasoning_level": "normal", "budget": 2048, "sampling": CLEAN_SAMPLING},
    "C": {"reasoning_level": "quality", "budget": 4096, "sampling": CLEAN_SAMPLING},
    "D": {"reasoning_level": "deep", "budget": 8192, "sampling": CLEAN_SAMPLING},
}
PROMPTS = {
    "concurrency_external_side_effects": """Review this code:
```python
def buy(stock, quantity):
    if stock >= quantity:
        stock -= quantity
        charge_customer()
        return stock
    return stock
```
Two threads may call `buy()` concurrently for the same stock value.

Requirements:
* Stock must never become negative.
* A customer must never be charged twice because of a retry.
* If payment fails, stock must not be permanently lost.
* Do not assume `charge_customer()` is transactional.
* Do not invent capabilities it was not stated to have.
* If a requirement cannot be guaranteed, state exactly what additional capability is required.
* Challenge any requirement or assumption that is logically insufficient.

Identify the problems and propose the smallest robust design. Your answer must also check whether its sample code really implements its prose, distinguish retry ambiguity from payment-result ambiguity, and distinguish the explicit requirements from any stronger crash-consistency requirement. Keep the visible answer under 300 words.""",
    "debugging_self_consistency": """Audit this proposed fix:
```python
def remove_matches(items, predicate):
    # Proposed fix: iterate backward so every matching item is removed safely.
    for index in range(len(items) - 1, -1, -1):
        if predicate(items[index]):
            return items.pop(index)
    return items
```

The required contract is: mutate `items` in place by removing every element for which `predicate(element)` is true, preserve the relative order of retained elements, and return the same list object. `predicate` is allowed to raise; in that case the function must leave `items` unchanged. Use no second list proportional to input size.

Identify every contradiction between the comment, code, and contract. Give the smallest correct implementation if the contract is satisfiable. If some constraints conflict or require an unstated capability, say so precisely. Check your proposed code against empty input, adjacent matches, all matches, no matches, and an exception after earlier successful predicate calls. Keep the visible answer under 300 words.""",
    "architecture_requirement_consistency": """A service must send an order-confirmation email through an external provider. The only stated provider operation is `send(address, body)`, which may succeed and then time out before returning. The service has a transactional SQL database, but the provider is not part of that transaction. The process may crash at any instruction.

Requirements:
* never send a duplicate email;
* never lose an email;
* the database must eventually say exactly whether the provider sent it;
* use the smallest sufficient design;
* do not assume provider idempotency, status lookup, cancellation, or transactional messaging;
* do not silently strengthen the requested failure model.

Explain which guarantees are achievable with only the stated capabilities, identify the exact missing capability or capabilities for the others, and give the smallest honest design. Separate requirements from assumptions and avoid proposing machinery that does not close the ambiguity window. Keep the visible answer under 300 words.""",
}


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def read_dotenv(path: Path, key: str) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip("'\"")
    return ""


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def wait_for_load_temperature(maximum_c: int = 65) -> None:
    while True:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            text=True,
            timeout=10,
        )
        temperature = int(output.strip().splitlines()[0])
        if temperature <= maximum_c:
            return
        time.sleep(10)


def run_one(api: str, key: str, context: ssl.SSLContext, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        api.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Accept": "text/event-stream", "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    first_token_at: float | None = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
    timings: dict[str, Any] = {}
    done = False
    with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
        status = response.status
        for raw_line in response:
            line = raw_line.strip()
            if not line.startswith(b"data:"):
                continue
            value = line[5:].strip()
            if value == b"[DONE]":
                done = True
                break
            try:
                item = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(item.get("usage"), dict):
                usage = item["usage"]
            if isinstance(item.get("timings"), dict):
                timings = item["timings"]
            choices = item.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            content = delta.get("content") if isinstance(delta.get("content"), str) else ""
            reasoning = delta.get("reasoning_content") if isinstance(delta.get("reasoning_content"), str) else ""
            if first_token_at is None and (content or reasoning):
                first_token_at = time.monotonic()
            content_parts.append(content)
            reasoning_parts.append(reasoning)
    completed = time.monotonic()
    if status != 200 or not done:
        raise RuntimeError(f"incomplete public response: HTTP {status}, done={done}")
    return {
        "http_status": status,
        "started_at": utc_now(),
        "wall_clock_seconds": completed - started,
        "ttft_seconds": (first_token_at or completed) - started,
        "response_text": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts),
        "usage": usage,
        "timings": timings,
    }


def terminal_rows(output: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not output.is_dir():
        return result
    for path in output.glob("runs/*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        key = str(row.get("run_key") or "")
        if key in result:
            raise RuntimeError(f"duplicate run key: {key}")
        if row.get("terminal_status") in {"completed", "failed"} or row.get("status") in {200, "completed", "failed"}:
            result[key] = row
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="https://api.ai.b1.germering")
    parser.add_argument("--dotenv", type=Path, default=Path("/srv/DialectiCore/.env"))
    parser.add_argument("--dotenv-key", default="B1_API_KEY")
    parser.add_argument("--ca-file", type=Path, default=Path("/srv/b1-p40-worker/secrets/trust/b1-ai-hub-caddy-root.crt"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=2400)
    args = parser.parse_args()
    api_key = read_dotenv(args.dotenv, args.dotenv_key)
    if not api_key:
        parser.error("B1 API key is unavailable")
    context = ssl.create_default_context(cafile=str(args.ca_file))
    existing = terminal_rows(args.output)
    expected_keys = {
        f"{test_id}:{configuration}:{repetition}"
        for test_id in PROMPTS for configuration in CONFIGURATIONS for repetition in range(1, 4)
    }
    if set(existing) - expected_keys:
        raise RuntimeError("output directory contains unexpected benchmark run keys")
    for test_id, prompt in PROMPTS.items():
        for configuration, config in CONFIGURATIONS.items():
            for repetition in range(1, 4):
                run_key = f"{test_id}:{configuration}:{repetition}"
                payload: dict[str, Any] = {
                    "model": MODEL_ALIAS,
                    "messages": [{"role": "user", "content": prompt}],
                    "reasoning_budget_tokens": config["budget"],
                    **config["sampling"],
                    "max_tokens": 9216,
                    "seed": 1000 + list(PROMPTS).index(test_id) * 100 + ord(configuration) * 3 + repetition,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "cache_prompt": False,
                }
                digest = request_hash(payload)
                if run_key in existing:
                    if existing[run_key].get("request_sha256") != digest:
                        raise RuntimeError(f"terminal run payload changed: {run_key}")
                    continue
                print(json.dumps({"event": "starting", "run_key": run_key}), flush=True)
                wait_for_load_temperature()
                try:
                    response = run_one(args.api, api_key, context, payload, args.timeout_seconds)
                except (OSError, urllib.error.HTTPError, RuntimeError) as exc:
                    print(json.dumps({"event": "failed", "run_key": run_key, "error": str(exc)}), flush=True)
                    row = {
                        "schema_version": SCHEMA,
                        "terminal_status": "failed",
                        "run_key": run_key,
                        "request_sha256": digest,
                        "model_alias": MODEL_ALIAS,
                        "model_internal_id": MODEL_ID,
                        "model_revision": MODEL_REVISION,
                        "test_id": test_id,
                        "repetition": repetition,
                        "configuration": configuration,
                        "reasoning_level": config["reasoning_level"],
                        "reasoning_budget_parameter": "reasoning_budget_tokens",
                        "reasoning_budget_tokens_sent": config["budget"],
                        "sampling": config["sampling"],
                        "failure": "incomplete_public_response",
                        "failure_detail": str(exc),
                        "response_text": None,
                        "reasoning_content": None,
                        "usage": None,
                        "timings": None,
                        "evaluator_scores": None,
                        "evaluator_comments": "Terminal runtime failure; no complete answer was returned.",
                        "critical_error_flags": ["runtime_stability_failure"],
                    }
                    atomic_json(args.output / "runs" / f"{test_id}-{configuration}-{repetition}.json", row)
                    existing[run_key] = row
                    continue
                usage = response.get("usage") or {}
                row = {
                    "schema_version": SCHEMA,
                    "terminal_status": "completed",
                    "run_key": run_key,
                    "request_sha256": digest,
                    "model_alias": MODEL_ALIAS,
                    "model_internal_id": MODEL_ID,
                    "model_revision": MODEL_REVISION,
                    "test_id": test_id,
                    "repetition": repetition,
                    "configuration": configuration,
                    "reasoning_level": config["reasoning_level"],
                    "reasoning_budget_parameter": "reasoning_budget_tokens",
                    "reasoning_budget_tokens_sent": config["budget"],
                    "sampling": config["sampling"],
                    "prompt_token_count": usage.get("prompt_tokens"),
                    "reasoning_token_count": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                    "reasoning_token_count_exposed": isinstance((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"), int),
                    "output_token_count": usage.get("completion_tokens"),
                    "prompt_tokens_per_second": (response.get("timings") or {}).get("prompt_per_second"),
                    "generation_tokens_per_second": (response.get("timings") or {}).get("predicted_per_second"),
                    **response,
                    "evaluator_scores": None,
                    "evaluator_comments": None,
                    "critical_error_flags": None,
                }
                atomic_json(args.output / "runs" / f"{test_id}-{configuration}-{repetition}.json", row)
                print(json.dumps({"event": "completed", "run_key": run_key, "seconds": row["wall_clock_seconds"]}), flush=True)
    rows = terminal_rows(args.output)
    if set(rows) != expected_keys or len(rows) != 36:
        raise RuntimeError(f"Phase-1A terminal run count is {len(rows)}, expected 36")
    jsonl = "".join(json.dumps(rows[key], sort_keys=True) + "\n" for key in sorted(rows))
    (args.output / "benchmark-runs.raw.jsonl").write_text(jsonl, encoding="utf-8")
    print(json.dumps({"status": "completed", "runs": 36, "phase1b_started": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
