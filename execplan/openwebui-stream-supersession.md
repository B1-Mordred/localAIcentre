# Make Laguna chat handoff immediate, visible, and safe

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept current as the work proceeds. The methodology is the `references/PLANS.md` document from the installed `execplan` skill; that reference is present in the skill Git tree but absent from its sparse working copy, so it was read with `git show` before this plan was written.

## Purpose / Big Picture

After this change, a person using B1 AI Hub can send a new message as soon as the previous Laguna answer is useful. If the previous stream is still decoding, B1 will preserve the text already delivered, cancel only that request, wait for the worker to report that the request drained, and run the new message on the same loaded Laguna model. While a request waits for the single Tesla P40, Open WebUI will show a small status such as “Waiting for the P40 worker” rather than an error or an unexplained spinner. Laguna remains `--parallel 1`; this work does not permit simultaneous inference or weaken the scheduler lease.

The behavior is visible by opening one Open WebUI conversation, starting a deliberately long answer, and submitting a second message after useful text appears. The first message should finish as superseded without losing its displayed text, the UI should briefly show the wait/handoff status, and the second message should begin without a `409`, model unload, CUDA OOM, or another conversation being cancelled.

## Progress

- [x] (2026-08-09 11:55Z) Confirmed the production baseline: Open WebUI requests queue instead of returning `409`, the P40 backend remains `--parallel 1`, and the rollback image is tagged.
- [x] (2026-08-09 11:58Z) Located the controller stream finalizer, Open WebUI session-header support, Open WebUI native status-event handling, and the worker router’s downstream-disconnect behavior.
- [x] (2026-08-09 12:00Z) Wrote this initial self-contained ExecPlan.
- [x] (2026-08-09 12:02Z) Captured a controlled Laguna stream trace. The explicit marker was the last content, no characters followed it, finish reason was `stop`, and `[DONE]` followed 110 ms later.
- [x] (2026-08-09 12:28Z) Added staged controller implementation and 28 passing scheduler tests for conversation-scoped supersession, interruptible upstream reads, worker-drain verification, forced-recovery fallback, and native status SSE events.
- [x] (2026-08-09 12:29Z) Configured the pinned Open WebUI image to forward its chat UUID in `X-B1-OpenWebUI-Chat-Id`; the targeted Compose policy and internal-client trust assertions pass.
- [x] (2026-08-09 12:40Z) Built and rollback-tagged the controller, reused the unchanged pinned Open WebUI image, and ran the packaged 29-test scheduler suite plus targeted Compose policy assertions.
- [x] (2026-08-09 12:41Z) Passed public API and real-browser acceptance for visible queue status, same-conversation supersession, cross-conversation isolation, preserved partial text, lease handoff, and coherent history after reload.
- [x] (2026-08-09 12:44Z) Deployed only while the P40 was idle, recorded final and rollback image IDs, and updated the Laguna final report and JSON evidence.

## Surprises & Discoveries

- Observation: The lease is not waiting for its five-minute expiry. It is renewed while the upstream SSE response is active and explicitly released by the stream finalizer.
  Evidence: Production database rows changed to an already-expired timestamp immediately when `active_requests` reached zero.

- Observation: The existing cancellation path is too destructive for supersession.
  Evidence: `services/control-plane/app/main.py::cancel_sync_gpu_runtime` calls the worker `cancel` lifecycle hook, verifies that VRAM is released, and records the runtime as unloaded. Supersession must instead close only the HTTP inference request and preserve the loaded model.

- Observation: No custom frontend bundle is required for queue visibility.
  Evidence: Open WebUI v0.10.2 forwards a chat ID through `FORWARD_SESSION_INFO_HEADER_CHAT_ID` when session forwarding is enabled. Its streaming middleware emits any provider object containing an `event`, and its event emitter persists and displays events of type `status`.

- Observation: The Laguna router can propagate request-scoped cancellation from a closed downstream connection.
  Evidence: `b1_laguna_router.py::proxy_laguna` closes its child connection on `BrokenPipeError` or `ConnectionResetError` and decrements `active_requests` in `finally`, without unloading the child process.

