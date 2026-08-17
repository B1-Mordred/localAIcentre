# Preserve managed reasoning across OpenWebUI tool loops

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

Reasoning-capable B1 models must keep their structured reasoning visible in OpenWebUI. Laguna's pinned template cannot preserve reasoning when native tool definitions or tool-role transcripts are present, so Laguna alone uses B1's managed web loop and a tool-free final evidence synthesis; DeepSeek and all other models retain native OpenWebUI tools. Success is observable in a fresh saved OpenWebUI Laguna response containing a native `reasoning` output item, with automatic web access still handled through the authenticated B1 path.

## Progress

- [x] (2026-08-16 23:34Z) Reproduced and isolated direct versus tool-continuation behavior without changing the running services.
- [x] (2026-08-16 23:42Z) Added a fail-closed, capability-gated OpenWebUI patch and four focused unit tests; nine related OpenWebUI tests pass.
- [x] (2026-08-16 23:56Z) Built the digest-pinned managed OpenWebUI image; the dependency-complete backend quality container passed all 1,634 tests (2 skipped), and the installed patched middleware compiles.
- [x] (2026-08-17 00:42Z) Deployed the managed OpenWebUI and control-plane changes; both became healthy with the existing persistent data mount, and the legacy OpenWebUI was not restarted.
- [x] (2026-08-17 01:02Z) Corrected the live capability-shape mismatch and verified a saved Laguna response contains a native `reasoning` item with 1,166 characters.
- [x] (2026-08-17 01:02Z) Preserved DeepSeek/native-tool and generic-provider branches through focused regression tests and verified their worker containers were not restarted.
- [x] (2026-08-17 01:03Z) Committed and pushed the complete change set through `d43b504532fcbcf01d06d955f3b54e79508b42bc`; recorded exact live images and rollback points.

## Surprises & Discoveries

- Observation: Laguna and B1 already emit the same structured field OpenWebUI accepts for DeepSeek.
  Evidence: A public ordinary Laguna stream produced 160 `delta.reasoning_content` chunks, and a first-turn tool request produced 28 reasoning chunks plus structured `tool_calls`.
- Observation: Laguna's post-tool continuation can legitimately answer without a new reasoning block.
  Evidence: A controlled assistant-tool-result continuation produced 17 content chunks and no reasoning chunk, so losing the first turn's reasoning leaves nothing for OpenWebUI to display.
- Observation: The saved OpenWebUI Laguna tool loop has no reasoning output item, while recent DeepSeek output has an explicit reasoning item before its message.
  Evidence: Read-only SQLite inspection showed Laguna output types `message`, `function_call`, and `function_call_output`; DeepSeek showed `reasoning`, then `message`.
- Observation: OpenWebUI reconstructs tool-continuation messages with structured reasoning only when its internal provider string is exactly `llama.cpp`.
  Evidence: `get_reasoning_format()` returns `None` for B1's OpenAI-compatible connection even though B1's model record explicitly advertises `capabilities.reasoning_content=true`.
- Observation: The host `make validate` environment is not a valid full-suite runner because its Python 3.14 environment lacks backend dependencies such as httpx, Starlette, AnyIO, and OpenCV.
  Evidence: The repository's dependency-complete quality container subsequently ran the full suite successfully: 1,634 tests passed and 2 were skipped.
- Observation: The live internal model dictionary keeps merged capabilities under `info.meta.capabilities`, in addition to the API response's top-level and nested representations.
  Evidence: A real saved-format Laguna search completed two tool calls but still omitted reasoning after the first deployment; the original gate did not inspect the internal `info.meta` location used throughout OpenWebUI's tool code.
- Observation: Laguna's pinned GLM template emits structured reasoning without tool definitions, but suppresses `reasoning_content` whenever OpenWebUI's native web tools are attached; DeepSeek is unaffected.
  Evidence: The same difficult coding prompt returned 2,438 structured reasoning characters without tools and zero with a native search tool. A managed B1 search loop completed successfully but discarded intermediate Laguna reasoning from its final response.
