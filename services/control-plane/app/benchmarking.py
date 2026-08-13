from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


RAW_OUTPUT_RETENTION_DAYS = 14
CAMPAIGN_STATES = {"draft", "queued", "running", "review", "completed", "failed", "cancelled"}
DEVICE_GROUP_BY_RUNTIME = {
    "localai": "b1-gpu",
    "comfyui": "b1-gpu",
    "voicebox": "b1-gpu",
    "lipsync": "b1-gpu",
    "lan-localai-worker": "p40-gpu",
    "lan-deepseek-worker": "p40-gpu",
    "lan-p40-media": "p40-gpu",
}


DEFAULT_PROFILES: list[dict[str, Any]] = [
    {"id": "assistant", "version": "1", "display_name": "Assistant & reasoning", "weights": {"quality": .45, "instruction": .25, "latency": .15, "cost": .05, "reliability": .10}},
    {"id": "character-animation", "version": "1", "display_name": "Character animation", "weights": {"identity": .30, "motion": .25, "temporal": .20, "prompt": .10, "latency": .05, "reliability": .10}},
    {"id": "talking-head", "version": "1", "display_name": "Talking head & lipsync", "weights": {"lip_sync": .35, "identity": .25, "temporal": .15, "visual": .10, "latency": .05, "reliability": .10}},
    {"id": "image-generation", "version": "1", "display_name": "Image generation", "weights": {"visual": .35, "prompt": .25, "identity": .15, "latency": .10, "cost": .05, "reliability": .10}},
]


DEFAULT_SUITES: list[dict[str, Any]] = [
    {"id": "llm-general", "version": "1", "display_name": "LLM General", "modality": "text", "cases": [
        {"id": "instruction-de", "prompt": "Fasse den folgenden Text präzise in drei Punkten zusammen.", "metrics": ["quality", "instruction"]},
        {"id": "structured", "prompt": "Antworte als valides JSON mit den Schlüsseln answer und confidence.", "metrics": ["quality", "instruction"]},
    ]},
    {"id": "character-animation", "version": "1", "display_name": "Character Animation", "modality": "video", "cases": [
        {"id": "identity-motion", "prompt": "Animate the reference character speaking naturally while preserving identity.", "metrics": ["identity", "motion", "temporal", "prompt"]},
    ]},
    {"id": "talking-head", "version": "1", "display_name": "Talking Head / Lipsync", "modality": "video", "cases": [
        {"id": "speech-sync", "prompt": "Create a stable talking-head clip aligned to the supplied speech.", "metrics": ["lip_sync", "identity", "temporal", "visual"]},
    ]},
    {"id": "image-general", "version": "1", "display_name": "Image Generation", "modality": "image", "cases": [
        {"id": "prompt-adherence", "prompt": "Create the described scene with consistent anatomy and composition.", "metrics": ["visual", "prompt", "identity"]},
    ]},
]


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_suite(definition: dict[str, Any]) -> dict[str, Any]:
    suite = dict(definition)
    for key in ("id", "version", "display_name", "modality", "cases"):
        if not suite.get(key):
            raise ValueError(f"suite field {key} is required")
    if not isinstance(suite["cases"], list) or not suite["cases"]:
        raise ValueError("suite must contain at least one case")
    ids = [str(case.get("id") or "") for case in suite["cases"] if isinstance(case, dict)]
    if len(ids) != len(suite["cases"]) or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("suite case ids must be non-empty and unique")
    return suite


def validate_profile(definition: dict[str, Any]) -> dict[str, Any]:
    profile = dict(definition)
    weights = profile.get("weights")
    if not profile.get("id") or not profile.get("version") or not isinstance(weights, dict) or not weights:
        raise ValueError("profile id, version, and non-empty weights are required")
    clean = {str(key): float(value) for key, value in weights.items() if float(value) >= 0}
    total = sum(clean.values())
    if total <= 0:
        raise ValueError("profile weights must sum to more than zero")
    profile["weights"] = {key: value / total for key, value in clean.items()}
    return profile


def normalized_score(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(100.0, number))