- Observation: The controlled Laguna response has no measurable post-answer generation tail once the answer has an explicit terminal boundary.
  Evidence: `laguna-stream-termination-trace-20260809-v1.json` recorded zero characters after `[[ANSWER_COMPLETE]]`; the marker was the last content at 21.722832 s, finish reason `stop` arrived at 21.833019 s, and `[DONE]` arrived at 21.833099 s.

- Observation: The staged scheduler suite passes 28 of 28 tests, including a blocked upstream read that is interrupted by same-conversation supersession and both successful-drain and drain-timeout finalizers.
  Evidence: The disposable pinned control-plane test container completed `tests.unit.test_sync_inference_scheduler` in 0.568 seconds with no failures.

- Observation: The full Compose policy baseline contains two failures unrelated to this work: its expected LocalAI non-root `USER` differs from the current Dockerfile, and its expected runtime action-service list omits the currently configured `lipsync` service.
  Evidence: The staged full policy run passed 55 tests and failed only `test_production_localai_wrapper_uses_pinned_upstream_image_and_hooks` and `test_runtime_agent_mounts_generated_token_read_only`; `test_open_webui_is_local_only_by_default`, including both new forwarding assertions, passed.

- Observation: Open WebUI itself stages a message typed during generation behind a `Send now` control. Activating that control closes the old browser stream before submitting the new request; the controller's downstream-disconnect cleanup therefore performs the same request-scoped drain even when registry supersession is not the initiating signal.
  Evidence: Browser acceptance preserved 26 list items from the old answer, submitted `BROWSER_SECOND` with `Send now`, completed the second answer, and retained both after reload.

- Observation: The first browser cancellation exposed an orphaned HTTPX read-task traceback even though request drain and lease release succeeded.
  Evidence: The controller logged `Task exception was never retrieved` with `httpx.ReadError` after Open WebUI closed its old provider connection. The relay now cancels and gathers both the upstream read and supersession waiter in all exit paths; a dedicated downstream-cancellation test passes and the final-image acceptance logs are clean.

## Decision Log

- Decision: Keep `--parallel 1` and the global scheduler lease.
  Rationale: The user explicitly excluded the parallel-two option, and the current placement has only about 2.5 GiB of VRAM safety headroom.
  Date/Author: 2026-08-09, Codex.

- Decision: Scope preemption by Open WebUI chat UUID, not merely by API-client identity.
  Rationale: A new message should supersede only an older stream from the same conversation. Another browser tab or another user conversation must remain queued and must not be cancelled.
  Date/Author: 2026-08-09, Codex.

- Decision: Implement request cancellation by closing the controller-to-worker SSE response, then wait for the authenticated worker metrics to report `active_requests == 0` before releasing the lease.
  Rationale: The existing runtime cancellation hook unloads Laguna and is unsuitable for a quick handoff. Waiting for drain closes the race where the next owner could reach the `--parallel 1` server before the old router thread notices the disconnect.
  Date/Author: 2026-08-09, Codex.

- Decision: Use Open WebUI’s native status-event path rather than modifying compiled frontend JavaScript.
  Rationale: Native status events are version-pinned, persisted in `statusHistory`, displayed by the existing UI, and testable in backend code.
  Date/Author: 2026-08-09, Codex.

- Decision: Do not add heuristic early stopping until the trace proves a safe Laguna terminal condition.
  Rationale: Punctuation or answer-like text is not a safe stop signal for code, JSON, reasoning, or tool calls. Supersession is initiated by explicit user intent and is safer.
  Date/Author: 2026-08-09, Codex.

- Decision: Make no Laguna template, EOS, or stop-sequence change in this task.
  Rationale: The controlled trace showed correct natural termination only 110 ms after the explicit answer boundary and no trailing content. There is no model-side gap to remove safely.
  Date/Author: 2026-08-09, Codex.

## Outcomes & Retrospective

The requested options 1, 2, and 3 are deployed; option 4 remains excluded. A controlled trace showed no useful post-answer model tail, so no unsafe EOS or template heuristic was added. Same-chat public handoff preserved 47 characters and completed the replacement in 4.798 seconds. Seventy-eight worker samples observed at most one active request, at least 2,512 MiB free VRAM, a resident model, and no safety event. Different chats queue visibly and do not cancel each other.

