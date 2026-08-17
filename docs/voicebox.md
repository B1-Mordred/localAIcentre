# Voicebox

Voicebox is an optional managed runtime. It is routed at `https://voice.ai.b1.germering/` through the gateway while the backend remains internal.

The normal Voicebox compatibility host requires a B1 bearer token before the control plane proxies native HTTP or WebSocket traffic. Read-only native requests require `jobs:read`; mutating native requests require `jobs:write`. The control plane strips `Authorization`, cookies, CSRF/session headers, forwarded-client-IP headers, hop-by-hop headers, and WebSocket handshake headers before forwarding to the internal Voicebox proxy, so upstream Voicebox never receives B1 API keys or browser credentials.

Start the pinned production server/web/API build with:

```bash
docker compose -f compose.yaml -f compose.production-voicebox.yaml --profile voicebox up -d
```

Requirements for the pinned Voicebox integration:

- preserve native remote/server API and WebSocket behaviour
- register engines, models, and voice profiles in Control Center
- route GPU work through the global lease
- protect reference samples and cloned voices as sensitive user data
- expose compatible `/v1/audio/speech` semantics where validated

Current implementation status:

- queued `tts/speech` jobs resolved to `voicebox` acquire the global GPU lease, call internal `/v1/audio/speech`, and store returned audio as durable artifacts
- synchronous OpenAI-compatible `POST /v1/audio/speech` resolves the public alias, rewrites it to the immutable model ID, and proxies Voicebox requests under a scheduler lease
- native `POST https://voice.ai.b1.germering/v1/audio/speech` compatibility requests keep the client's upstream payload intact but now acquire the same global GPU lease and run the unload/VRAM-verify/load/warm path before reaching the internal Voicebox proxy
- synchronous and native Voicebox speech renew the scheduler lease while the upstream call is outstanding and fail with `scheduler_lease_lost` if the lease cannot be retained
- read-only native Voicebox HTTP, non-speech native routes, and native WebSocket traffic remain immediate compatibility passthroughs with B1 credentials stripped
- synchronous speech can use a B1 voice profile ID in `voice`, `voice_profile`, or `voice_profile_id`; if `model` is omitted, the active profile's `model_alias` selects the runtime/model
- queued Voicebox speech jobs revalidate the referenced profile before submission, then forward the same bounded profile envelope under the GPU lease
- empty speech responses are treated as `recovery_required`, not successful placeholder output
- administrators and operators can list, create, update, export, and soft-delete Voicebox profiles through Control Center and `/admin/voicebox/profiles`
- `GET /admin/voicebox/profile-policy` exposes the same safe upstream selector metadata contract used by server validation, including `upstream_voice_id`, `upstream_speaker_id`, `language`, `style`, and `speed`, so Control Center can render policy-driven fields while retaining the metadata JSON editor for reviewed engine-specific fields
- administrators and operators can upload bounded audio reference samples through Control Center or `POST /admin/voicebox/sample-artifacts`; Storage can dry-run and clean unreferenced Voicebox samples without touching profile-referenced files
- profile records are durable PostgreSQL rows included in the logical backup export; reference samples and cloned-voice material are stored only as artifact references, not inline profile payloads
- acceptance evidence ties each reference sample through upload, fetched profile state, export, and audit records by sample ID, internal URL, byte count, SHA-256, MIME type, and redacted audit metadata
- the production image includes a B1 proxy that forwards native REST/web/MCP HTTP and WebSocket traffic to loopback upstream Voicebox while exposing scheduler lifecycle hooks on `/b1/runtime/*`
- the B1 proxy exposes read-only `/b1/runtime/build-info` metadata with the proxy version, Jamie Pine Voicebox version, pinned commit, and source archive SHA-256 used by compatibility evidence
- authenticated `/b1/runtime/status` reports the managed upstream process, active native request count, supported lifecycle actions, pinned build metadata, and model inventory as counts only, so Control Center and update gates can verify Voicebox without leaking model paths
- for `/v1/audio/speech`, that proxy consumes the B1 profile envelope, strips B1-only fields, maps safe upstream selector metadata such as `upstream_voice`, and translates validated `/artifacts/voicebox/...` sample references to read-only in-container paths for engines that support reference or cloned voices
- Chatterbox voice cloning is exposed through the same path: a B1 profile with `engine: "chatterbox"`, `profile_type: "clone"`, and one or more sample artifacts is provisioned as a native Voicebox cloned profile and rendered via upstream `/generate/stream`
- the native profile mapping is kept in `$B1_DATA_ROOT/data/voicebox/b1-profile-map.json`, so remote Voicebox clients can also see and use the generated native cloned profile through `https://voice.ai.b1.germering/`
- native `POST /generate/stream` requests are serialized by the B1 proxy for Chatterbox stability; all six tested talkshow profiles return `200 audio/wav` under parallel caller load after proxy-side serialization
- native `POST /generate/stream` requests with `normalize: true` and `Accept: audio/wav` are post-processed by the B1 proxy into broadcast-ready mono PCM WAV by default: `48000` Hz, EBU R128 loudness normalization, and a true-peak limiter at or below `-1.5 dBTP`; `normalize: false` preserves the raw upstream Voicebox output
- target-host WebSocket and engine-specific speech compatibility are covered by the opt-in compatibility harness and must either pass against the pinned upstream route or record an explicit pinned-upstream limitation

