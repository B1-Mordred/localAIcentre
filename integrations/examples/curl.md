# Curl Examples

List public aliases:

```bash
curl -s https://api.ai.b1.germering/v1/models
```

Create an asynchronous media job. The control plane returns HTTP `202 Accepted` with a durable job record and relative `links` for polling, events, cancellation, and artifacts:

```bash
curl -sS -o /tmp/b1-media-job.json -w 'HTTP %{http_code}\n' \
  https://api.ai.b1.germering/v1/media/jobs \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $B1_API_KEY" \
  -d '{"modality":"image","operation":"generation","model":"image-default","input":{"prompt":"test"}}'

jq . /tmp/b1-media-job.json
```

Follow the returned links instead of constructing job paths by hand:

```bash
JOB_URL="$(jq -r '.links.self' /tmp/b1-media-job.json)"
EVENTS_URL="$(jq -r '.links.events' /tmp/b1-media-job.json)"
ARTIFACTS_URL="$(jq -r '.links.artifacts' /tmp/b1-media-job.json)"

curl -s "https://api.ai.b1.germering${JOB_URL}" \
  -H "Authorization: Bearer $B1_API_KEY"

curl -N "https://api.ai.b1.germering${EVENTS_URL}" \
  -H "Authorization: Bearer $B1_API_KEY"

curl -s "https://api.ai.b1.germering${ARTIFACTS_URL}" \
  -H "Authorization: Bearer $B1_API_KEY"
```

List Model Hub catalog entries:

```bash
curl -s https://models.ai.b1.germering/modelhub/v1/catalog
```