def score_candidate(metric_rows: Iterable[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    validated = validate_profile(profile)
    buckets: dict[str, list[float]] = {key: [] for key in validated["weights"]}
    for row in metric_rows:
        metrics = row.get("metrics") if isinstance(row, dict) else None
        if not isinstance(metrics, dict):
            continue
        for metric in buckets:
            score = normalized_score(metrics.get(metric))
            if score is not None:
                buckets[metric].append(score)
    means = {key: sum(values) / len(values) for key, values in buckets.items() if values}
    covered_weight = sum(validated["weights"][key] for key in means)
    total = sum(means[key] * validated["weights"][key] for key in means) / covered_weight if covered_weight else 0.0
    return {"score": round(total, 3), "coverage": round(covered_weight, 4), "metrics": {key: round(value, 3) for key, value in means.items()}}


def bootstrap_ci(values: list[float], seed: str, samples: int = 1000) -> list[float] | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if len(clean) < 2:
        return None
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    means = sorted(sum(rng.choice(clean) for _ in clean) / len(clean) for _ in range(max(100, samples)))
    return [round(means[int(len(means) * .025)], 3), round(means[min(len(means) - 1, int(len(means) * .975))], 3)]


def build_ranking(candidate_rows: dict[str, list[dict[str, Any]]], profile: dict[str, Any], *, final: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate, measurements in candidate_rows.items():
        aggregate = score_candidate(measurements, profile)
        per_case = [score_candidate([item], profile)["score"] for item in measurements]
        rows.append({"candidate": candidate, **aggregate, "confidence_interval": bootstrap_ci(per_case, candidate)})
    rows.sort(key=lambda item: (-item["score"], -item["coverage"], item["candidate"]))
    previous: dict[str, Any] | None = None
    rank = 0
    for index, row in enumerate(rows, 1):
        tied = previous is not None and abs(previous["score"] - row["score"]) < .5
        if not tied:
            rank = index
        row["rank"] = rank
        row["rank_status"] = "final" if final else "provisional"
        previous = row
    return rows


def select_openrouter_judges(account_models: list[dict[str, Any]], public_models: list[dict[str, Any]], *, modality: str = "text", limit: int = 3) -> list[str]:
    available = {str(item.get("id")) for item in account_models if item.get("id")}
    selected: list[str] = []
    for item in public_models:
        model_id = str(item.get("id") or "")
        if not model_id or model_id not in available or "codex" in model_id.lower():
            continue
        architecture = item.get("architecture") or {}
        input_modalities = architecture.get("input_modalities") or ["text"]
        if modality in {"image", "video"} and "image" not in input_modalities:
            continue
        supported = set(item.get("supported_parameters") or [])
        if "response_format" not in supported and "structured_outputs" not in supported:
            continue
        selected.append(model_id)
        if len(selected) >= limit:
            break
    return selected


def raw_expires_at(created_at: datetime | None = None) -> datetime:
    return (created_at or utc_now()) + timedelta(days=RAW_OUTPUT_RETENTION_DAYS)


def expired_raw_paths(campaigns: Iterable[dict[str, Any]], now: datetime | None = None) -> list[Path]:
    current = now or utc_now()
    paths: list[Path] = []
    for campaign in campaigns:
        expiry = campaign.get("raw_expires_at")
        raw_path = campaign.get("raw_path") or (campaign.get("summary") or {}).get("raw_path")
        if isinstance(expiry, datetime) and expiry <= current and isinstance(raw_path, str):
            paths.append(Path(raw_path))
    return paths


def _simple_pdf(lines: list[str]) -> bytes:
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:110] for line in lines[:55]]
    stream = "BT /F1 10 Tf 45 800 Td 13 TL " + " ".join(f"({line}) Tj T*" for line in escaped) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream.encode())} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out)); out.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode())
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def render_report_files(report: dict[str, Any]) -> dict[str, bytes]:
    ranking = report.get("ranking") or []
    title = str(report.get("title") or "B1 Model Benchmark")
    generated = str(report.get("generated_at") or utc_now().isoformat())
    md_lines = [f"# {title}", "", f"Generated: {generated}", "", "| Rank | Candidate | Score | Status |", "|---:|---|---:|---|"]
    for row in ranking: md_lines.append(f"| {row.get('rank')} | {row.get('candidate')} | {row.get('score')} | {row.get('rank_status')} |")
    markdown = "\n".join(md_lines) + "\n"
    csv_io = io.StringIO(); writer = csv.DictWriter(csv_io, fieldnames=["rank", "candidate", "score", "coverage", "rank_status"]); writer.writeheader()
    for row in ranking: writer.writerow({key: row.get(key) for key in writer.fieldnames})
    rows_html = "".join(f"<tr><td>{html.escape(str(row.get('rank')))}</td><td>{html.escape(str(row.get('candidate')))}</td><td>{html.escape(str(row.get('score')))}</td><td>{html.escape(str(row.get('rank_status')))}</td></tr>" for row in ranking)
    html_doc = f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><h1>{html.escape(title)}</h1><p>Generated: {html.escape(generated)}</p><table><thead><tr><th>Rank</th><th>Candidate</th><th>Score</th><th>Status</th></tr></thead><tbody>{rows_html}</tbody></table>"
    files = {
        "report.json": (json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n").encode(),
        "report.md": markdown.encode(), "ranking.csv": csv_io.getvalue().encode(), "report.html": html_doc.encode(),
        "report.pdf": _simple_pdf(md_lines),
    }
    sums = "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sorted(files.items()))
    files["SHA256SUMS"] = sums.encode()
    return files


def write_report_bundle(root: Path, report_id: str, report: dict[str, Any]) -> dict[str, str]:
    target = root / report_id
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("report directory may not be a symlink")
    written: dict[str, str] = {}
    for name, content in render_report_files(report).items():
        path = target / name
        if path.exists() and path.is_symlink():
            raise ValueError("report file may not be a symlink")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content); temporary.replace(path)
        written[name] = str(path)
    return written
