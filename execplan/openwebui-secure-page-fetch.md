# Upgrade OpenWebUI and add controlled page fetching

## Outcome

Upgrade the B1 custom OpenWebUI image from 0.10.2 to 0.11.0, retain the B1
cancellation bridge, and allow full-page retrieval without granting OpenWebUI or
model runtimes direct network egress.

## Progress

- [x] Inventory the live Compose selection, image, networks, resources, and custom patches.
- [x] Confirm OpenWebUI 0.11.0 Firecrawl v2 integration and security fixes.
- [x] Design a private Firecrawl stack with a sole filtered-egress proxy.
- [x] Build and run unit/Compose policy tests.
- [x] Deploy Firecrawl without interrupting OpenWebUI.
- [x] Replace OpenWebUI and verify health, cancellation, search, public fetch, and private-address denial.

## Validation

- Custom OpenWebUI image: `sha256:be2e3adbdc8f2be81257fe4ee3048564a63810e07d9d5691a95db61a221a8c8a`.
- Live OpenWebUI: 0.11.0, upstream revision `f9590b8017199e56d5e953657e6498e3cef1d246`, healthy, `app` network only.
- Public page: OpenWebUI's Firecrawl client returned parsed Markdown and title for `https://example.com`.
- Private page: `http://192.168.2.103` was rejected by Squid with HTTP 403 and Firecrawl failed the scrape.
- Public routes: `https://ai.b1.germering/health` returned true; the authenticated B1 model endpoint returned 37 models.
- Cancellation patch: import and invocation are present in live `/app/backend/open_webui/tasks.py`.
- Exposure: no Firecrawl, Playwright, proxy, cache, queue, or database host ports.
- Idle memory snapshot: OpenWebUI 683 MiB; Firecrawl 2.75 GiB; Playwright 105 MiB; dependencies and proxy 228 MiB combined.
- Policy suite: 56 passed; two pre-existing unrelated dirty-tree failures remain for LocalAI root execution and the lipsync runtime-agent expectation.

## Decisions

- Keep SearXNG as the search engine; Firecrawl is the page loader only.
- Pin OpenWebUI 0.11.0 by its amd64 manifest digest.
- Pin the Firecrawl images by immutable amd64 digests and record upstream v2.11.0 commit `ef12eb36b2f3382838dfe0a0c1a5add3d5df7fe5` because numbered release images are not published.
- Do not publish Firecrawl, browser, proxy, queue, cache, or database ports.
- Force Firecrawl HTTP and Playwright traffic through Squid; deny non-public destination ranges after proxy-side DNS resolution.
- Limit fetch content to 200,000 characters and concurrency to two jobs.

## Rollback

Restore the prior OpenWebUI Dockerfile base digest, remove the loader environment
variables, and recreate only `open-webui`. Stop only the five `tool-firecrawl*`
and `tool-web-egress` services. The existing OpenWebUI data volume is retained.
