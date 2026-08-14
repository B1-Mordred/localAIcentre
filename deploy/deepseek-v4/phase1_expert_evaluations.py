#!/usr/bin/env python3
"""Emit the recorded Codex expert review of all 36 Phase-1A outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


DIMENSIONS = (
    "factual_technical_correctness", "requirement_coverage", "unsupported_assumptions",
    "internal_contradictions", "sample_code_correctness", "claimed_guarantees_validity",
    "explicit_vs_inferred_requirements", "unnecessary_overengineering",
    "clarity_concision", "overall_answer_quality",
)


def item(key: str, scores: tuple[float, ...], comment: str, *flags: str) -> dict[str, object]:
    if len(scores) != len(DIMENSIONS):
        raise ValueError(key)
    return {
        "run_key": key,
        "scores": dict(zip(DIMENSIONS, scores, strict=True)),
        "comments": comment,
        "critical_error_flags": list(flags),
    }


EVALUATIONS = [
    item("concurrency_external_side_effects:A:1", (7, 8, 7, 8, 5, 7, 9, 9, 9, 7),
         "Good race, retry, outcome-ambiguity, and crash-scope discussion, but it misses the primitive/shared-state ambiguity and its illustrative stock parameter does not mutate shared state.", "missed_requirement"),
    item("concurrency_external_side_effects:A:2", (6, 7, 7, 5, 4, 5, 9, 8, 9, 5.5),
         "It distinguishes timeout ambiguity and crash consistency, but claims local-integer rollback code implements the prose and never addresses shared mutation.", "missed_requirement", "code_prose_contradiction", "invalid_guarantee"),
    item("concurrency_external_side_effects:A:3", (3, 5, 4, 3, 2, 4, 7, 5, 1, 2),
         "The visible answer starts mid-deliberation, exceeds 300 words by a wide margin, and never reaches a clean self-consistent design.", "response_format_failure", "missed_requirement"),
    item("concurrency_external_side_effects:B:1", (7, 8, 6, 6, 4, 6, 9, 7, 9, 6),
         "The conceptual pending/idempotency design is mostly sound, but the sample uses a local stock integer, calls undefined reconciliation machinery, and returns success while outcome remains ambiguous.", "undefined_identifier", "code_prose_contradiction", "invalid_guarantee"),
    item("concurrency_external_side_effects:B:2", (8, 9, 7, 7, 4, 7, 9, 8, 9, 7),
         "It explicitly notices shared-state ambiguity and states the missing payment capabilities, but its own sample still mutates only a local integer.", "code_prose_contradiction"),
    item("concurrency_external_side_effects:B:3", (8, 9, 7, 5, 3, 6, 9, 8, 9, 6.5),
         "Strong prose on idempotency and unknown outcomes, but the sample uses invalid/non-local shared-state assumptions and restores stock on every exception despite later saying unknown outcomes must be queried.", "code_prose_contradiction", "external_side_effect_error"),
    item("concurrency_external_side_effects:C:1", (9, 9, 8, 9, 9, 8, 9, 9, 10, 9),
         "Best response for this test: uses an actual shared object and lock, qualifies every added payment capability, and cleanly separates ordinary failure from stronger crash consistency."),
    item("concurrency_external_side_effects:C:2", (8, 9, 7, 7, 4, 7, 9, 8, 9, 7),
         "Good ambiguity and capability analysis, but its claimed sample still cannot update shared stock because stock is an integer parameter.", "code_prose_contradiction"),
    item("concurrency_external_side_effects:C:3", (8, 9, 8, 8, 5, 8, 9, 9, 9, 7.5),
         "The guarantees and ambiguous-result handling are careful; the remaining defect is that the illustrative stock parameter is not a shared mutable store.", "code_prose_contradiction"),
    item("concurrency_external_side_effects:D:1", (8, 9, 7, 8, 4, 7, 9, 9, 9, 7),
         "Correct conceptual requirements and crash caveat, but the sample again treats a local integer as shared mutable stock.", "code_prose_contradiction"),
    item("concurrency_external_side_effects:D:2", (6, 8, 7, 6, 4, 5, 9, 9, 9, 6),
         "Concise, but the code has local-stock semantics and the prose overstates idempotency alone as resolving payment-result knowledge.", "code_prose_contradiction", "retry_semantics_error", "invalid_guarantee"),
    item("concurrency_external_side_effects:D:3", (8, 9, 7, 5, 6, 6, 9, 9, 9, 6.5),
         "Uses real shared state and finds both ambiguities, but restores on every exception in code while admitting that a committed-but-lost result must not be restored blindly.", "code_prose_contradiction", "external_side_effect_error"),

    item("debugging_self_consistency:A:1", (9, 9, 9, 9, 8, 9, 9, 10, 9, 9),
         "Correctly finds early return, wrong return type, edge-case failures, and the exception-atomicity versus O(n) storage conflict."),
    item("debugging_self_consistency:A:2", (9, 10, 9, 9, 8, 9, 9, 10, 9, 9),
         "Complete, concise audit with a valid relaxed implementation and an honest impossibility result for the full contract."),
    item("debugging_self_consistency:A:3", (9, 9, 9, 9, 8, 9, 9, 9, 8, 9),
         "Technically correct and complete, though it spends more space restating the prompt than necessary."),
    item("debugging_self_consistency:B:1", (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
         "Managed thermal cutoff ended the SSE stream before a complete answer was returned.", "runtime_stability_failure"),
    item("debugging_self_consistency:B:2", (9, 9, 9, 9, 8, 9, 9, 9, 9, 9),
         "Correctly audits the proposed code and states why arbitrary predicate exceptions require proportional rollback/result state."),
    item("debugging_self_consistency:B:3", (8, 9, 9, 8, 8, 8, 9, 10, 9, 8),
         "Gets the contract and impossibility right; its discussion of a later exception after a match is awkward because the current function returns at the first match."),
    item("debugging_self_consistency:C:1", (8, 9, 8, 8, 8, 8, 9, 9, 9, 8),
         "Strong result, with a slightly imprecise proposed missing guarantee about predicate behavior rather than simply allowing proportional state."),
    item("debugging_self_consistency:C:2", (9, 9, 9, 9, 8, 9, 9, 10, 9, 9),
         "Cleanly distinguishes the valid in-place compaction from the unsatisfied exception-atomicity requirement."),
    item("debugging_self_consistency:C:3", (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
         "Managed thermal cutoff ended the SSE stream before a complete answer was returned.", "runtime_stability_failure"),
    item("debugging_self_consistency:D:1", (9, 9, 9, 9, 9, 9, 9, 9, 9, 9),
         "Complete audit and a correct exception-atomic implementation once the prohibited temporary list is explicitly allowed."),
    item("debugging_self_consistency:D:2", (7, 8, 9, 6, 8, 7, 9, 10, 9, 7),
         "Core impossibility result is correct, but it says the current code can mutate and then reach a later exception even though its return makes that path unreachable."),
    item("debugging_self_consistency:D:3", (7, 8, 9, 6, 8, 7, 9, 9, 9, 7),
         "Correct final conclusion and relaxation, but repeats the same unreachable post-match exception claim about the original early-return code."),

    item("architecture_requirement_consistency:A:1", (8, 9, 8, 9, 8, 8, 9, 9, 9, 8),
         "Correct impossibility and minimal outbox trade-off; the added provider operation could be specified more precisely than idempotency or lookup in the abstract."),
    item("architecture_requirement_consistency:A:2", (7, 9, 8, 6, 8, 6, 9, 8, 8, 7),
         "Mostly correct, but briefly claims exact DB truth can follow from merely not resending an ambiguous attempt, contradicting its own unknown-state analysis.", "invalid_guarantee"),
    item("architecture_requirement_consistency:A:3", (9, 9, 9, 9, 9, 9, 9, 10, 9, 9),
         "Small, honest design and an exact explanation of why local durability cannot close the provider ambiguity."),
    item("architecture_requirement_consistency:B:1", (9, 9, 9, 9, 9, 9, 9, 9, 9, 9),
         "Correctly separates at-most-once, at-least-once, and provider truth, with the exact missing provider-side resolution capability."),
    item("architecture_requirement_consistency:B:2", (9, 9, 9, 9, 9, 9, 9, 10, 9, 9),
         "Precise and minimal; it does not pretend that an outbox alone provides external exactly-once semantics."),
    item("architecture_requirement_consistency:B:3", (10, 10, 10, 10, 9, 10, 10, 10, 10, 10),
         "Best architecture response: explicitly preserves unknown, rejects false DB truth, identifies the exact trade-off, and avoids machinery that cannot close the window."),
    item("architecture_requirement_consistency:C:1", (9, 9, 9, 9, 9, 9, 9, 9, 9, 9),
         "Correct and concise; it treats the combined requirements as exactly-once semantics without silently requiring any stronger delivery model."),
    item("architecture_requirement_consistency:C:2", (9, 9, 9, 9, 9, 9, 9, 10, 9, 9),
         "Clearly states the unresolvable timeout state and gives the two honest weaker policies."),
    item("architecture_requirement_consistency:C:3", (9, 9, 9, 9, 9, 9, 9, 9, 9, 9),
         "Strong smallest-design answer; the sending/unknown states are honest and no local mechanism is overclaimed."),
    item("architecture_requirement_consistency:D:1", (9, 9, 9, 9, 9, 9, 9, 9, 9, 9),
         "Correct impossibility proof and explicit crash window; its strengthened sample clearly labels the added provider capability."),
    item("architecture_requirement_consistency:D:2", (8, 9, 9, 8, 8, 8, 9, 10, 9, 8),
         "Correct trade-off, though naming an ambiguous timeout FAILED is semantically misleading even after the answer explains that it means unknown."),
    item("architecture_requirement_consistency:D:3", (9, 9, 9, 9, 9, 9, 9, 9, 9, 9),
         "Correctly separates the impossible original contract from a minimal idempotent-provider design and states the eventual-success assumption."),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    keys = [entry["run_key"] for entry in EVALUATIONS]
    if len(keys) != 36 or len(set(keys)) != 36:
        raise RuntimeError("expert evaluation set must contain 36 unique runs")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(EVALUATIONS, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
