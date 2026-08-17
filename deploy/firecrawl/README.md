# B1 private Firecrawl page loader

Open WebUI keeps search on the internal SearXNG service and sends selected result
URLs to `tool-firecrawl`. Firecrawl and its Playwright worker are attached only to
the internal `web-fetch` network. Their configured proxy, `tool-web-egress`, is
the sole service attached to the normal egress network and rejects private,
loopback, link-local, tailnet, Docker, multicast, and documentation ranges.

The deployment uses Firecrawl v2.11.0 source commit
`ef12eb36b2f3382838dfe0a0c1a5add3d5df7fe5` and immutable amd64 image digests.
The Firecrawl API, dependency services, and proxy publish no host ports.

Validate with:

```bash
docker compose config --quiet
docker compose ps tool-firecrawl tool-firecrawl-playwright tool-web-egress
docker compose exec open-webui python -c "import requests; print(requests.post('http://tool-firecrawl:3002/v2/scrape', json={'url':'https://example.com','formats':['markdown']}, timeout=90).json()['success'])"
```

Private-address probes must fail, and `open-webui` must remain attached only to
the internal `app` network.