The real Open WebUI browser rendered `Waiting for the P40 worker`, then `P40 worker acquired`. Its same-chat `Send now` flow preserved 26 streamed list items, returned `BROWSER_SECOND`, and remained coherent after reload. The final packaged controller scheduler suite passes 29/29. The production controller is `sha256:86d9c0c06b3899d9a59fc14db6554c5a2bcb3988a008ab19d118f647e44b5d1b`; the unchanged Open WebUI image is `sha256:d34224956fa68e0af7229305c02964ebbc4769936969e9b0d4906ed01059491e`. Pre-change rollback tags are retained for both.

The full repository unit baseline is not clean for unrelated reasons: 1,515 tests ran with 13 failures and 14 errors in existing dirty-tree areas. The scoped tests and production acceptance for this plan pass. Evidence is recorded in `/srv/b1-p40-worker/reports/laguna-s-2.1/openwebui-stream-handoff-20260809-v1.json` and `final-config.md`.

## Context and Orientation

The B1 controller repository is `/home/mordred/localAIcentre` on `ai.b1.germering`. It is an extensively modified working tree containing the live B1 AI Hub Compose project. Existing unrelated changes must be preserved. The controller is `services/control-plane/app/main.py`; its `/v1/chat/completions` route authenticates Open WebUI, resolves `laguna-s-quality` to the LAN P40 worker, and proxies SSE through `call_openai_runtime_stream`. A scheduler lease is one PostgreSQL row that grants one owner exclusive access to GPU inference. The lease prevents model replacement and simultaneous execution on the single P40.

Open WebUI is pinned in `deploy/open-webui/Dockerfile` to v0.10.2. Its entrypoint is `deploy/open-webui/b1-open-webui-entrypoint.sh`, and its Compose environment is in `compose.yaml`. The upstream Open WebUI router can forward the current chat UUID in a configurable HTTP header. The upstream streaming middleware understands an SSE object shaped as `{"event":{"type":"status","data":{"description":"...","done":false}}}` and shows it as message status instead of assistant text.

The P40 worker runs the managed router container `b1-p40-worker-laguna-router-1`. The router source used in the image is `/usr/local/bin/b1_laguna_router.py`; the production image is `b1-ai-hub/laguna-router:06f8cebd-sm61-v0.4.0`. Its `proxy_laguna` method holds one child HTTP connection to llama-server and records `active_requests`. A downstream disconnect closes that child connection and removes the request while leaving the loaded model resident. Worker metrics are authenticated at `/b1/runtime/metrics` and expose `work.active_requests`.

“Supersession” means that a newer message explicitly replaces an older, still-streaming inference request from the same Open WebUI chat. It does not delete already received text. “Drain” means that the controller has closed the old upstream response and the worker reports no active inference request. “Status event” means a non-content Open WebUI event that appears as transient progress metadata and does not become part of the assistant answer.

## Plan of Work

First, create a deterministic stream trace using the public B1 API and the real Open WebUI service credential. The prompt will require a short answer ending in a unique completion marker. Record monotonic timestamps for the first answer token, marker completion, last non-empty content delta, finish reason, `[DONE]`, worker `active_requests == 0`, and scheduler release. Record token usage but store no credential and no private conversation text. If the marker-to-finish gap contains meaningful output, document it. If the gap is only normal finalization, do not invent an early-stop rule.

Second, extend `services/control-plane/app/main.py` with a small in-memory registry keyed by the trusted Open WebUI chat UUID. Each active entry owns a cancellation event and a finished event. The `/v1/chat/completions` route will read `X-B1-OpenWebUI-Chat-Id`; it will honor the header only when authentication identifies `client_open_webui_internal`, validate its bounded ASCII form, and pass it to streaming proxy code. A new stream atomically replaces the registry entry and signals the prior entry from the same chat. Other chats do not receive that signal.

Refactor the Open WebUI streaming path so the HTTP streaming response begins before lease acquisition. While its lease-acquisition task is pending, emit a native status event at a bounded interval. Once acquired, emit a completed status event and prepare the existing runtime. If the new stream superseded an older one, describe that handoff in the status text without exposing lease IDs or internal credentials.

