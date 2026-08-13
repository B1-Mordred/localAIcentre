# Managed P40 media worker for DialectiCore

This ExecPlan is a living document. Keep `Progress`, `Surprises and discoveries`, `Decision log`, and `Outcomes` current while implementing the work.

## Purpose

Move DialectiCore's GPU-backed visual production behind B1's existing scheduler onto the Tesla P40 without exposing a second scheduling surface. The first production gate is a genuine 1280x720 seated panel scene with reliable character placement and scene-aware lip animation. DialectiCore continues to call only the public B1 API. Existing local B1 media remains available for rollback but P40-qualified aliases fail closed rather than silently falling back to the RTX 3060.

## Progress

- [x] 2026-08-09: Inventoried B1 runtime registry, global lease, media executor, ComfyUI/MuseTalk services, model manifests, workflows, current aliases, and the installed P40 Laguna Compose project.
- [x] 2026-08-09: Confirmed current executor hard-codes local `comfyui` and `lipsync` endpoints; runtime registration alone cannot move work to the P40.
- [x] 2026-08-09: Added and unit-tested the private `lan-p40-media` adapter and per-job authenticated ComfyUI/MuseTalk endpoint selection.
- [x] 2026-08-09: Added internal-only P40 ComfyUI/MuseTalk/manager services, Caddy forward authentication, lifecycle/metrics hooks, bounded logs, and no direct backend ports or Docker socket.
- [x] 2026-08-09: Staged, hash-verified, and atomically published the immutable `seated-v1` artifact set under `/srv/b1-p40-worker/models/media`.
- [x] 2026-08-09: Added interactive-waiter priority so the GPU runner cannot claim a new media job while an OpenWebUI chat is waiting; an already-running media job remains atomic.
- [x] 2026-08-09: Built and qualified the pinned Pascal ComfyUI image with an executed `sm_61` CUDA kernel.
- [x] 2026-08-09: Produced and visually approved a native 1280x720 transparent seated-character plate through public B1 job `job_72efb8a2645c4a7199f554f305f58efc`.
- [x] 2026-08-09: Moved deterministic panel composition to the `panel-cpu` runtime, which neither acquires the GPU lease nor reports synthetic VRAM.
- [x] 2026-08-09: Qualified real MuseTalk inference through public B1 job `job_f5d64dbde9884ebc93e4482bb4eaa44f`; the 512x512, 25 fps, 0.816 s MP4 is bound to the supplied WAV and records the pinned MuseTalk commit.
- [x] 2026-08-09: Published `studio-seated-character-p40`, `studio-panel-shot`, and `talking-head-lipsync` on `lan-p40-media`/`panel-cpu`, then added DialectiCore workflow `workflow-studio-seated-character-p40-v2` while preserving the legacy workflow for rollback.
- [x] 2026-08-09: Verified queued and in-flight cancellation, manager recovery, post-media Laguna wake, immediate lease release, warm reuse, idle unload, lipsync restart recovery, free VRAM, and internal-only backend networking.
- [x] 2026-08-10: Installed the empty 1672x941 studio master `47d9f89bed32daac...`, repaired stale DialectiCore workflow bindings, and replanned six native 1280x720 P40 seated plates plus their CPU panel dependency graph.
- [x] 2026-08-10: Ran first-gate ChatGPT plate job `job_24ff4d080ea14c44af2e8501cbc05c6a`; rejected it because the human-only U2Net matte retained a large opaque polygon around the robot when composited over the empty studio.
- [x] 2026-08-10: Qualified U2Net, IS-Net general-use, and BiRefNet General Lite against multiple generated seeds. Published `seated-v3` read-only with BiRefNet SHA-256 `5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333`, added near-head halo rejection, and passed the fresh ChatGPT gate.
- [x] 2026-08-10: Produced, visually reviewed, and approved all six seated plates, then produced and approved CPU panel job `job_470f2781e3244a1794e3f7dbdada1290` with the empty studio and all six occupied seats.
- [x] 2026-08-10: Corrected DialectiCore's native-camera adapter so editorial moves remain timeline metadata while MuseTalk receives `action=cut`; passed establishing-wide job `job_4b6438bf321d4818b157b4a8f28a8a88` and visible speaker-medium job `job_31ce1aaf7b704db8bdf7f63756605fae`.
- [ ] Qualify a non-Haar or explicit-region MuseTalk face tracker for Grok's dark robotic face. Job `job_7131af36255446b1b2b7e51e8894b5da` failed closed with `scene_face_not_detected`; no static fallback was accepted.