- Observation: The live `/api/models` representation does not use `info.meta.capabilities`; it exposes Laguna capabilities at the top level and under `openai.capabilities`.
  Evidence: The first saved validation after deployment still stored only a message. The live model record showed `capabilities.chat_template=laguna_glm_thinking_v8` in the top-level and upstream records, while `info.meta.capabilities` was null.
- Observation: Reasoning is conditional model output, not mandatory decoration.
  Evidence: A trivial arithmetic request returned one content character and no reasoning, while the concurrency/payment prompt returned 1,148 raw structured reasoning characters and a saved OpenWebUI `reasoning` item of 1,166 characters.

## Decision Log

- Decision: Gate reasoning reconstruction on the upstream model's explicit `reasoning_content` capability, with the existing `llama.cpp` provider behavior retained as fallback.
  Rationale: This uses B1's existing model contract, covers Laguna and DeepSeek consistently, and leaves non-reasoning models untouched rather than guessing from model names.
  Date/Author: 2026-08-16 / Codex
- Decision: Patch the pinned managed OpenWebUI image at build time using an idempotent, anchor-checked script.
  Rationale: The deployment already carries B1-owned build-time patches; this remains reproducible and fails the image build if upstream v0.11.0 changes incompatibly.
  Date/Author: 2026-08-16 / Codex
- Decision: For the `laguna_glm_thinking_v8` capability only, omit OpenWebUI-native web definitions and use B1's existing managed current-information loop; carry Laguna's intermediate reasoning into that loop's final response.
  Rationale: Ordinary Laguna turns regain structured reasoning, current-information turns retain automatic managed web access and reasoning, while DeepSeek and every other model keep their native streaming tools unchanged.
  Date/Author: 2026-08-17 / Codex
- Decision: When Laguna completes a B1 tool loop without structured reasoning, retry only the final synthesis from the original messages plus bounded, untrusted tool evidence and no tool markers.
  Rationale: This preserves the full managed search loop and restores Laguna's normal thinking template without fabricating reasoning or changing other runtimes. Evidence is explicitly treated as untrusted and capped at 24,000 characters.
  Date/Author: 2026-08-17 / Codex

## Outcomes & Retrospective

Completed. Commit `d43b504532fcbcf01d06d955f3b54e79508b42bc` is pushed and live. The dependency-complete backend gate passed 1,637 tests (2 environment skips), and ten focused OpenWebUI tests passed. Live managed images are control plane `sha256:25f716c9488af5b9d135796000db4f9487c3891d10421d2bb70a1e133eea02f1` and OpenWebUI `sha256:c1c006e5e149f495412163585f0d0a440e41c2eced785e674258dd93a72e8b12`. A saved difficult Laguna turn produced output type `reasoning` with 1,166 characters. DeepSeek and LocalAI routers were neither rebuilt nor restarted, and the legacy OpenWebUI retained its 2026-08-08 start time.

Laguna current-information requests intentionally show B1-managed evidence synthesis rather than OpenWebUI-native function-call cards. A simple live GitHub existence check completed with `X-B1-Tools: web_search,web_fetch`; the model emitted only final content because it judged the request trivial. The system never invents a reasoning item when the model emits none.

## Context and Orientation

The repository is `/opt/b1-ai-hub-source`. `deploy/open-webui/Dockerfile` builds the digest-pinned managed OpenWebUI v0.11.0 image and already applies B1 patch scripts. The upstream function `get_reasoning_format()` lives in `/app/backend/open_webui/utils/middleware.py`; it controls whether output reasoning items are converted back into `reasoning_content` when OpenWebUI calls the model again after a tool result. B1's `/v1/models` records include a top-level `capabilities` object and mark Laguna S and DeepSeek structured reasoning with `reasoning_content: true`.

The live appliance source is `/home/mordred/localAIcentre` on `ai.b1.germering`. The managed service is `b1-ai-hub-open-webui-1` and its persistent data is bind-mounted from `/srv/b1-ai-hub/data/open-webui`. Only that service should be recreated for deployment. The separate legacy `open-webui` container is unrelated and must not be changed.

## Plan of Work