The production override builds Jamie Pine Voicebox `v0.5.0` at commit `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`, exposes the B1 proxy on the internal native port `17493`, starts upstream Voicebox on loopback `127.0.0.1:17494`, and maps voice data/cache/model views plus the read-only Voicebox artifact namespace into B1-managed paths. The default Compose file still keeps the lightweight placeholder so `docker compose up -d` remains small. Runtime model downloads are disabled by default with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`; Model Hub must prepare compatible runtime views before enabling a Voicebox engine/profile.

The production image includes Debian `build-essential` because the pinned Chatterbox stack uses Triton and compiles CUDA helper modules on the first GPU generation. The compiler is present only inside the non-root Voicebox runtime image; user-facing APIs still cannot execute arbitrary shell commands, install packages, or mount host paths.

The B1 image also patches Chatterbox's Cangjie mapping lookup to prefer the verified local snapshot file before falling back to Hugging Face cache resolution. This keeps multilingual tokenizer startup offline when the runtime view contains `Cangjie5_TC.json`.

## Native Broadcast Audio

External Voicebox-compatible clients may keep using the native endpoint:

```http
POST https://voice.ai.b1.germering/generate/stream
Accept: audio/wav
Content-Type: application/json
Authorization: Bearer ...
```

The B1 proxy preserves native profile UUIDs, `engine: "chatterbox"`, and the normal `audio/wav` response. For production media, send:

```json
{
  "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
  "text": "Kurzer DialectiCore Broadcast-Audio-Test.",
  "language": "de",
  "engine": "chatterbox",
  "normalize": true,
  "effects_chain": []
}
```

Accepted B1 broadcast fields:

- `normalize`: boolean. When `true`, B1 performs broadcast post-processing after upstream Voicebox generation. This includes resampling, loudness normalization, and true-peak limiting. When `false`, B1 preserves raw upstream audio; upstream Voicebox may still interpret the field internally.
- `b1_broadcast_audio`: optional boolean. Overrides the B1 policy. `true` enables broadcast post-processing even if `normalize` is absent; `false` disables B1 post-processing.
- `b1_audio_sample_rate`: optional integer, default `48000`. B1 writes mono PCM WAV at this sample rate when broadcast post-processing is enabled.
- `b1_loudness_lufs`: optional number, default `-18.0`. This is passed to `ffmpeg loudnorm` as integrated loudness target.
- `b1_loudness_range_lu`: optional number, default `11.0`. This is passed to `ffmpeg loudnorm` as loudness range target.
- `b1_true_peak_dbtp`: optional number. B1 clamps this to the configured ceiling, default `-1.5`, so callers cannot request a hotter broadcast output than the appliance policy permits.

Implementation details:

- The proxy uses container-local `ffmpeg` with `aresample`, `loudnorm`, and `alimiter`.
- The response includes `x-b1-audio-policy: broadcast`, `x-b1-audio-sample-rate`, `x-b1-audio-channels`, `x-b1-audio-loudness-lufs`, and `x-b1-audio-true-peak-dbtp` when B1 post-processing is applied.
- When the final response body is a valid WAV, the response also includes `x-b1-generation-id`, `x-b1-audio-sha256`, and `x-b1-timing-url` so a client can retrieve timing metadata for the exact emitted bytes.
- If post-processing fails, B1 returns structured JSON with `reason: "broadcast_audio_postprocess_failed"` instead of returning raw non-conforming audio.
- Set `B1_VOICEBOX_BROADCAST_AUDIO_ENABLED=false` only for diagnostics. The production default is enabled.

## Native Timing Metadata

The existing audio-only route remains unchanged:

```http
POST https://voice.ai.b1.germering/generate/stream
Accept: audio/wav
Content-Type: application/json
Authorization: Bearer ...
```

For a successful WAV response, B1 stores timing metadata keyed by `x-b1-generation-id` and `x-b1-audio-sha256`. Retrieve it with either identifier:

```bash
curl --fail --cacert b1-ai-hub-caddy-root.crt \
  -H "Authorization: Bearer $B1_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"generation_id":"gen_..."}' \
  https://voice.ai.b1.germering/generate/timing
