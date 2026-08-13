# Voicebox Deployment

This directory contains the B1-managed Jamie Pine Voicebox production image build. There is no published upstream image with immutable base/runtime pins and the B1 Compose hardening contract, so B1 builds from the upstream source archive and patches only dependency references needed for reproducible Git checkouts. The final image also patches Chatterbox's Cangjie mapping lookup to use the verified local snapshot file directly before falling back to Hugging Face cache resolution, avoiding an offline-runtime warning caused by upstream passing the snapshot directory as `cache_dir`. Upstream backend dependencies use broad version ranges; `constraints.txt` records the resolved PyPI dependency graph from the validated B1 build.

Pinned upstream:

```text
jamiepine/voicebox v0.5.0
commit: 2bcb98d1a8b6fe05e15fbc1559e3085669e4035d
source archive SHA-256: d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083
frontend base: oven/bun:1.3.8@sha256:371d30538b69303ced927bb5915697ac7e2fa8cb409ee332c66009de64de5aa3
backend/runtime base: python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93
Qwen3-TTS commit: 022e286b98fbec7e1e916cb940cdf532cd9f488e
LinaCodec commit: c0ae7c7285e121475c27592cfbb600624b714290
LuxTTS commit: 28ae6a61151684fffc9d1a7aa15eafa02286fe0b
```

Start it with the optional production profile:

```bash
docker compose -f compose.yaml -f compose.production-voicebox.yaml --profile voicebox up -d
```

Voicebox remains internal. External clients use `https://voice.ai.b1.germering/` through Caddy and B1 access policy; raw `17493` is not published.

The production image starts a B1 proxy on `:17493` and starts upstream Voicebox on loopback `127.0.0.1:17494` by default. The proxy forwards native REST, web, MCP HTTP, and WebSocket traffic to upstream Voicebox while handling B1 scheduler lifecycle routes itself. This avoids patching upstream Voicebox source and keeps raw upstream traffic inside the container.

The proxy also serves read-only `GET /b1/runtime/build-info` with the B1 proxy version, Jamie Pine Voicebox version, pinned upstream commit, and source archive SHA-256. Authenticated `POST /b1/runtime/build-info` and `POST /b1/runtime/status` provide the same scheduler-facing lifecycle contract used by LocalAI and ComfyUI. The status payload reports the managed upstream process, active native request count, lifecycle actions, pinned build metadata, and model inventory as counts only. The Voicebox compatibility harness records these live endpoints and Control Center acceptance reports reject speech/WebSocket limitation evidence that does not match the deployed proxy metadata.

For native `POST /generate/stream`, the proxy serializes generation requests before forwarding them to upstream Voicebox. This protects the pinned Chatterbox backend from concurrent generation crashes while preserving the external native route and profile UUID contract.

Successful WAV stream responses include `x-b1-generation-id`, `x-b1-audio-sha256`, and `x-b1-timing-url` when B1 can parse the final audio. `POST /generate/timing` accepts either `generation_id` or `audio_sha256` and returns the stored `b1_voice_timing.v1` JSON payload. It can also generate timing from a full native generation payload, but clients that need exact media binding should call `/generate/stream` first and retrieve timing with the returned generation ID or checksum.

The proxy uses TorchAudio `MMS_FA` for real local CTC word and character alignment when `/srv/b1-ai-hub/cache/voicebox/xdg/torch/hub/checkpoints/model.pt` exists. It runs on CPU by default via `B1_VOICEBOX_TIMING_MMS_ALIGNER_DEVICE=cpu`; set `B1_VOICEBOX_TIMING_MMS_ALIGNER_ENABLED=false` to force the proportional fallback. IPA phonemes are generated with eSpeak/phonemizer and placed inside aligned word windows because upstream Chatterbox does not expose native phoneme timing.

The validated MMS_FA checkpoint SHA-256 on 2026-07-30 is `20ef12963ab4924bef49ac4fc7f58ad5da2ee43b2c11bc8c853c9b90ecdbc680`.

## Runtime Layout

The production override resets the development placeholder environment and volume list. The Voicebox container receives only:

- `/srv/b1-ai-hub/voicebox`, backed by `$B1_DATA_ROOT/data/voicebox`, writable
- `/srv/b1-ai-hub/models`, backed by `$B1_DATA_ROOT/models/runtime-views/voicebox`, read-only
- `/srv/b1-ai-hub/artifacts/voicebox`, backed by `$B1_DATA_ROOT/artifacts/voicebox`, read-only
- `/srv/b1-ai-hub/cache`, backed by `$B1_DATA_ROOT/cache/voicebox`, writable
- `/tmp`, a Compose tmpfs inherited from the service defaults

Voice profiles, captures, generated audio, and Voicebox's SQLite database are sensitive user data and belong under `$B1_DATA_ROOT/data/voicebox`, not in Git. Voice reference samples are stored as B1 artifacts and mounted into the Voicebox container as read-only paths only under `/srv/b1-ai-hub/artifacts/voicebox`. Model weights remain controlled by B1 Model Hub; Voicebox receives only the runtime-specific read-only model view.

