# Make managed web access durable for every OpenWebUI model

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

Every model selected in OpenWebUI should receive B1's managed `web_search` and
`web_fetch` tools without the user having to mention the web or enable a tool.
The model should decide whether a tool is useful. Ordinary answers must retain
live streaming, while tool calls must still complete through B1's authenticated,
scheduler-aware tool loop. The change must survive control-plane and OpenWebUI
restarts and must not give model containers unrestricted network access.

## Progress

- [x] (2026-08-14 07:05Z) Inspected the live environment, stored OpenWebUI API-client policy, running images, and request router.
- [x] (2026-08-14 07:29Z) Added default OpenWebUI web capability injection and forwarded only the vetted native `search_web` and `fetch_url` definitions.
- [x] (2026-08-14 07:29Z) Added focused tests for patch idempotence/compilation, trusted-tool filtering, ordinary streaming, and policy reconciliation.
- [x] (2026-08-14 07:48Z) Ran 142 focused tests and the complete 1,626-test unit suite successfully.
- [x] (2026-08-14 07:47Z) Deployed only control-plane and OpenWebUI; a production OpenWebUI request with `features: {}` caused `gpt-oss-20b` to return a real `fetch_url` call for `https://example.com`.
- [ ] Commit, push, and verify CI and deployed health.

## Surprises & Discoveries

- Observation: The live `b1_api_clients` row already stores `["web_search", "web_fetch"]`, and the live Compose environment also sets those values.
  Evidence: PostgreSQL returned the two tools for `client_open_webui_internal` at `2026-08-14 05:51:48Z`.
- Observation: `chat_completions` discards client defaults unless a narrow `CURRENT_INFORMATION_REQUEST` regular expression matches the prompt.
  Evidence: `services/control-plane/app/main.py` explicitly assigns `requested_tools = []` for unmatched OpenWebUI prompts.
- Observation: The running control-plane image contains an older settings fallback of an empty string even though Compose supplies the correct environment value.
  Evidence: `/app/app/settings.py` in the container uses `_words(..., "")`; source uses `web_search,web_fetch`.
- Observation: The first live OpenWebUI image build exposed malformed patch-marker characters in the generated middleware source.
  Evidence: the live source failed compilation; the patcher test now compiles its generated source and the rebuilt image reports `middleware_compile=ok`.
- Observation: Initial live inference was blocked by an unrelated stale NVML handle in the stateless P40 media manager.
  Evidence: media backends had unloaded and host VRAM was 233 MiB, but the manager returned `memory_used_mib:null`; recreating only `media-manager` restored its NVML query to 233 MiB.

## Decision Log

- Decision: Treat managed tools as capabilities offered to the model, not as a prompt classifier decision.
  Rationale: Prompt regexes cannot reliably recognize URLs, indirect requests, or future phrasing; the model's tool choice is the correct decision point.
  Date/Author: 2026-08-14 / Codex
- Decision: Preserve managed egress and API-client policy rather than enabling direct model-container internet.
  Rationale: This keeps URL validation, auditability, authentication, and network isolation intact.
  Date/Author: 2026-08-14 / Codex

## Outcomes & Retrospective

OpenWebUI now adds web search to every browser chat even when its incoming
`features` object is empty. B1 forwards only `search_web` and `fetch_url`; note,
memory, code, and user-defined helpers remain stripped. The production model
selected `fetch_url` without an explicit tool request, proving the default reaches
inference. Direct runtime streaming remains in place because OpenWebUI owns the
native tool continuation. All 1,626 unit tests pass. Commit, CI, and final SHA
verification remain.

## Context and Orientation

`services/control-plane/app/main.py` implements the OpenAI-compatible chat
endpoint and B1's managed tool loop. `requested_b1_model_tools` derives default
tools from the authenticated API-client row. `call_chat_with_b1_tools` executes
model-selected calls against the central tool registry. OpenWebUI authenticates
as `client_open_webui_internal`; its generated key is mounted read-only into the
OpenWebUI container. `services/control-plane/app/settings.py` defines the startup
default and `ensure_open_webui_api_client` reconciles it into PostgreSQL.

## Plan of Work

Replace prompt-regex eligibility with a capability path that always supplies the
authenticated OpenWebUI client's managed tools. Extend the streaming path so the
first model turn can stream normally when it produces text, while buffering only
when a model emits a tool call and then continuing the managed loop. Keep explicit
`b1_tools` semantics and all non-OpenWebUI callers unchanged. Add regression tests
covering requests with no web keywords and source/fetch-style prompts.

## Concrete Steps

Work from `/home/mordred/localAIcentre` on B1 and run:

    python -m unittest tests.unit.test_model_tools tests.unit.test_browser_auth_api
    docker compose build control-plane
    docker compose up -d --no-deps --force-recreate control-plane
    docker compose ps control-plane open-webui tool-search tool-firecrawl

Use the mounted OpenWebUI API key for a request that omits `b1_tools`; successful
evidence is an HTTP 200 response with `X-B1-Tools` naming managed tools and an
answer grounded in search/fetch results.

## Validation and Acceptance

Tests must prove client defaults are applied regardless of prompt wording,
ordinary text responses still stream incrementally, model tool calls execute and
resume, and explicit caller tool selection remains authoritative. Live acceptance
must exercise the same internal client identity as OpenWebUI and prove both search
and URL retrieval. Containers must remain healthy, and no new port or network
attachment may appear.

## Idempotence and Recovery

Database reconciliation is idempotent. Rebuilding and force-recreating only
`control-plane` is safe and does not restart model runtimes or OpenWebUI. If live
acceptance fails, restore the prior commit and rebuild/recreate only
`control-plane`; the stored API-client policy remains valid.

## Artifacts and Notes

Record focused test output, deployed image ID, response tool headers, and CI URL
here as work completes. Do not store API keys or complete private prompts.

Focused suite: 142 passed. Complete unit suite: 1,626 passed, 2 skipped. Live
OpenWebUI request: HTTP 200, model `b1-openai-gpt-oss-20b-mxfp4-localai`, finish
reason `tool_calls`, function `fetch_url`, URL `https://example.com`. Deployed
services `control-plane`, `open-webui`, `tool-search`, and `tool-firecrawl` are
healthy.

## Interfaces and Dependencies

The public interface remains OpenAI-compatible `/v1/chat/completions`. The private
`b1_tools` request field remains optional. Defaults come from
`AuthContext.default_b1_tools`. Managed tool implementations remain
`web_search` and `web_fetch`; no new dependency or external port is introduced.

Plan update note (2026-08-14): Initial plan created after live root-cause inspection.

Plan update note (2026-08-14 07:49Z): Recorded implementation, packaging recovery,
full tests, deployment, and production tool-call evidence.