```

`GET /generate/timing/{generation_id}` is also available for clients that prefer the URL returned in `x-b1-timing-url`.

The timing payload schema is `b1_voice_timing.v1`:

```json
{
  "schema_version": "b1_voice_timing.v1",
  "generation_id": "gen_...",
  "audio_sha256": "...",
  "audio_bytes": 123456,
  "audio_sample_rate": 48000,
  "audio_channels": 1,
  "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
  "engine": "chatterbox",
  "language": "de",
  "duration_ms": 13580,
  "phoneme_alphabet": "ipa",
  "timing_method": "torchaudio-mms-fa",
  "timing_precision": "word-forced-aligned_phoneme-estimated",
  "alignment_model": "torchaudio.pipelines.MMS_FA",
  "alignment_device": "cpu",
  "word_timestamps": [
    {"word": "Guten", "start_ms": 120, "end_ms": 430, "confidence": 0.91}
  ],
  "character_timestamps": [
    {"character": "g", "start_ms": 120, "end_ms": 160, "word_index": 0, "confidence": 0.90}
  ],
  "phoneme_timestamps": [
    {"phoneme": "ɡ", "start_ms": 120, "end_ms": 160, "word_index": 0, "confidence": 0.50}
  ]
}
```

The checksum is computed over the exact bytes emitted by B1 after broadcast post-processing. With `normalize: true`, this means the metadata belongs to the 48 kHz broadcast WAV, not the raw upstream Voicebox audio.

B1 uses TorchAudio's multilingual `MMS_FA` forced-alignment bundle when its acoustic checkpoint is available in the Voicebox Torch cache. The aligner runs on CPU by default (`B1_VOICEBOX_TIMING_MMS_ALIGNER_DEVICE=cpu`) to avoid taking additional VRAM from Chatterbox on the 6 GB GPU. Set `B1_VOICEBOX_TIMING_MMS_ALIGNER_ENABLED=false` to disable it, or `B1_VOICEBOX_TIMING_MMS_ALIGNER_DEVICE=cuda` only after GPU memory has been measured.

With MMS_FA enabled, `word_timestamps` and `character_timestamps` are CTC forced-aligned to the final WAV. The `phoneme_timestamps` remain IPA values from eSpeak/phonemizer distributed inside each aligned word window, because the pinned Chatterbox/Voicebox stack does not expose native acoustic phoneme alignment. If the MMS_FA model is missing or alignment fails, B1 falls back to `timing_method: "b1-proportional-ipa-estimate"` and `timing_precision: "estimated"`.

The MMS_FA checkpoint is downloaded from TorchAudio's configured public URL and should be staged into the persistent Voicebox Torch cache before production use:

```bash
install -d /srv/b1-ai-hub/cache/voicebox/xdg/torch/hub/checkpoints
curl --fail --location \
  https://dl.fbaipublicfiles.com/mms/torchaudio/ctc_alignment_mling_uroman/model.pt \
  --output /srv/b1-ai-hub/cache/voicebox/xdg/torch/hub/checkpoints/model.pt
