# Qualify DeepSeek Quality Reasoning Budgets

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

## Purpose / Big Picture

The existing `deepseek-quality` alias should stop inheriting a fixed, process-wide reasoning ceiling and should instead use a B1-owned, per-request reasoning policy. After this change, an ordinary `deepseek-quality` chat gets at most 4096 reasoning tokens, callers can select the named `fast`, `normal`, `quality`, or opt-in `deep` levels, and explicitly supplied supported sampling fields still win over the profile-only clean defaults. The model files, llama.cpp build, 32K context, F16 KV cache, Flash Attention, P40 tensor placement, concurrency, lease lifecycle, security boundaries, and every other alias remain unchanged.

Success is demonstrated by unit tests, deterministic template inspection, a live public-API smoke test, and exactly 36 independent Phase-1A responses: three difficult prompts by four budget/sampling configurations by three repetitions. The run must stop after aggregating those results; Phase-1B is only proposed.

## Progress

- [x] (2026-08-13 22:00Z) Inspected the exact pinned llama.cpp commit and installed binary. Confirmed `--reasoning-budget -1` is unrestricted and request field `reasoning_budget_tokens` overrides the process default; `thinking_budget_tokens` is an accepted compatibility alias.
- [x] (2026-08-13 22:08Z) Compared the live profiles and B1 chat path. Found `deepseek-quality` capped at 512, `deepseek-main` capped at 384, and no DeepSeek-specific B1 reasoning abstraction.
- [x] (2026-08-13 22:24Z) Added the alias-scoped B1 and worker-boundary request policy, validation, sampling defaults, and unit tests; all control-plane and worker tests pass.
- [x] (2026-08-13 22:33Z) Added an existing-profile-only configurator and resumable benchmark harness without changing placement or isolation.
- [x] (2026-08-13 22:46Z) Deployed only the B1 control plane and managed DeepSeek router/profile changes. Verified the pinned image, effective process, request validation, placement, and security invariants.
- [x] (2026-08-14 02:08Z) Ran and persisted exactly 36 Phase-1A terminal outcomes and scored every answer; Phase-1B was not started.
- [x] (2026-08-14 02:18Z) Aggregated quality and latency, retained 4096 as the production default, verified the five-minute idle unload to 233 MiB VRAM, and documented rollback plus a reduced Phase-1B proposal.

## Surprises & Discoveries

- Observation: the live `deepseek-quality` profile currently uses `--reasoning-budget 512`, not the 384 value used by `deepseek-main` and the requested legacy benchmark A.
  Evidence: `/srv/b1-p40-worker/data/deepseek-profiles.json` on 2026-08-13.
- Observation: the pinned server accepts both numeric fields but treats `reasoning_budget_tokens` as canonical and reads `thinking_budget_tokens` second.
  Evidence: `tools/server/server-common.cpp` at commit `030ebb558a5820b444a8f836ed5cdd46c9b4bd7a`.
- Observation: the existing B1 abstraction named `reasoning_effort` is intentionally GPT-OSS-only and limited to `low`, `medium`, and `high`; reusing its database column would conflate unrelated model-family semantics.
  Evidence: `ChatCompletionRequest`, `gpt_oss_reasoning_effort`, and `inject_gpt_oss_reasoning_effort` in `services/control-plane/app/main.py`.
- Observation: two qualification streams reached the unchanged 88 C managed safety threshold and were terminated by the existing recovery path. They are retained as failed terminal outcomes rather than silently rerun.
  Evidence: run keys `debugging_self_consistency:B:1` and `debugging_self_consistency:C:3`, router logs, and the corresponding immutable run JSON files.
- Observation: llama.cpp exposes separate streamed `reasoning_content`, but this pinned server's usage object does not expose a reasoning-token count. The benchmark therefore records that field as unavailable instead of estimating it.
  Evidence: completed Phase-1A response usage and stream chunks.
- Observation: adding the typed public `reasoning` field changes the generated OpenAPI request schema. The first CI run correctly rejected the stale committed `docs/openapi.json`; regenerating that repository-owned artifact fixed the drift.
  Evidence: GitHub Actions run 31763336390, backend job 94654070278, and `test_committed_openapi_schema_is_current`.