While relaying Laguna chunks, check the entry’s cancellation event. On supersession, emit a final `status` event for the old message, emit a terminal OpenAI chunk and `[DONE]`, close the HTTPX upstream response, and wait for the LAN worker metrics to report `active_requests == 0`. Then mark the runtime idle and release the scheduler lease. Do not call `cancel_sync_gpu_runtime`, do not unload the model, and do not report success unless drain is observed. If request-scoped drain fails within a short bound, use the existing forced recovery path because safety is more important than latency.

Third, set `ENABLE_FORWARD_USER_INFO_HEADERS=true` and `FORWARD_SESSION_INFO_HEADER_CHAT_ID=X-B1-OpenWebUI-Chat-Id` in the Open WebUI Compose service. The B1 controller ignores the accompanying user-information headers. Add Compose-policy and wrapper tests to pin the session header behavior. Because the header travels only from the internal Open WebUI service over the Compose network and is ignored for all other API identities, it cannot let an external caller cancel an Open WebUI conversation.

Finally, build new controller and Open WebUI images with rollback tags, deploy them only when worker metrics show `active_requests == 0`, and validate through the public API and a real browser. Preserve Caddy, LocalAI, the Laguna worker, all unrelated services, and the existing model placement.

## Concrete Steps

Work from `/home/mordred/localAIcentre` on `ai.b1.germering`. Before each deployment, check the worker from `userver`:

    sudo docker exec b1-p40-worker-laguna-router-1 python3 -c '<authenticated metrics query>'

Expect `active_requests` to be zero before recreating any B1 service.

Run the stream-trace probe through `https://api.ai.b1.germering/v1/chat/completions` using the Open WebUI key read into memory from `/srv/b1-ai-hub/secrets/open_webui_api_key`. Save redacted evidence under `/srv/b1-p40-worker/reports/laguna-s-2.1`; never write the key.

Run scheduler tests in the pinned service environment:

    cd /home/mordred/localAIcentre
    sudo docker compose run --rm --no-deps \
      -v "$PWD/services/control-plane/app:/app/app:ro" \
      -v "$PWD/tests:/app/tests:ro" \
      control-plane python -m unittest -v tests.unit.test_sync_inference_scheduler

Run Open WebUI wrapper and Compose-policy tests using the appropriate repository test environment or disposable service image. Build rollback tags before rebuilding the live tags. Recreate only `control-plane` and `open-webui`; do not restart the gateway, worker, LocalAI, or unrelated services.

## Validation and Acceptance

The stream trace must distinguish content production from transport finalization. It must state whether a safe natural stop correction exists. No heuristic stop change is accepted without a deterministic Laguna marker that also preserves code, JSON, reasoning, streaming, and tool calls.

Unit tests must prove that an authenticated Open WebUI stream can supersede only an older stream with the same validated chat ID; a different chat ID queues normally; an external API client cannot activate supersession by forging the header; already-delivered chunks are preserved; the old stream emits `[DONE]`; request drain precedes lease release; a drain timeout invokes forced recovery; and ordinary cancellation remains correct.

Integration acceptance uses two overlapping SSE requests through the public B1 API. For the same chat ID, the first must be request-cancelled without unloading Laguna and the second must begin after drain with no `409`. For different chat IDs, the second must show a waiting status and begin only after the first finishes naturally. The worker must remain `parallel=1`, report at most one active request, retain approximately 1.5–2 GiB or more VRAM headroom, and show no safety event.

Browser acceptance must use `https://ai.b1.germering`. The queued assistant message must visibly show a waiting/handoff status. Submitting a second same-chat message after useful output appears must preserve the old text, mark that message completed/superseded, and start the new answer. Refreshing the page must retain a coherent history. Cancellation, ordinary streaming, tool calls, and reasoning must still work.

Production acceptance also checks control-plane, Open WebUI, gateway, and worker health; public model visibility; scheduler lease release; idle unload and wake; and absence of new `409`, traceback, CUDA OOM, or forced recovery during normal supersession.

