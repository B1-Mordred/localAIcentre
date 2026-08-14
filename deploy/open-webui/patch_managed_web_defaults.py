from __future__ import annotations

from pathlib import Path


MIDDLEWARE_PATH = Path("/app/backend/open_webui/utils/middleware.py")
FEATURES_ANCHOR = "    features = form_data.pop('features', None) or {}\n"
MANAGED_WEB_DEFAULT = '''    # B1 exposes search/fetch through private SearXNG and Firecrawl services.
    # Make that capability available in every browser chat; the model still
    # decides whether a web tool is relevant to the current turn.
    if os.getenv('B1_OPEN_WEBUI_DEFAULT_WEB_ACCESS', 'true').strip().lower() in {'1', 'true', 'yes', 'on'}:
        features['web_search'] = True
'''


def patch_managed_web_defaults(path: Path = MIDDLEWARE_PATH) -> None:
    source = path.read_text(encoding="utf-8")
    if MANAGED_WEB_DEFAULT in source:
        return
    if FEATURES_ANCHOR not in source:
        raise RuntimeError("Open WebUI features anchor not found")
    path.write_text(
        source.replace(FEATURES_ANCHOR, FEATURES_ANCHOR + MANAGED_WEB_DEFAULT, 1),
        encoding="utf-8",
    )


if __name__ == "__main__":
    patch_managed_web_defaults()
