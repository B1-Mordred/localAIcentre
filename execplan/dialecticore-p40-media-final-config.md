# DialectiCore managed P40 media configuration

Date: 2026-08-16 UTC

## Production routing

DialectiCore submits GPU visual work only to `https://api.ai.b1.germering`. B1 remains the sole owner of the global GPU scheduler lease. No DialectiCore component calls the P40 ComfyUI or MuseTalk backend directly.

- `studio-seated-character-p40` -> `lan-p40-media`
- `talking-head-lipsync` -> `lan-p40-media`
- `studio-panel-shot` -> `panel-cpu`
- `laguna-s-quality` -> `lan-localai-worker`

The DialectiCore workflow is `workflow-studio-seated-character-p40-v2`, model alias `studio-seated-character-p40`, runtime policy `any`, and native dimensions 1280x720. The dedicated alias has no non-P40 runtime, so `any` does not permit a silent RTX 3060 fallback. The legacy `workflow-studio-seated-character-v1` remains stored for explicit rollback but is ordered after the P40 workflow.

## Pinned runtime

- B1 control plane source commit: `f73dc16db43da6e360448773a424d8e270bf2322`
- B1 control plane image digest/ID: `sha256:68ed6a4389fbe7682933cf0157454cf0aa461cb2c3a3e4c9c10254326c0f02c7`
- Previous control plane rollback image: `sha256:e86b3e864ae2f1cf4d8eaf51289862891dfa0f4dbd166c5c41ae18511cfbfa6e`
- P40 media manager: `b1-ai-hub/p40-media-manager:v0.1.3@sha256:bec3fb35922b8e41aa81e986380d9ae95ada8b690e2a7733b7a4f651f5451baa`
- Previous media-manager rollback image: `b1-ai-hub/p40-media-manager:v0.1.2@sha256:a47b16abb44457111c316850a2461be353a71eb8fffd9c6392279d5f2f1a4267`
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

Idle recovery uses a 256 MiB unloaded baseline, a bounded 256 MiB allowance,
and three consecutive samples at or below the resulting 512 MiB threshold.
This is a stability envelope, not permission to retain a model: recovery also
requires both ComfyUI and MuseTalk to report no resident model.

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
- Recovery-hook qualification: authenticated `POST /b1/runtime/recover` returned
  HTTP 200 with samples `[233,233,233]`, zero loaded ComfyUI models, and a null
  resident MuseTalk model. The P40 remained capped at 125 W.
- DialectiCore episode `9d145344-82c9-46cc-b4c1-661d95f0bf56`: one gated clip
  plus 20 serial recovery jobs completed without a batch failure. All 21 current
  clips synchronized as H.264/AAC at 1024x576 and 12 fps; local SHA-256 values
  matched B1, native camera evidence passed, the rear screen was preserved, and
  every clip records `audio_driven_seated_panel` without fallback or stale camera
  rejection metadata.
- The gate asset `344e4d35-770b-4c7a-a66b-6ec3e3a5315f` used B1 job
  `job_7851d2d303d54fae83c25047d7df535f`, SHA-256
  `f45c0ff496a2be5344ae8a397647354017888cf5c4b0a66b6190d3dcb2f87bca`,
  and changed its mouth-region luma above 0.1 in 103 of 104 transitions.
- Tests after the recovery changes: 1,630 B1 unit tests plus compatibility,
  security, and frontend quality gates passed; all seven media-manager tests and
  all 56 DialectiCore ComfyUI service tests passed.

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
2. For only the 2026-08-16 recovery-evidence change, retag control-plane image
   `sha256:e86b3e864ae2f1cf4d8eaf51289862891dfa0f4dbd166c5c41ae18511cfbfa6e`
   as `b1-ai-hub-control-plane:latest` and force-recreate only `control-plane`
   with the existing production Compose files.
3. To roll back only idle recovery, pin media-manager v0.1.2 at
   `sha256:a47b16abb44457111c316850a2461be353a71eb8fffd9c6392279d5f2f1a4267`
   and force-recreate only `media-manager`. Its exact 256 MiB threshold can
   falsely reject the measured 233 MiB idle state after small CUDA fluctuations.
4. Disable or remove only the P40-specific alias/workflow. Preserve `laguna-s-*`, Caddy, the LAN worker adapter, and all unrelated services.
5. Verify the scheduler lease is expired, `nvidia-smi` is at idle, and the original local aliases still resolve before accepting jobs.

## Known limitation

MuseTalk's internal face detector can reject Grok's dark robotic face in the
native speaker crop (`job_7131af36255446b1b2b7e51e8894b5da`,
`scene_face_not_detected`). The managed runtime now uses the declared scene face
region as its bounded fallback; the current full-episode batch completed without
accepting a static visual fallback. A future tracker may improve motion quality,
but it is not required for completion or camera-coverage correctness.