## Idempotence and Recovery

All source edits are additive or narrowly scoped and can be rebuilt repeatedly. The registry is in memory and requires no database migration. A controller restart clears it; existing leases still expire through the scheduler watchdog. Deployment happens only while inference is idle.

Before replacing each live image, tag its exact ID with a dated rollback name. Rollback retags the prior image as the live Compose image and force-recreates only the affected service. If request-scoped cancellation does not drain reliably, disable supersession while retaining the existing bounded queue and status events. Never solve a drain failure by allowing a second request into the worker.

## Artifacts and Notes

The current production baseline before this plan is:

    controller image: sha256:2a2cfa74908ed07c941e7936acd81185bc3b445b99c7c1d8a98e189daa4c4f6f
    controller rollback: b1-ai-hub-control-plane:pre-openwebui-lease-wait-20260809
    worker router: b1-ai-hub/laguna-router:06f8cebd-sm61-v0.4.0
    llama.cpp commit: 06f8cebd7fe728687be3d19f8bdedb70d75883af
    server concurrency: --parallel 1

The final production and rollback identities are:

    controller image: sha256:86d9c0c06b3899d9a59fc14db6554c5a2bcb3988a008ab19d118f647e44b5d1b
    controller rollback tag: b1-ai-hub-control-plane:pre-stream-supersession-20260809
    controller rollback image: sha256:2a2cfa74908ed07c941e7936acd81185bc3b445b99c7c1d8a98e189daa4c4f6f
    Open WebUI image: sha256:d34224956fa68e0af7229305c02964ebbc4769936969e9b0d4906ed01059491e
    Open WebUI rollback tag: b1-ai-hub-open-webui:pre-stream-supersession-20260809

The Laguna production report is `/srv/b1-p40-worker/reports/laguna-s-2.1/final-config.md`. Add the final trace, production image IDs, exact rollback commands, and acceptance results there.

## Interfaces and Dependencies

In `services/control-plane/app/main.py`, define a private active-stream record containing a generated request ID, a hashed chat ID, an `asyncio.Event` used to request supersession, and an `asyncio.Event` used to signal final drain. Maintain it in an event-loop-local dictionary; registration and replacement contain no `await`, so each transition is atomic within the controller process. Add a validated optional supersession event to `call_openai_runtime_stream`.

Add a helper that serializes Open WebUI status SSE objects in this exact outer shape:

    {"event":{"type":"status","data":{"description":"Waiting for the P40 worker","done":false}}}

Add a LAN-worker drain helper that uses the selected runtime adapter’s authenticated request headers to query `/b1/runtime/metrics` and returns only after `work.active_requests` is zero. Its timeout must be bounded and tested.

In `compose.yaml`, set the pinned Open WebUI service environment so the upstream v0.10.2 router forwards its metadata chat UUID as `X-B1-OpenWebUI-Chat-Id`. Do not forward this header from Caddy or trust it for any API identity other than `client_open_webui_internal`.

The implementation uses existing dependencies only: FastAPI/Starlette streaming responses, HTTPX asynchronous streaming, asyncio synchronization primitives, Open WebUI’s existing session forwarding, and its existing status event renderer. No Docker socket, new public endpoint, new inference port, database migration, or additional service is introduced.

Revision note (2026-08-09 12:00Z): Initial plan created after production code and pinned Open WebUI behavior were inspected. The design chooses request-scoped connection cancellation and native status events while explicitly excluding parallel inference and heuristic early stopping.

Revision note (2026-08-09 12:02Z): Updated after the controlled termination trace. The trace rules out a useful post-marker decoding tail, so model/template early stopping is explicitly removed from the implementation scope.

Revision note (2026-08-09 12:29Z): Updated after staged implementation and tests. Recorded 28 passing scheduler tests, the passing targeted Compose assertion, two unrelated full-policy baseline failures, and the event-loop-atomic registry design.

Revision note (2026-08-09 12:44Z): Completed after final deployment and acceptance. Added downstream-cancellation task reaping, raised the scoped suite to 29 tests, recorded public and browser outcomes, final images, rollback tags, and evidence paths.
