# Make managed web fetching faster without losing source evidence

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept current while work proceeds. It follows the repaired ExecPlan skill reference at `/home/mordred/.codex/skills/execplan-skill/references/PLANS.md`.

## Purpose / Big Picture

Ordinary OpenWebUI chats now give every B1-managed model access to `web_search` and `web_fetch`, but a DeepSeek fetch of the Python JSON documentation expanded the final inference input to roughly 6,400 tokens and took about 132 seconds to process. After this change, fetched pages will be reduced to query-relevant passages, repeated fetches will reuse a bounded in-process extraction cache, and the controller will avoid duplicating fetched content in its prompt. A normal DeepSeek request with no explicit tool field must still search, fetch an official page, answer correctly, and release the managed GPU lease.

## Progress

- [x] (2026-08-13 19:57Z) Repair and validate the ExecPlan skill reference required by `SKILL.md`.
- [x] (2026-08-13 19:58Z) Trace the live controller tool loop and establish the pre-change DeepSeek evidence.
- [x] (2026-08-13 20:11Z) Implement query-aware HTML passage extraction, progressive result sizing, and bounded extraction caching.
- [x] (2026-08-13 20:12Z) Remove duplicate tool-result injection and compact the fallback instruction.
- [x] (2026-08-13 20:14Z) Add and run 62 focused unit tests in the built control-plane image.
- [x] (2026-08-13 20:15Z) Build and deploy only the B1 control-plane container, preserving OpenWebUI and the loaded worker model.
- [x] (2026-08-13 20:17Z) Repeat a no-override DeepSeek search-plus-fetch request and compare input tokens and latency with the baseline.
- [ ] Run a representative second-model semantic check later; switching the single P40 away from the user's currently loaded DeepSeek session is intentionally deferred.

## Surprises & Discoveries

- Observation: The controller inserts each fetched result twice: once as a standard `role=tool` message and again as JSON inside `b1_text_tool_result_instruction`, with the duplicate capped at 6,000 characters.
  Evidence: `services/control-plane/app/main.py` appends both messages for every executed call; the baseline final DeepSeek turn processed about 6,398 prompt tokens.

- Observation: DeepSeek can emit both `web_search` and `web_fetch` in one assistant response even though the fallback prompt asks for one call per message.
  Evidence: The default-policy test returned both native tool calls and completed correctly in one reported tool iteration.

## Decision Log

- Decision: Preserve the hard 12,000-character policy ceiling but use a 5,000-character default when the model does not request a size.
  Rationale: This enables progressive fetching without removing the administrator's escape hatch for unusually detailed pages.
  Date/Author: 2026-08-13 / Codex

- Decision: Rank passages with deterministic lexical overlap rather than adding an embedding model or external service.
  Rationale: The controller remains reproducible, CPU-cheap, offline-capable, and does not introduce another model lease or network dependency.
  Date/Author: 2026-08-13 / Codex

- Decision: Cache cleaned page text in memory with a small TTL and entry limit, never raw credentials or private URLs.
  Rationale: It eliminates repeated download and HTML parsing within a controller lifetime while retaining existing URL safety enforcement and simple restart-based invalidation.
  Date/Author: 2026-08-13 / Codex

## Outcomes & Retrospective

The managed OpenWebUI path now provides a smaller tool prompt, query-ranked page passages, a 5,000-character progressive default with the 12,000-character hard ceiling retained, and a bounded extraction cache. All 62 focused tests pass. A normal `deepseek-main` request with no explicit `b1_tools` field returned the correct official Python documentation title and `JSONEncoder`, with `X-B1-Tools: web_search,web_fetch`. The fetched-page turn fell from roughly 6,398 prompt tokens to 2,472 (61% fewer) and the comparable end-to-end request fell from about 198 seconds to 126.9 seconds (36% faster). The worker released its slot, GPU utilization returned to zero, and the loaded server retained 20,457 MiB VRAM. A cross-model reliability sweep remains appropriate, but was not allowed to evict the user's active DeepSeek model merely for qualification.

## Context and Orientation

The production repository is `/home/mordred/localAIcentre` on `ai.b1.germering`. `services/control-plane/app/model_tools.py` defines safe URL validation, tool schemas, page download, and HTML-to-text extraction. `services/control-plane/app/main.py` injects tool definitions, runs the multi-turn tool loop, and forwards each model turn through B1's scheduler. `tests/unit/test_model_tools.py` covers both modules. The P40 worker is separate and must remain managed by B1; this change does not modify its Compose project or directly expose its inference server.

A passage is a short text block derived from headings and paragraphs. Query-aware extraction scores passages using normalized word overlap with the question supplied to `web_fetch`. When no query is provided, the extractor returns the start of the cleaned page under the smaller default budget.

## Plan of Work

In `model_tools.py`, retain structural breaks during HTML cleanup, split the cleaned text into bounded passages, rank those passages against a new optional `query` argument, and return selected passages with explicit extraction metadata. Add an ordered, TTL-bounded in-process cache keyed by normalized URL. Keep all existing destination validation and redirect behavior.

In `main.py`, make the fallback tool instruction concise and stop reinserting full JSON results through a second system message. Keep a short instruction after the standard tool message so weak models still know to answer or request more evidence.

Add focused tests for passage ranking, default result size, explicit expansion up to the hard cap, cache reuse, tool schema, and non-duplication. Then rebuild and recreate only `control-plane`, verify health, and perform a normal OpenWebUI-client request with no `b1_tools` field.

## Concrete Steps

Work from `/home/mordred/localAIcentre` on the controller. Run:

    sudo -n docker compose -f compose.yaml -f compose.production-localai.yaml \
      -f compose.production-comfyui.yaml -f compose.production-voicebox.yaml \
      build control-plane

Then recreate only the controller and confirm health. Run focused tests before deployment and issue the acceptance request from the OpenWebUI container afterward.

## Validation and Acceptance

Focused unit tests must pass. A request authenticated as `client_open_webui_internal`, selecting `deepseek-main`, and omitting `b1_tools` must return HTTP 200 with `X-B1-Tools: web_search,web_fetch`. Worker logs must show tool calls and a final grounded answer. The final-turn prompt input and wall time must be materially below the prior approximately 6,400-token and 132-second baseline for the same documentation page. GPU utilization must return to idle and the server process must remain loaded.

## Idempotence and Recovery

Code edits and tests are repeatable. The cache is memory-only and disappears safely on restart. Before deployment, retain the current controller image ID. If health or acceptance fails, restore the previous source files or retag the prior image and recreate only `control-plane`; do not restart OpenWebUI or worker services unnecessarily.

## Artifacts and Notes

Baseline evidence: the explicit test completed with HTTP 200, `X-B1-Tools: web_search,web_fetch`, and two iterations. Its final DeepSeek turn processed 5,960 new prompt tokens in 121 seconds and took about 136 seconds total. The default-policy test processed roughly 6,398 input tokens before final generation.

## Interfaces and Dependencies

`web_fetch` retains required `url` and gains optional `query` plus optional `max_chars`. `ModelToolRegistry.web_fetch()` remains asynchronous and returns the existing keys plus extraction and cache metadata. No new Python dependency is added; use only the standard library and existing `httpx`.

Revision note (2026-08-13): Initial plan created after tracing the production duplication and measuring the DeepSeek baseline.

Revision note (2026-08-13): Updated after implementation and live acceptance with measured token, latency, cache, correctness, and lease-release evidence.
