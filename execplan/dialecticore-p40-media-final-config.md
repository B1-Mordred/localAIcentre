# DialectiCore managed P40 media configuration

Date: 2026-08-10 UTC

## Production routing

DialectiCore submits GPU visual work only to `https://api.ai.b1.germering`. B1 remains the sole owner of the global GPU scheduler lease. No DialectiCore component calls the P40 ComfyUI or MuseTalk backend directly.

- `studio-seated-character-p40` -> `lan-p40-media`
- `talking-head-lipsync` -> `lan-p40-media`
- `studio-panel-shot` -> `panel-cpu`
- `laguna-s-quality` -> `lan-localai-worker`

The DialectiCore workflow is `workflow-studio-seated-character-p40-v2`, model alias `studio-seated-character-p40`, runtime policy `any`, and native dimensions 1280x720. The dedicated alias has no non-P40 runtime, so `any` does not permit a silent RTX 3060 fallback. The legacy `workflow-studio-seated-character-v1` remains stored for explicit rollback but is ordered after the P40 workflow.

## Pinned runtime

- B1 control plane tag: `b1-ai-hub/control-plane:p40-media-v0.1.17`
- B1 control plane image digest/ID: `sha256:dc86430bb79a10101a5686c0670775202c0c7b6b559397bac68703a185e05539`
- Previous rollback tag: `b1-ai-hub/control-plane:p40-media-v0.1.9`, image `sha256:1741ac8f105e8b9d594c6c67b89a487752e7b6eb67bc4375dfa0e9c81c5eb70b`
- P40 media manager: `b1-ai-hub/p40-media-manager:v0.1.2@sha256:a47b16abb44457111c316850a2461be353a71eb8fffd9c6392279d5f2f1a4267`
- P40 ComfyUI: `b1-ai-hub/comfyui:v0.3.77-b1-pascal-cu124@sha256:26ce27c1c2b576a3c503ead71a9598860b24563812b49fae236108a9c0707d69`
- P40 MuseTalk image: `b1-ai-hub-lipsync@sha256:b8a6fb843e78559bd39a8d15d683c82bf3bd825fe4c6f5dba9466dda5840a72f`
- MuseTalk commit: `0a89dec45a0192b824e3cf4daf96c239440c5ed8`
- Base media model version: `/srv/b1-p40-worker/models/media/versions/seated-v1`
- Robot matte model version: `/srv/b1-p40-worker/models/media/versions/seated-v3`
- Approved production gate manifest: `/srv/b1-p40-worker/models/media/versions/seated-v4/manifest.json`
- BiRefNet General Lite SHA-256: `5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333`, 224,005,088 bytes, read-only
- MuseTalk version mount: `/srv/b1-p40-worker/models/media/versions/seated-v1/lipsync` -> `/srv/b1-ai-hub/models`, read-only
- DWPose checkpoint SHA-256: `0d9408b13cd863c4e95a149dd31232f88f2a12aa6cf8964ed74d7d97748c7a07`

Production backends are attached only to Docker network `b1-p40-worker_media`, which is `internal=true`. ComfyUI and MuseTalk have no published host ports, and none of the media containers mounts the Docker socket. Caddy is the only LAN-facing gateway.

## Acceptance evidence