## Surprises and discoveries

- B1 already has the desired three-stage seated workflow, but active DialectiCore profiles still target the older 512x512 talking-head workflow.
- The existing B1 ComfyUI image contains PyTorch 2.8.0, while the MuseTalk image contains PyTorch 2.0.1. Each must be tested for actual `sm_61` execution; an image starting successfully is not sufficient evidence.
- The real P40 kernel gate rejected the existing ComfyUI image: PyTorch 2.8/CUDA 12.9 advertised only `sm_70` and newer and `torch.arange(..., device="cuda")` failed with `no kernel image is available for execution on the device`. The warning alone was not treated as failure; the executed kernel was decisive.
- The existing MuseTalk image passed the same real kernel gate with PyTorch 2.0.1/CUDA 11.7 and explicitly listed `sm_61`.
- Existing Wan manifests use FP8 text encoders and are not eligible for the Pascal production lane.
- The B1 source tree is intentionally dirty with a large body of current media work. Changes here must be narrow and file-by-file; no reset, checkout, broad rewrite, or automatic commit is safe.
- The installed ExecPlan skill references `references/PLANS.md`, but that required file is missing from the installed skill package. This plan follows the standard living-plan structure as a fallback.
- Active cancellation initially terminated as `failed` because the seated path treated a cancelled history wait as a render timeout. The runner now invokes managed recovery and preserves terminal `cancelled` state.
- Warm Laguna reuse initially returned HTTP 503 because B1 reissued unload hooks for runtimes already recorded as unloaded; the inactive media manager then mistook Laguna's VRAM for its own. Explicit unloaded states are now skipped, while missing state remains fail-closed.
- Remote MuseTalk needs dual authentication: Caddy validates `Authorization`, while the internal runtime validates `X-B1-Runtime-Token`. Both are now sent on the managed route.
- The MuseTalk weights were correctly published, but its fixed `./models` lookup was not mounted. The exact `seated-v1/lipsync` version is now mounted read-only at `/srv/b1-ai-hub/models`.
- The supplied empty studio solved the baked-in-character problem, but exposed a different gate: `u2net_human_seg.onnx` classifies the stylized robot's glow/card as foreground. GrabCut and threshold sweeps cannot remove the card without deleting robot limbs, so this requires a general object matte rather than another threshold constant.
- IS-Net looked clean on the first rejected frame but retained a broad near-body glow on a fresh seed. BiRefNet General Lite remained clean on both seeds. The first production wrapper also centre-cropped 16:9 to square; matching the published full-frame resize was necessary to reproduce qualification.
- Editorial camera moves such as `slow_push` cannot be passed into B1's native lipsync camera contract. DialectiCore now records the requested move separately and submits a stable `cut`; the render timeline can apply the move after lipsync.
- The approved 1280x720 six-seat panel contains only 25-43 source pixels of face height per participant. B1 can create a 140px native speaker-medium crop, but MuseTalk's internal detector still rejects Grok's especially dark face. ChatGPT passes the same path with real mouth motion.

## Decision log

- Decision: B1 remains the sole lease owner. No P40 endpoint may accept an inference job without B1's authenticated runtime control path.
  Rationale: one physical P40 is shared by Laguna and media; a second scheduler would permit CUDA OOM and corrupt ownership semantics.
- Decision: interactive Laguna has priority at job boundaries, but an active media operation is atomic and is not preempted mid-render.
  Rationale: forced interruption can corrupt media artifacts; bounded completion plus no new media dispatch while chat waits gives predictable behavior.
