# Voicebox Deployment

This directory will hold the pinned Jamie Pine Voicebox integration once a tested upstream release is selected.

Voice profiles and reference samples are sensitive user data and belong under `$B1_DATA_ROOT/data/voicebox`, not in Git.

The GPU runner now receives `VOICEBOX_URL` and can submit Voicebox TTS jobs to `/v1/audio/speech` under the global GPU lease. It may also call optional internal B1 runtime hooks before submission:

- `POST /b1/runtime/load`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`

The placeholder service implements these hooks for smoke tests. The pinned Voicebox integration must document which hooks are supported and which voice/model profiles can run under CPU residency without taking the GPU lease.
