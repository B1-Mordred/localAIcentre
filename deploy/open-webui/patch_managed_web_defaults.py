from __future__ import annotations

from pathlib import Path


MIDDLEWARE_PATH = Path("/app/backend/open_webui/utils/middleware.py")
FEATURES_ANCHOR = "    features = form_data.pop('features', None) or {}\n"
MANAGED_WEB_DEFAULT = '''    # B1 exposes search/fetch through private SearXNG and Firecrawl services.
    # Laguna's pinned GLM template cannot preserve structured reasoning while
    # native tool definitions are present. Let B1's managed current-information
    # policy execute search/fetch for that template; all other models retain
    # OpenWebUI's native streaming tools.
    model_capabilities = (model.get('info', {}).get('meta', {}).get('capabilities') or {})
    use_b1_managed_web_fallback = (
        model_capabilities.get('chat_template') == 'laguna_glm_thinking_v8'
        and model_capabilities.get('reasoning_content') is True
    )
    if use_b1_managed_web_fallback:
        features.pop('web_search', None)
    elif os.getenv('B1_OPEN_WEBUI_DEFAULT_WEB_ACCESS', 'true').strip().lower() in {'1', 'true', 'yes', 'on'}:
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