Add `deploy/open-webui/patch_reasoning_tool_loops.py`. It will replace only the pinned upstream `get_reasoning_format()` body, first recognizing an explicit structured-reasoning capability from either the merged model or its retained upstream `openai` record, then preserving the existing provider fallbacks. The patch must be idempotent and fail closed if its exact upstream anchor is absent. Wire it into the Dockerfile and add tests that compile the patched fixture and prove Laguna/DeepSeek-style capability records select `reasoning_content`, Ollama still selects `think_tags`, llama.cpp still selects `reasoning_content`, and an ordinary OpenAI-compatible model still selects `None`.

Build the image from the committed source, run the focused unit tests and repository validation, deploy only the managed OpenWebUI service, and confirm its data mount and health. Exercise the managed OpenWebUI chat path with a temporary authenticated test request that is not saved into the user's conversation history. Verify the stream and, where a disposable chat record is required for timeline inspection, remove it after recording only structural evidence. Run a Laguna tool loop, a DeepSeek reasoning request, and a non-reasoning model request.

## Concrete Steps

From `/opt/b1-ai-hub-source`:

    python3 -m unittest tests.unit.test_open_webui_reasoning_tool_loops
    python3 -m unittest tests.unit.test_open_webui_wrapper tests.unit.test_open_webui_managed_web_defaults
    make validate

On `ai.b1.germering`, after synchronizing the committed branch:

    sudo docker compose build open-webui
    sudo docker compose up -d --no-deps --force-recreate open-webui
    sudo docker inspect b1-ai-hub-open-webui-1

Expected: the service becomes healthy with the existing data and secret mounts, and the installed `get_reasoning_format()` contains the capability gate exactly once.

## Validation and Acceptance

The patch tests must prove idempotence, exact-anchor failure, explicit-capability behavior, and unchanged fallbacks. The repository validation must pass. The live managed OpenWebUI service must remain healthy and keep the same persistent data mount, API endpoint, authentication, web-tool defaults, and cancellation bridge.

A difficult Laguna request must produce and retain a native reasoning output item. Laguna current-information requests must retain automatic authenticated B1 web access without native tool definitions or tool-role markers. DeepSeek must retain its native OpenWebUI tool path. A model without `reasoning_content: true` must not acquire a fabricated reasoning field or altered request format. Existing user chats must remain present, and neither P40 DeepSeek nor LocalAI router may be recreated for this change.

## Idempotence and Recovery

The build-time patch is safe to run repeatedly and must modify its target once. Rebuilding and recreating only `open-webui` is repeatable because `/app/backend/data` is a host bind mount. Before deployment, record the current managed OpenWebUI image ID. Rollback restores that exact image reference or rebuilds the preceding Git commit, then recreates only `open-webui`; no database migration is introduced.

## Artifacts and Notes

Baseline structural evidence from chat `a599fcd7-472e-4faf-a977-0bb2d72edee4` was inspected without printing prompt or response text. It contains a completed Laguna tool loop with no reasoning item. Baseline public probes used the existing scheduler-aware B1 API and did not bypass the worker gateway.

## Interfaces and Dependencies

No new Python dependency is required. The patch relies on the existing B1 `/v1/models` capability field `reasoning_content: true`, OpenWebUI's merged model dictionary, and its existing `convert_output_to_messages(..., reasoning_format=...)` contract. The implementation must not change model weights, templates, inference settings, B1 tool schemas, authentication, network exposure, GPU scheduling, or any non-OpenWebUI runtime.

Plan created 2026-08-16 after direct SSE, tool-continuation, and saved-output inspection isolated the provider-versus-capability mismatch.

Plan updated 2026-08-16 after the capability-gated build-time patch and focused tests were implemented.

Plan updated 2026-08-16 after the exact OpenWebUI image build and dependency-complete repository quality gate passed.

Plan updated 2026-08-17 after the first live tool loop exposed the internal `info.meta.capabilities` representation and the gate was extended without changing its explicit-capability requirement.

Plan updated 2026-08-17 after controlled native-tool/no-tool comparisons isolated Laguna's template interaction and selected the B1-managed fallback.

Plan updated 2026-08-17 after the live top-level capability shape was added to the routing gate and the saved OpenWebUI reasoning item passed end-to-end validation.