## Decision Log

- Decision: use `--reasoning-budget -1` in the existing quality runtime profile, and always inject the B1 default 4096 at request routing for `deepseek-quality`.
  Rationale: this directly matches the installed implementation and keeps the process unrestricted while the scheduler-facing API owns normal limits.
  Date/Author: 2026-08-13 / Codex.
- Decision: add `reasoning` as the DeepSeek logical request field while preserving raw `reasoning_budget_tokens` and `thinking_budget_tokens` pass-through for existing callers.
  Rationale: `reasoning` matches the requested B1 abstraction without changing GPT-OSS `reasoning_effort`; raw compatibility remains available and is validated.
  Date/Author: 2026-08-13 / Codex.
- Decision: benchmark A by explicitly sending budget 384 and the pinned server's legacy sampling defaults, rather than touching `deepseek-main` or retaining a production legacy profile.
  Rationale: it reproduces the requested control condition while honoring the strict alias isolation requirement.
  Date/Author: 2026-08-13 / Codex.
- Decision: cool to the existing 65 C load threshold before each remaining qualification request and preserve thermal shutdowns as failures.
  Rationale: this removes accumulated-temperature ordering bias without changing model, runtime, thermal, power, or placement settings; retaining failures measures production robustness honestly.
  Date/Author: 2026-08-13 / Codex.

## Outcomes & Retrospective

The controlled matrix supports keeping 4096 as the `deepseek-quality` default. Relative to 2048 it raised the mean expert score from 7.167 to 7.500 and the median from 8 to 9, with a 25.2% average completed-run latency cost. Moving from 4096 to 8192 added only 0.111 mean points, reduced the median to 7, and cost another 20.6% latency, so 8192 remains opt-in only.

Two streams were terminated by the unchanged 88 C managed cutoff and remain zero-scored terminal outcomes. The pinned server did not expose reasoning token counts in usage. Host memory stayed plentiful, but sampled SwapFree declined by about 21 MiB from an already nearly full swap device, so the strict no-swap-out check cannot honestly be marked passed. These limitations are retained in the final report rather than normalized away.

## Context and Orientation

The B1 source repository is `localAIcentre`. Public chat enters `services/control-plane/app/main.py` at `chat_completions`, resolves the catalog alias, removes B1-private fields, then calls a scheduler-aware runtime adapter. The P40 DeepSeek adapter points only to the authenticated `/deepseek` path on the worker gateway.

The managed worker runtime is defined by `/opt/b1-p40-worker/compose.yaml`, `/opt/b1-p40-worker/deploy/deepseek-v4/deepseek_manager.py`, and the read-only profile document `/srv/b1-p40-worker/data/deepseek-profiles.json`. The manager starts the pinned `/usr/local/bin/llama-server` on container loopback port 18000 only after B1's lifecycle hook has acquired the P40 lease. A profile's `server_args` supplies all placement and inference options not owned by the manager.

The source used to build the live profile is `/opt/b1-p40-worker/deploy/deepseek-v4/build_production_profiles.py`. Phase-1 artifacts will be stored under `/srv/b1-p40-worker/reports/deepseek-v4-flash-0731/phase-1-quality` as JSONL plus Markdown, without changing model artifacts.

## Plan of Work

First extend `ChatCompletionRequest` with an alias-agnostic `reasoning` field whose accepted values are the four B1 levels and add small pure functions that activate only when `resolution.public_alias == "deepseek-quality"`. Resolve a named value to 512, 2048, 4096, or 8192; if absent, use 4096. Accept existing numeric `reasoning_budget_tokens` or `thinking_budget_tokens`, reject booleans, negative values, conflicting aliases, or values above 8192, and let an explicit numeric value take precedence for backward compatibility. Inject the canonical backend field and only missing clean sampling fields.

Then modify the managed DeepSeek manager to enforce the same quality-profile request contract at the authenticated worker boundary. This defense in depth prevents a direct authenticated adapter caller from bypassing the upper bound and supplies the quality default if an older B1 control plane omits it. Change only the `deepseek-quality` profile from budget 512 to `-1` and append the clean sampling CLI defaults; assert all placement arguments are identical before and after.

