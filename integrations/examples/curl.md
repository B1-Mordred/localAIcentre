# Curl Examples

List public aliases:

```bash
curl -s https://api.ai.b1.germering/v1/models
```

Create an asynchronous media job:

```bash
curl -s https://api.ai.b1.germering/v1/media/jobs \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $B1_API_KEY" \
  -d '{"modality":"image","operation":"generation","model":"image-default","input":{"prompt":"test"}}'
```

List Model Hub catalog entries:

```bash
curl -s https://models.ai.b1.germering/modelhub/v1/catalog
```
