from __future__ import annotations

from pathlib import Path


MIDDLEWARE_PATH = Path("/app/backend/open_webui/utils/middleware.py")
FUNCTION_ANCHOR = '''def get_reasoning_format(model: dict) -> str | None:
    """
    Determine how reasoning should be included in reconstructed messages.

    Returns:
        'think_tags': Ollama expects <think> tags in content.
        'reasoning_content': llama.cpp supports reasoning_content as a top-level field.
        None: skip reasoning (safe default for strict providers).
    """
    provider = model.get('provider', '')
    if provider == 'ollama':
        return 'think_tags'
    if provider == 'llama.cpp':
        return 'reasoning_content'
    return None
'''
FUNCTION_REPLACEMENT = '''def get_reasoning_format(model: dict) -> str | None:
    """
    Determine how reasoning should be included in reconstructed messages.

    Returns:
        'think_tags': Ollama expects <think> tags in content.
        'reasoning_content': llama.cpp or an explicitly capable OpenAI-compatible
            model supports reasoning_content as a top-level field.
        None: skip reasoning (safe default for strict providers).
    """
    provider = model.get('provider', '')
    if provider == 'ollama':
        return 'think_tags'
    if provider == 'llama.cpp':
        return 'reasoning_content'

    # B1 is exposed to OpenWebUI as an OpenAI-compatible provider. Its model
    # records retain the upstream capability object in the merged model, the
    # nested ``openai`` record, or OpenWebUI's internal ``info.meta`` record.
    # Honor only the explicit structured-reasoning contract so ordinary
    # OpenAI-compatible models keep the strict-provider behavior above.
    capability_sets = [model.get('capabilities')]
    upstream = model.get('openai')
    if isinstance(upstream, dict):
        capability_sets.append(upstream.get('capabilities'))
    info = model.get('info')
    if isinstance(info, dict):
        meta = info.get('meta')
        if isinstance(meta, dict):
            capability_sets.append(meta.get('capabilities'))
    if any(
        isinstance(capabilities, dict) and capabilities.get('reasoning_content') is True
        for capabilities in capability_sets
    ):
        return 'reasoning_content'
    return None
'''


def patch_reasoning_tool_loops(path: Path = MIDDLEWARE_PATH) -> None:
    source = path.read_text(encoding="utf-8")
    if FUNCTION_REPLACEMENT in source:
        return
    if FUNCTION_ANCHOR not in source:
        raise RuntimeError("Open WebUI reasoning-format anchor not found")
    path.write_text(source.replace(FUNCTION_ANCHOR, FUNCTION_REPLACEMENT, 1), encoding="utf-8")


if __name__ == "__main__":
    patch_reasoning_tool_loops()