- Decision: Laguna is not automatically reloaded after media. The next chat request loads it.
  Rationale: avoids wasting several minutes of reload time when more media work is queued.
- Decision: keep the panel compositor CPU-only on B1.
  Rationale: composition does not benefit from CUDA and must not consume the P40 lease.
- Decision: enable P40 aliases one capability at a time. Unqualified aliases remain on the existing runtime and P40-qualified aliases fail closed.
  Rationale: Pascal incompatibility must not become a silent quality regression.
- Decision: qualify CPU ONNX general-object mattes outside the GPU lease and publish only the visually winning immutable artifact.
  Rationale: matting is CPU post-processing; keeping it off CUDA preserves P40 scheduling while a model-derived alpha is more reproducible than character-specific color keys or hand masks.
- Decision: use pinned BiRefNet General Lite, not the faster IS-Net candidate, and reject mattes whose near-head wedges exceed alpha 8.
  Rationale: end-to-end visual robustness across generated seeds is more important than saving roughly eight seconds of CPU time.
- Decision: do not hide robot-specific MuseTalk face-detection failures behind static video.
  Rationale: a static fallback would violate the requested audio-driven performance and make a failed character look complete.

## Implementation outline

1. Extend B1 settings and runtime registry with a LAN-validated `lan-p40-media` adapter using the same approved hostname, CIDR, CA, and runtime credential policy as `lan-localai-worker`.
2. Make the GPU executor choose ComfyUI and MuseTalk endpoints from each job's resolved runtime. Add `lan-p40-media` to startup reconciliation, lifecycle hooks, metrics, VRAM release verification, cancellation, and recovery.
3. Add P40 internal-only ComfyUI and MuseTalk services plus a small authenticated manager. Caddy exposes only `/media/*` to the B1 host. No backend port binds to the LAN and no container mounts the Docker socket.
4. Transfer pinned images and only the first-gate model views using partial staging, verify hashes, and publish read-only versioned views atomically.
5. Exercise P40 CUDA through a B1-held lease. Reject images or weights that fail real kernels on compute capability 6.1. Do not enable FP8/BF16-only paths.
6. Run the 1280x720 seated scene and scene-face lip-sync workflow through `https://api.ai.b1.germering`; capture job IDs, lease epochs, dimensions, hashes, timings, resource samples, and visual evidence.
7. Create new versioned DialectiCore profile variants referencing P40-only aliases. Preserve old profiles for rollback.

## Validation and acceptance

Unit tests must cover adapter configuration, private-LAN validation, runtime-specific endpoint selection, authenticated lifecycle calls, cancellation/recovery, and fail-closed runtime resolution. Compose configuration must validate on both hosts. Live acceptance requires B1 to hold the lease throughout every CUDA operation, no direct LAN backend listener, Laguna unload before media, chat acquisition at the next job boundary, media unload before Laguna, and a 1280x720 final scene with correct character placement and lip animation. Record output hashes and inspect representative frames, not only HTTP success.

## Rollback

Do not remove local B1 media services or old DialectiCore profiles. To roll back, stop only P40 media services, remove `lan-p40-media` from the new aliases, restart only the B1 control plane, and verify the original aliases still resolve to local `comfyui`/`lipsync`. P40 Caddy and Laguna configuration must remain independently valid. Model files are immutable; rollback changes views and aliases, not blobs.

## Outcomes

The scheduler-managed P40 media lane is production-enabled for native seated-character plates and MuseTalk, with CPU-only panel compositing. The supplied empty studio, six approved BiRefNet-matted characters, and approved six-seat panel now form a clean production scene. Public B1 cancellation, recovery, lease handoff, warm Laguna reuse, idle unload, restart recovery, artifact provenance, and internal-only exposure are verified. Establishing-wide and speaker-medium scene lipsync pass with ChatGPT. Full-episode rendering remains open because MuseTalk's internal face detector rejects Grok's dark robotic face; the failed clip is retryable and no invalid/static substitute was approved.
