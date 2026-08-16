# Persist DeepSeek artifact attestations across router recovery

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

The managed P40 DeepSeek router must continue to verify every new or changed GGUF artifact against its registered SHA-256 digest, but recreating an otherwise unchanged router must not reread and hash all 104 GiB before a user can receive an answer. After this change, a successful full attestation is stored in a small authenticated cache. A later router instance reuses it only when the registered profile digest and every file's device, inode, size, modification time, and expected SHA-256 remain identical. A changed or untrusted cache causes a normal full verification.

## Progress

- [x] (2026-08-16 20:24Z) Reproduced the slow recovery and identified first-load full hashing after router recreation.
- [x] (2026-08-16 20:25Z) Confirmed that the existing in-memory identity cache already avoids rehashing ordinary idle reloads.
- [x] (2026-08-16 20:28Z) Implemented an authenticated persistent attestation cache and fail-closed invalidation tests.
- [x] (2026-08-16 20:31Z) Built and pinned managed router image `sha256:c823817bfadc565def218d3b90135d59c810a182dc06aa3c5cef7eb7d15c9c2e`.
- [x] (2026-08-16 20:32Z) Added the bounded cache mount to the installed Compose project without changing model or runtime placement.
- [x] (2026-08-16 20:35Z) Validated a router recreation and fast managed B1 reload using the persisted attestation.
- [x] (2026-08-16 20:36Z) Recorded rollback and final evidence.

## Surprises & Discoveries

- Observation: The reported 503 was initially caused by stale NVIDIA container state, not model corruption.
  Evidence: Host `nvidia-smi` succeeded while the old router returned `Failed to initialize NVML: Unknown Error`; recreating only `deepseek-router` restored `/readyz` and GPU telemetry.
- Observation: `DeepSeekRuntime.verified_files` already caches successful hashes for the lifetime of one router process.
  Evidence: `_verify_profile_files()` skips SHA-256 when device, inode, size, modification time, and expected digest match the in-memory record.
- Observation: The persistent cache removed checksum I/O but model initialization remains material.
  Evidence: Manager reads increased by about 88 KiB before llama-server launch. The public request completed in 125.452 seconds versus 548.051 seconds during full attestation and cold load.

## Decision Log

- Decision: Persist only authenticated attestation metadata, never model content, request data, or arbitrary commands.
  Rationale: This removes repeat hashing after safe container recovery while preserving the read-only model mount and existing security boundaries.
  Date/Author: 2026-08-16 / Codex
- Decision: Treat any malformed cache, token rotation, profile digest change, file identity change, or expected digest change as a cache miss.
  Rationale: Performance state must fail closed to the existing full SHA-256 path rather than weaken artifact validation.
  Date/Author: 2026-08-16 / Codex

## Outcomes & Retrospective

The router now persists a signed four-file attestation across controlled container recreation. The next scheduler-controlled public request started llama-server without model-wide checksum reads and returned HTTP 200 with visible content `OK`. Cold response time fell from 548.051 seconds to 125.452 seconds. The remaining delay is actual model initialization. Malformed signatures and changed artifact identities fall back to full hashing. Model files remain read-only and the scheduler-aware B1 path remains mandatory.

## Context and Orientation

`deploy/deepseek-v4/deepseek_manager.py` is the authenticated LAN worker adapter and process manager for the pinned DeepSeek llama-server. `DeepSeekRuntime._verify_profile_files()` validates every registered shard below `/models`. `/models` is a read-only bind mount. The installed worker Compose project is `/opt/b1-p40-worker/compose.yaml`; B1 reaches it only through the existing Caddy gateway and lifecycle hooks.

The persistent cache is a small JSON document stored outside the read-only root filesystem in a dedicated cache directory. It is authenticated with HMAC-SHA256 using the existing runtime-control credential. The cache is only an optimization: failure to read, validate, or write it falls back to full hashing.

## Plan of Work

Extend `DeepSeekRuntime` to load and validate a signed cache during construction and save it atomically after a successful hash. Keep the existing identity tuple and SHA-256 comparison as the cache key. Add tests proving reuse across two runtime instances and invalidation after artifact or cache mutation. Add a dedicated Compose cache bind mount and environment path, prepare it for the unprivileged `deepseek` identity, build a new pinned image, and recreate only the DeepSeek router after active work ends.

## Concrete Steps

From `/opt/b1-ai-hub-source`:

    python -m unittest deploy/deepseek-v4/test_deepseek_manager.py
    docker build -f deploy/deepseek-v4/Dockerfile.router -t b1-ai-hub/deepseek-v4-router:030ebb55-sm61-default-v0.1.3 deploy/deepseek-v4

From `/opt/b1-p40-worker` after no DeepSeek request is active:

    sudo docker compose up -d --no-deps --force-recreate deepseek-router

Expected: the first load creates a signed cache; after router recreation, the load advances directly to llama-server startup without reading 104 GiB for SHA-256 again.

## Validation and Acceptance

Unit tests must prove cache reuse and fail-closed invalidation. Compose validation must pass. The router must remain unprivileged after startup, keep `/models` read-only, retain all capability and network restrictions, and expose no new port. A public request through `https://api.ai.b1.germering` must acquire the B1 lease and succeed. A controlled router recreation followed by a second public request must show negligible artifact-read growth before llama-server starts.

## Idempotence and Recovery

Cache writes use a temporary sibling and `os.replace()`. An interrupted or malformed write is ignored and causes a full hash. Removing the cache file is always safe. Rollback restores the prior pinned image and removes the cache environment/mount, after which the original in-memory-only behavior returns.

## Artifacts and Notes

Initial recovery evidence showed 104,211,818,925 bytes read by the manager before llama-server startup. After recreation with the persistent cache, manager reads moved from 2,647,847 to 2,735,869 bytes before the child appeared. The public acceptance response was HTTP 200 in 125.452 seconds from `deepseek-quality`, with content `OK`, 119 reasoning characters, and 42 total tokens.

Rollback is to restore router image `b1-ai-hub/deepseek-v4-router:030ebb55-sm61-default-v0.1.2@sha256:ceb1a1e789ca0e1cfdb99a681bee00dae5544bb238bbc9dde0ad66e7d4987205`, remove `B1_DEEPSEEK_ATTESTATION_CACHE_FILE` and the `/var/cache/b1-deepseek` mount from `/opt/b1-p40-worker/compose.yaml`, and recreate only `deepseek-router`. The cache directory can remain unused or be removed while the router is stopped.

## Interfaces and Dependencies

The cache uses only Python standard-library `json`, `hmac`, `hashlib`, `os`, and `pathlib`. No B1 API, model schema, inference request, tensor placement, context, security route, or scheduler contract changes.

Plan created 2026-08-16 to replace container-lifetime-only attestation caching with conservative persistent reuse after managed router recovery. Updated 2026-08-16 after unit, Compose, live recreation, and public B1 acceptance completed.
