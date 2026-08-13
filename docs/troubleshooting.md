# Troubleshooting

## Maintenance 503

If inference, media upload, media job, native ComfyUI prompt, runtime reservation, or job retry requests return HTTP 503 with `maintenance mode is active`, open Control Center System and disable maintenance mode after the update or backup window is complete. Queued CPU/GPU jobs and model downloads also remain queued while maintenance is active. `GET /admin/maintenance` shows the persisted reason and last update time.

## CPU Audio Engine Unavailable

If `tts-fast`, `stt-default`, or `embedding-default` returns HTTP 503 with `code=b1_audio_cpu_engine_unavailable`, the bundled scaffold CPU engine is disabled by policy or no production engine is configured. This is expected when `B1_CPU_AUDIO_ENABLE_PLACEHOLDER=false`.

For real CPU TTS, keep the default `B1_INSTALL_PIPER=true` build argument or provide an equivalent derived image, set `B1_CPU_AUDIO_ENGINE=piper`, install a Piper voice such as the seeded `b1-piper-en-us-amy-low` recommendation through Model Hub, and route `tts-fast` to the installed manifest. The default image installs rhasspy/piper `2023.11.14-2` to `/opt/piper/piper` after verifying SHA-256 `a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992`; Piper model weights are still operator-managed model artifacts and are not bundled. `GET /admin/runtimes` or `GET /healthz` on the internal service reports `details.reason` values such as `piper_binary_missing`, `model_path_missing`, `model_runtime_view_missing`, `model_onnx_missing`, `model_onnx_ambiguous`, or `model_path_outside_allowed_root`. `B1_PIPER_MODEL_PATH` is only a static fallback for one manually selected voice. Piper mode enables speech only.

For real CPU embeddings, set `B1_CPU_EMBEDDING_ENGINE=onnx`, install the seeded `b1-minilm-l6-v2-onnx-q4` recommendation through Model Hub, and route `embedding-default` to the installed manifest. The default image pins `onnxruntime==1.27.0`, `tokenizers==0.23.1`, and `numpy==2.5.1`; model weights and tokenizer files are still operator-managed artifacts and are not bundled. Runtime health reports embedding reasons such as `embedding_onnx_missing`, `embedding_onnx_ambiguous`, `embedding_tokenizer_missing`, `embedding_tokenizer_ambiguous`, or `missing_dependencies:numpy,onnxruntime,tokenizers`. ONNX embedding mode enables only embeddings.

For real CPU STT, set `B1_CPU_STT_ENGINE=vosk`, install the seeded `b1-vosk-small-en-us-0.15` recommendation through Model Hub, and route `stt-default` to the installed manifest. The default image pins `vosk==0.3.45`; model weights are still operator-managed artifacts and are not bundled. Runtime health reports STT reasons such as `vosk_model_files_missing`, `vosk_model_ambiguous`, `model_runtime_view_missing`, `model_path_outside_allowed_root`, or `missing_dependencies:vosk`. The Vosk seed is a zip archive; Model Hub verifies SHA-256 `30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498` and safely extracts it into the read-only runtime view. The first STT runtime accepts mono signed 16-bit PCM WAV only. `b1_audio_cpu_wav_channels_unsupported`, `b1_audio_cpu_wav_sample_width_unsupported`, `b1_audio_cpu_wav_sample_rate_unsupported`, `b1_audio_cpu_audio_too_large`, and `b1_audio_cpu_audio_duration_too_large` are input-contract failures, not scheduler failures. Convert other audio containers before submission or use the async upload path once a future decoder stage is added.

## Update Plan Blocked

If update staging, health-check, or rollback returns HTTP 409, enable maintenance mode first. If update creation returns HTTP 422, check that every image reference includes `@sha256:<64 hex chars>`, no image uses `latest`, each service appears only once, and the optional release URL is HTTPS without embedded credentials.

If update staging returns `stage_failed`, inspect the update row's `image_stage` entries in Control Center or `GET /admin/updates/{id}`. `dry_run` is expected when `B1_ENABLE_MUTATIONS=false`; `failed` means runtime-agent rejected the service/image or Docker could not pull the pinned reference. Confirm the service is in `B1_ALLOWED_SERVICES`, the image uses an immutable digest, the runtime-agent token and mTLS certificate files are mounted under `/run/secrets`, and the host can reach the registry during the maintenance window.

## GPU Memory Not Released

Check:

```bash
nvidia-smi
sudo fuser -v /dev/nvidia*
```

Use Control Center Runtimes -> Unload or Recover for the affected B1 runtime. The current forced-unload strategy is a bounded restart through the runtime-agent and is dry-run unless `B1_ENABLE_MUTATIONS=true`. During migration, avoid manually killing unrelated services.

If a GPU media job fails with `gpu_runner_error` after `verifying_vram_recovering`, runtime-agent reported VRAM above the configured reserve after the recovery action. On the target host, confirm `B1_ENABLE_MUTATIONS=true` only during a controlled maintenance window, then rerun the Control Center recovery action and verify `nvidia-smi` before submitting new GPU work.

## Compose Validation

```bash
make bootstrap
docker compose config
```

## Quality Container DNS

If `make repository-quality-evidence` fails during `backend-python-quality-container` with pip errors such as `Temporary failure in name resolution` while the host itself can resolve and download packages, test Docker bridge DNS:

```bash
docker run --rm python:3.12.12-slim-bookworm getent hosts pypi.org
docker run --rm --network host python:3.12.11-slim-bookworm getent hosts pypi.org
```

When bridge containers can resolve LAN records but not recursive external names, rerun the quality evidence with host networking for the quality container only:

```bash
make repository-quality-evidence B1_QUALITY_DOCKER_RUN_ARGS="--network host"
```

Do not change the production Compose networks to work around this. Production runtime containers should remain on the documented internal networks.

## TLS Trust

If browsers reject `*.ai.b1.germering`, install the Caddy internal CA root on the LAN client.

## Backend Ports

Only the gateway should be exposed. Confirm backend services are internal:

```bash
docker compose ps
ss -ltnp
```

## Blob Quarantine Blocked

Use the Models tab to inspect the blob quarantine plan after a model record has already been quarantined. A blocked plan means the control plane found an active job reference, a shared blob used by another model record, a missing or non-file blob path, a symlink, a size/SHA-256 mismatch, or an existing quarantine destination. Resolve the listed blocker and run the plan again before applying cleanup.