Create a benchmark program containing the exact regression prompt plus two concise adversarial prompts and four explicit request configurations. It records raw responses, usage and timings for each independent run. The evaluator records the ten requested 0-10 dimensions and binary critical flags, then writes per-run JSONL and aggregate JSON/Markdown. Run exactly three repetitions of each of the 12 prompt/config pairs through the public B1 API.

Finally inspect the effective child command, container and network state, lease, GPU/RAM/swap/power/temperature telemetry, template output/log evidence, and unload behavior. Compare configuration aggregates and stop before Phase-1B.

## Concrete Steps

From a clean checkout of the active localAIcentre branch:

    cd /tmp/localAIcentre-deepseek-quality-phase1
    python -m unittest tests.unit.test_sync_inference_scheduler

On the P40 worker, run the manager tests and profile assertions:

    sudo -n python3 /opt/b1-p40-worker/deploy/deepseek-v4/test_deepseek_manager.py
    sudo -n docker compose -f /opt/b1-p40-worker/compose.yaml config

Deploy only the changed B1 control-plane image/service and the changed DeepSeek router/profile, then execute:

    sudo -n python3 /opt/b1-p40-worker/deploy/deepseek-v4/phase1_quality_benchmark.py run

The command is resumable by unique `(test_id, configuration, repetition)` key but must refuse duplicates and must contain exactly 36 terminal outcomes before aggregation. Managed safety failures remain terminal outcomes and score as failures; they are never silently rerun.

## Validation and Acceptance

Unit tests must prove named mappings, default quality, explicit supported-sampling precedence, raw numeric compatibility, conflict rejection, upper-bound rejection, no effect on `deepseek-main`, and worker-boundary enforcement. Template evidence must show the embedded template supplies the expected DeepSeek markers and a thinking start/end pair so the pinned server actually installs the budget sampler.

The live `deepseek-quality` child command must contain the same model, 32768 context, F16 KV, Flash Attention, tensor override, threads, batches, mmap and isolation arguments as before; only its reasoning budget and sampling defaults may differ. A before/after normalized command diff will be stored.

Phase-1A is complete only when 36 independent public-API responses and all requested telemetry/evaluations exist, aggregate statistics are computed, a reasoned default recommendation is recorded, and no Phase-1B inference has run.

## Idempotence and Recovery

Code changes are repeatable and unit tests are read-only. Before replacing the worker profile, manager, or control-plane deployment, save timestamped copies and SHA-256 hashes. Profile publication uses a staged file, mode 0444, and atomic rename. Service recreation targets only the named control-plane or DeepSeek router service.

The benchmark writes one immutable raw response per run and an append-only JSONL index. Restarting skips only rows whose payload hash and terminal success evidence match. A partial run may therefore resume without contaminating another repetition. Rollback restores the exact saved files and recreates only the affected services; model data is untouched.

## Artifacts and Notes

Expected Phase-1 output directory:

    /srv/b1-p40-worker/reports/deepseek-v4-flash-0731/phase-1-quality/
        benchmark-runs.jsonl
        aggregate.json
        summary.md
        template-verification.md
        effective-command.txt
        operational-validation.json
        rollback.md

## Interfaces and Dependencies

The public chat interface adds the optional B1 field:

    reasoning: "fast" | "normal" | "quality" | "deep"

For `deepseek-quality` only, it maps to canonical llama.cpp field `reasoning_budget_tokens` with values 512, 2048, 4096, and 8192. The default is 4096. Existing numeric `reasoning_budget_tokens` and `thinking_budget_tokens` remain accepted through Pydantic's extra-field compatibility but are normalized and bounded for this alias. Other aliases receive no new defaults or translation.

The clean quality-profile sampling defaults are temperature 1.0, top_p 1.0, top_k 0, min_p 0.0, typical_p 1.0, repeat_penalty 1.0, presence_penalty 0.0, and frequency_penalty 0.0. Explicit caller fields override these defaults.
