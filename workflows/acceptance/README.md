# Acceptance Templates

The Compose `bootstrap` service copies these files once to:

```text
/srv/b1-ai-hub/workflows/acceptance/
```

Existing files in that external data-root directory are never overwritten. Edit the copied files on the target host to bind real model filenames, uploaded input names, published workflow IDs, and safe RTX 3060 parameters before running live acceptance.

`native-comfyui-smoke-prompt.json` uses the B1 runtime hook's deterministic tiny image node. It is useful for proving native ComfyUI REST, WebSocket, history, and `/view` compatibility without model weights. It is not valid final RTX GPU handoff evidence; the GPU acceptance harness rejects the `B1RuntimeTinyImage` prompt unless `B1_GPU_ACCEPTANCE_ALLOW_COMFY_TINY_SMOKE=1` is set for a labelled dry run, and the final report still marks that dry-run evidence incomplete.

`text-to-image-api-prompt.json`, `image-generation-job.json`, `image-edit-job.json`, and `short-video-job.json` are production acceptance templates. They must be adjusted to installed aliases, measured model versions, and approved workflows before handoff evidence is valid. The cross-runtime GPU evidence requires the ComfyUI template to produce a native prompt ID and at least one B1-stored artifact whose authenticated download bytes, SHA-256, MIME type, `ETag`, and range metadata verify successfully.