- Native seated plate: public job `job_72efb8a2645c4a7199f554f305f58efc`, 1280x720, 84.607 s, SHA-256 `1b7595426dbc665cf2e6979dae7c9f218fc026cb42aa716fcac487bd3c79b924`, visually approved reference `upload_e9934e5a78274cf895a662c356e5675d`.
- CPU panel lane: public job `job_241550b0ae344cc78e9e062b90c35698`, 0.692 s, runtime `panel-cpu`, no model load and no VRAM allocation, SHA-256 `0c317b3cd4a39aac3865bfb0e1d14725143a4d86425237daf89b82e2f9172793`.
- Queued cancellation: `job_e5361875031b4b449010b67ed9153203`, terminal `cancelled`, 2.14 s, zero artifacts.
- In-flight cancellation: `job_3349cfa9e11642bd86e16d904e8e47b3`, real native prompt reached, terminal `cancelled` in 4.636 s, managed cancel/recover hooks invoked, zero artifacts.
- MuseTalk: public job `job_f5d64dbde9884ebc93e4482bb4eaa44f`, runtime `lan-p40-media`, 80.3 s wall time, 78.624 s run time, measured peak VRAM 16,635 MiB, peak RAM 18,746 MiB. Output is H.264/AAC, 512x512, 25 fps, 20 frames, 0.816 s, SHA-256 `ec7d6f9d3ef3aec0a63ab2d407ed0e5436281040bc762dab1772c50ae000ffb2`.
- Laguna cold wake after media: HTTP 200, 92.087 s to headers, 104.770 s TTFT, correct response. Lease expiration timestamp equalled stream completion.
- Laguna warm reuse after the handoff fix: HTTP 200 headers in 0.110 s, TTFT 2.090 s, total 2.786 s. The lease was immediately expired at completion.
- Idle media state: 233 MiB VRAM used, 22,674 MiB free, 0% GPU utilization.
- Tests: 169 focused B1 control-plane tests plus 60 subtests passed; 52 DialectiCore ComfyUI/media tests passed.
- Robot matte qualification: U2Net 0.604 s, IS-Net general-use 1.492 s on the first seed but rejected on a fresh seed, BiRefNet General Lite 10.093 s in qualification and 8.663-9.213 s on production plates. All six approved production plates record the exact BiRefNet SHA and near-head halo evidence.
- Approved six-seat panel: public job `job_470f2781e3244a1794e3f7dbdada1290`, runtime `panel-cpu`, 1280x720, SHA-256 `5eb40a570af88eb6f195ed89609514dc9bbafc9b79b5d965a9289a8abe56cf2e`; all six seats occupied, physical desk/rear screen preserved.
- Scene lipsync establishing wide: `job_4b6438bf321d4818b157b4a8f28a8a88`, 21.000 s, 1024x576 at 12 fps, SHA-256 `1d9e7b2843619338c031f101de260c38c8f6e27ec8b1548d30987421c0c2577e`.
- Scene lipsync speaker medium: `job_31ce1aaf7b704db8bdf7f63756605fae`, 14.667 s, 1024x576 at 12 fps, 178x140 speaker face, SHA-256 `4a1ca95a76ece34c4490538ce66ddb8ae6d071e05f21994801e955965d54a591`; all 176 mouth-region frames are distinct.
- Post-job idle: scheduler lease expired, 233 MiB VRAM used, 22,674 MiB free, 0% GPU utilization.

## Scheduling behavior

Interactive chat waiters prevent the GPU runner from claiming a new media job. A media job already running remains atomic. When switching from Laguna to media, B1 unloads Laguna under the lease before the media load. Media is not followed by an automatic Laguna reload; the next chat performs that load. Explicit unloaded runtime-state records prevent redundant unload hooks during warm reuse, while a missing runtime-state record remains fail-closed and is probed.

## Operator commands

Run these from `/opt/b1-p40-worker`; every command is scoped to P40 media services.

    sudo docker compose --env-file .env ps media-manager media-comfyui media-lipsync gateway
    sudo docker compose --env-file .env logs --tail=200 media-manager media-comfyui media-lipsync
    sudo docker compose --env-file .env restart media-manager media-comfyui media-lipsync
    sudo docker compose --env-file .env stop media-manager media-comfyui media-lipsync
    sudo docker compose --env-file .env up -d --no-deps media-comfyui media-lipsync media-manager

Do not start an independent ComfyUI or MuseTalk service and do not expose backend ports.

## Rollback

1. Stop only `media-manager`, `media-comfyui`, and `media-lipsync` in `/opt/b1-p40-worker`.
2. On B1, retag `b1-ai-hub/control-plane:p40-media-v0.1.14` as `b1-ai-hub-control-plane:latest` and force-recreate only `control-plane` with the existing production Compose files. This restores the previous U2Net/GrabCut matte; do not approve robot plates produced by that rollback path.
3. Disable or remove only the P40-specific alias/workflow. Preserve `laguna-s-*`, Caddy, the LAN worker adapter, and all unrelated services.
4. Verify the scheduler lease is expired, `nvidia-smi` is at idle, and the original local aliases still resolve before accepting jobs.

## Known limitation

MuseTalk's internal face detector rejects Grok's dark robotic face in the native speaker crop (`job_7131af36255446b1b2b7e51e8894b5da`, `scene_face_not_detected`). The clip remains failed/retryable and no static fallback was accepted. ChatGPT passes both wide and medium scene-aware lipsync, but full-episode completion requires an explicit-region/non-Haar tracker or a separately qualified higher-detail speaker source for Grok.