sha256sum /srv/b1-ai-hub/cache/voicebox/xdg/torch/hub/checkpoints/model.pt
```

Validated checkpoint SHA-256 on 2026-07-30: `20ef12963ab4924bef49ac4fc7f58ad5da2ee43b2c11bc8c853c9b90ecdbc680`.

This is a local runtime model, not a cloud API. Once cached, timing generation does not send audio or text outside the LAN.

For the pinned Chatterbox backend, cache the required `ResembleAI/chatterbox` files under the Voicebox Hugging Face cache before first use:

```text
$B1_DATA_ROOT/cache/voicebox/huggingface/hub/models--ResembleAI--chatterbox/
├── refs/main
└── snapshots/5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18/
    ├── Cangjie5_TC.json
    ├── conds.pt
    ├── grapheme_mtl_merged_expanded_v1.json
    ├── s3gen.pt
    ├── t3_mtl23ls_v2.safetensors
    └── ve.pt
```

Upstream Voicebox sets `VOICEBOX_MODELS_DIR` as Hugging Face's active `HF_HUB_CACHE`, so Model Hub must also publish that same cache layout into the read-only runtime view mounted at `/srv/b1-ai-hub/models`:

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

The multilingual tokenizer path also requires `spacy_pkuseg`'s `spacy_ontonotes` data offline. Store the verified archive at `$B1_DATA_ROOT/data/voicebox/.pkuseg/spacy_ontonotes.zip`, extract it to `$B1_DATA_ROOT/data/voicebox/.pkuseg/spacy_ontonotes/`, and verify SHA-256 `b216e7f92de7ae285aeab8feba2faa8ea8216e5995ff6fb3d391cc8356db1bfe`.

The lifecycle hooks are conservative. `status` reports process/request/model-count readiness without returning filenames or paths. `load` checks model files/directories visible to Voicebox and returns `unconfirmed` because upstream engine/profile selection performs final validation. `warm` and `smoke` do not synthesize audio unless explicitly enabled with `B1_VOICEBOX_HOOK_WARM_ENABLED=true` or `B1_VOICEBOX_HOOK_SMOKE_ENABLED=true`. `unload` restarts the loopback upstream process only when the proxy has no active native requests, giving the GPU scheduler a bounded cleanup mechanism without terminating the container.

Do not add `voicebox` to `B1_RUNTIME_PRODUCTION_REQUIRED` until the selected engine/profile has passed target-host smoke tests, voice profile backup/export/delete validation, and GPU lease overlap checks against LocalAI and ComfyUI.

Run the remote/server compatibility harness during target-host acceptance:

```bash
B1_VOICEBOX_LIVE_TEST=1 \
B1_VOICEBOX_BASE=https://voice.ai.b1.germering \
B1_VOICEBOX_API_BASE=https://api.ai.b1.germering \
B1_VOICEBOX_API_KEY=... \
B1_VOICEBOX_SPEECH_MODEL=tts-quality \
B1_VOICEBOX_EXPECTED_VERSION=v0.5.0 \
B1_VOICEBOX_EXPECTED_COMMIT=2bcb98d1a8b6fe05e15fbc1559e3085669e4035d \
B1_VOICEBOX_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/voicebox-remote.json \
make voicebox-compatibility
```

If pinned upstream Voicebox lacks a stable remote WebSocket route or compatible speech mode for the selected profile, set `B1_VOICEBOX_SKIP_WEBSOCKET=1` with `B1_VOICEBOX_WEBSOCKET_LIMITATION=...` or `B1_VOICEBOX_SKIP_SPEECH=1` with `B1_VOICEBOX_SPEECH_LIMITATION=...`. Those limitation strings become machine-readable acceptance evidence only when they are tied to the same proxy version, upstream repository/version/commit, and source archive SHA-256 returned by `/b1/runtime/build-info`; missing models, unhealthy containers, or credentials are not valid upstream limitations.