The image defaults to `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, so it does not silently download weights at runtime. Model Hub must prepare a Voicebox-compatible runtime view before a profile or engine is enabled.

The final runtime image includes Debian `build-essential` because Chatterbox uses Triton and compiles CUDA helper modules during the first GPU generation. This is not used for arbitrary package installation or user-provided code; the container still runs as the non-root `b1` user and only exposes the bounded B1 proxy API.

## Scheduler Contract

The control plane is still the only service that may submit managed inference work. GPU Voicebox jobs are routed through the global lease and use `VOICEBOX_URL=http://voicebox:17493` when this override is active.

The B1 proxy implements these runtime hooks:

- `GET /b1/runtime/build-info`
- `GET /b1/runtime/status`
- `POST /b1/runtime/build-info`
- `POST /b1/runtime/status`
- `POST /b1/runtime/load`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`

Production Compose mounts `$B1_DATA_ROOT/secrets/runtime_control_token` read-only and sets `B1_RUNTIME_CONTROL_REQUIRE_AUTH=true`. The control plane sends this token as `Authorization: Bearer ...` for all `/b1/runtime/*` hook calls. If the token is missing or wrong, the proxy rejects lifecycle actions before touching the managed Voicebox process.

Hook behavior is intentionally bounded:

- `load` checks Voicebox-visible runtime model roots and returns `unconfirmed` because upstream engine/profile selection performs final validation and lazy model loading.
- `status` reports the upstream process status, active native request count, supported lifecycle actions, and model-root/file counts without returning model filenames or paths.
- `warm` returns `unconfirmed` unless `B1_VOICEBOX_HOOK_WARM_ENABLED=true`.
- `smoke` returns `unconfirmed` unless `B1_VOICEBOX_HOOK_SMOKE_ENABLED=true`.
- enabled `warm`/`smoke` send a small `/v1/audio/speech` probe to upstream using `B1_VOICEBOX_HOOK_SMOKE_TEXT`, `B1_VOICEBOX_HOOK_SMOKE_VOICE`, and `B1_VOICEBOX_HOOK_SMOKE_ENDPOINT`; enable this only after the selected engine/profile has safe smoke parameters.
- `unload` refuses to restart while native proxy requests are active; when idle and `B1_VOICEBOX_HOOK_RESTART_ON_UNLOAD=true`, it restarts the loopback upstream process to release model memory.

Use `B1_VOICEBOX_HOOK_STRICT_MODEL_LIST=true` only when installed manifests resolve to filenames or directories visible under `B1_VOICEBOX_HOOK_MODEL_ROOTS`. Production acceptance still requires measured smoke tests for the selected engine/profile on `ai.b1.germering`, including profile backup/export/delete and verification that a GPU Voicebox request cannot overlap with LocalAI or ComfyUI.

## B1 Voice Profiles

The control plane accepts B1-managed voice profiles in synchronous `/v1/audio/speech` and queued `tts/speech` media jobs. Callers can pass a profile ID as `voice`, `voice_profile`, or `voice_profile_id`. If `model` is omitted from synchronous speech, the profile's `model_alias` selects the runtime/model. The profile must be active, visible to the caller, and bound to the resolved runtime and alias before work is submitted.

For Voicebox profiles, the control plane forwards a bounded `b1_voice_profile` envelope to this proxy. The envelope contains the profile ID, engine, profile type, model alias, safe upstream selector metadata, and sample artifact references/checksums. It never contains inline sample bytes or arbitrary metadata. The proxy consumes that envelope before calling upstream Voicebox: safe metadata such as `upstream_voice`, `upstream_speaker_id`, `language`, `style`, or `speed` is mapped to native fields, B1-only fields are stripped, and read-only `/artifacts/voicebox/...` sample references are translated to in-container paths.

`chatterbox` is the default B1 clone engine for Voicebox profiles. For `POST /v1/audio/speech` with a B1 profile envelope, the proxy provisions or reuses a native Voicebox cloned profile, uploads the mounted reference sample files to that native profile, and calls upstream `/generate/stream` with `engine: "chatterbox"`. The B1-to-native profile mapping is stored in `$B1_DATA_ROOT/data/voicebox/b1-profile-map.json`. Native remote Voicebox clients can still use the same server through `https://voice.ai.b1.germering/` with Voicebox's `/profiles`, `/profiles/{id}/samples`, `/generate`, `/generate/stream`, `/speak`, MCP, and WebSocket routes.

Chatterbox runtime weights must be present before the first offline generation because production sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. The pinned Voicebox backend uses `ResembleAI/chatterbox` revision `5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18` and requires only:

- `ve.pt`
- `t3_mtl23ls_v2.safetensors`
- `s3gen.pt`
- `grapheme_mtl_merged_expanded_v1.json`
- `conds.pt`
- `Cangjie5_TC.json`

Place those files under `$B1_DATA_ROOT/cache/voicebox/huggingface/hub/models--ResembleAI--chatterbox/snapshots/5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18/` and write the same revision into `.../refs/main`. This mirrors Hugging Face's local cache layout and allows runtime model loading without network access.

Upstream Voicebox assigns `VOICEBOX_MODELS_DIR` to Hugging Face's `HF_HUB_CACHE`, so the same Hugging Face cache shape must also be visible inside the read-only Voicebox runtime model view mounted at `/srv/b1-ai-hub/models`. Model Hub should publish hardlinks or copied verified files under:

```text
$B1_DATA_ROOT/models/runtime-views/voicebox/models--ResembleAI--chatterbox/
├── refs/main
└── snapshots/5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18/
    ├── Cangjie5_TC.json
    ├── conds.pt
    ├── grapheme_mtl_merged_expanded_v1.json
    ├── s3gen.pt
    ├── t3_mtl23ls_v2.safetensors
    └── ve.pt
```

The multilingual Chatterbox tokenizer also needs `spacy_pkuseg`'s `spacy_ontonotes` model available offline. Store the verified archive at `$B1_DATA_ROOT/data/voicebox/.pkuseg/spacy_ontonotes.zip`, extract it to `$B1_DATA_ROOT/data/voicebox/.pkuseg/spacy_ontonotes/`, and verify SHA-256 `b216e7f92de7ae285aeab8feba2faa8ea8216e5995ff6fb3d391cc8356db1bfe`.

The sample path mapping is controlled by:

- `B1_VOICEBOX_ARTIFACT_ROOT`, defaulting to `/srv/b1-ai-hub/artifacts`
- `B1_VOICEBOX_PROFILE_MAP_FILE`, defaulting to `/srv/b1-ai-hub/voicebox/b1-profile-map.json`
- `B1_VOICEBOX_FORWARD_SAMPLE_PATHS`, defaulting to `true`
- `B1_VOICEBOX_REQUIRE_SAMPLE_PATH_EXISTS`, defaulting to `true`
- `B1_VOICEBOX_DEFAULT_LANGUAGE`, defaulting to `en`
- `B1_VOICEBOX_DEFAULT_REFERENCE_TEXT`, defaulting to `B1 voice reference sample`
- `B1_VOICEBOX_SAMPLE_FIELD`, defaulting to `reference_audio_path`
- `B1_VOICEBOX_SAMPLE_LIST_FIELD`, defaulting to `reference_audio_paths`

Adjust the sample field names only if the pinned upstream Voicebox route for the selected engine expects different JSON keys. Unsafe artifact URLs, traversal, malformed percent escapes, and missing mounted samples are rejected with HTTP 422 before upstream Voicebox sees the request.

## Broadcast Audio Contract

The native Voicebox stream route returns raw upstream audio unless B1 broadcast post-processing is enabled for the request. Production clients should use:

```json
{
  "profile_id": "native-voicebox-profile-uuid",
  "text": "Kurzer Broadcast-Audio-Test.",
  "language": "de",
  "engine": "chatterbox",
  "normalize": true,
  "effects_chain": []
}
```

With `Accept: audio/wav`, `normalize: true` enables B1 post-processing by default. The proxy returns mono PCM WAV at `48000` Hz, applies `ffmpeg loudnorm`, and applies an `alimiter` ceiling at the configured true-peak policy, default `-1.5 dBTP`. `normalize: false` keeps the upstream output unchanged. Optional B1-specific fields are `b1_broadcast_audio`, `b1_audio_sample_rate`, `b1_loudness_lufs`, `b1_loudness_range_lu`, and `b1_true_peak_dbtp`; requested true-peak values hotter than the configured ceiling are clamped.

Relevant environment defaults:

- `B1_VOICEBOX_BROADCAST_AUDIO_ENABLED=true`
- `B1_VOICEBOX_BROADCAST_SAMPLE_RATE=48000`
- `B1_VOICEBOX_BROADCAST_LOUDNESS_LUFS=-18.0`
- `B1_VOICEBOX_BROADCAST_LRA_LU=11.0`
- `B1_VOICEBOX_BROADCAST_TRUE_PEAK_DBTP=-1.5`
- `B1_VOICEBOX_BROADCAST_FFMPEG_TIMEOUT_SECONDS=120`

When post-processing is applied, responses include `x-b1-audio-policy: broadcast` plus headers describing sample rate, channels, loudness target, and true-peak target. Post-processing failures return structured JSON with `reason: "broadcast_audio_postprocess_failed"` instead of returning raw non-conforming speech.

Control Center uploads reference samples through `POST /admin/voicebox/sample-artifacts`, which writes bounded audio files under `/artifacts/voicebox/references/...` and returns a profile-ready `sample_artifacts[]` entry with sample ID, URL, SHA-256, MIME type, and byte count. Compatibility evidence follows that entry through fetched profile state, export, and upload/export/delete audit events; audit metadata must identify the target sample/profile and remain free of raw sample arrays. Storage cleanup for these samples uses `POST /admin/voicebox/sample-artifacts/retention-plan` and `POST /admin/voicebox/sample-artifacts/cleanup`; it only deletes old unreferenced files and preserves every sample still referenced by a Voicebox profile row, including deleted rows retained for recovery.
