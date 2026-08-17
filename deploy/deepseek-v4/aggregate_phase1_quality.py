#!/usr/bin/env python3
"""Validate independent Phase-1A judgements and aggregate the 36 outcomes."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "factual_technical_correctness",
    "requirement_coverage",
    "unsupported_assumptions",
    "internal_contradictions",
    "sample_code_correctness",
    "claimed_guarantees_validity",
    "explicit_vs_inferred_requirements",
    "unnecessary_overengineering",
    "clarity_concision",
    "overall_answer_quality",
)
CONFIGURATIONS = ("A", "B", "C", "D")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--evaluations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runs = {}
    for path in args.runs.glob("*.json"):
        row = load_json(path)
        key = row["run_key"]
        if key in runs:
            raise ValueError(f"duplicate run key: {key}")
        runs[key] = row
    evaluations = {item["run_key"]: item for item in load_json(args.evaluations)}
    if len(runs) != 36 or set(runs) != set(evaluations):
        raise ValueError(f"expected matching 36-run sets, got runs={len(runs)} evaluations={len(evaluations)}")

    evaluated: list[dict[str, Any]] = []
    for key in sorted(runs):
        row = dict(runs[key])
        evaluation = evaluations[key]
        scores = evaluation.get("scores")
        if set(scores or {}) != set(DIMENSIONS):
            raise ValueError(f"incomplete dimensions: {key}")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10 for value in scores.values()):
            raise ValueError(f"score outside 0..10: {key}")
        row["evaluator_scores"] = scores
        row["evaluator_comments"] = evaluation["comments"]
        row["critical_error_flags"] = sorted(set(evaluation.get("critical_error_flags") or []))
        row["dimension_mean"] = rounded(statistics.fmean(scores.values()))
        row["qualification_success"] = (
            row.get("terminal_status") == "completed"
            and scores["overall_answer_quality"] >= 8
            and not row["critical_error_flags"]
        )
        evaluated.append(row)

    aggregates: dict[str, Any] = {}
    for configuration in CONFIGURATIONS:
        group = [row for row in evaluated if row["configuration"] == configuration]
        overall = [float(row["evaluator_scores"]["overall_answer_quality"]) for row in group]
        dimension_means = {
            dimension: rounded(mean([float(row["evaluator_scores"][dimension]) for row in group]))
            for dimension in DIMENSIONS
        }
        reasoning_counts = [
            float(row["reasoning_token_count"])
            for row in group if isinstance(row.get("reasoning_token_count"), int)
        ]
        runtimes = [float(row["wall_clock_seconds"]) for row in group if isinstance(row.get("wall_clock_seconds"), (int, float))]
        output_counts = [float(row["output_token_count"]) for row in group if isinstance(row.get("output_token_count"), int)]
        per_test: dict[str, Any] = {}
        for test_id in sorted({row["test_id"] for row in group}):
            test_rows = [row for row in group if row["test_id"] == test_id]
            per_test[test_id] = {
                "successes": sum(bool(row["qualification_success"]) for row in test_rows),
                "runs": len(test_rows),
                "success_rate": rounded(mean([1.0 if row["qualification_success"] else 0.0 for row in test_rows])),
                "mean_overall_score": rounded(mean([float(row["evaluator_scores"]["overall_answer_quality"]) for row in test_rows])),
            }
        aggregates[configuration] = {
            "runs": len(group),
            "completed_responses": sum(row.get("terminal_status") == "completed" for row in group),
            "mean_score": rounded(mean(overall)),
            "median_score": rounded(statistics.median(overall)),
            "score_spread": {
                "minimum": min(overall), "maximum": max(overall),
                "population_standard_deviation": rounded(statistics.pstdev(overall)),
            },
            "critical_error_count": sum(len(row["critical_error_flags"]) for row in group),
            "runs_with_critical_error": sum(bool(row["critical_error_flags"]) for row in group),
            "average_reasoning_token_use": rounded(mean(reasoning_counts)),
            "reasoning_token_count_exposed_runs": len(reasoning_counts),
            "average_runtime_seconds": rounded(mean(runtimes)),
            "average_generation_token_count": rounded(mean(output_counts)),
            "dimension_means": dimension_means,
            "per_test": per_test,
        }

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "benchmark-runs.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in evaluated), encoding="utf-8"
    )
    (args.output / "aggregate.json").write_text(
        json.dumps({"schema_version": "b1.deepseek_quality.phase1a.aggregate.v1", "configurations": aggregates}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
