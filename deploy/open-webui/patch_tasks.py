from __future__ import annotations

from pathlib import Path


TASKS_PATH = Path("/app/backend/open_webui/tasks.py")
IMPORT_ANCHOR = "from open_webui.env import REDIS_KEY_PREFIX\n"
IMPORT_LINE = "from open_webui.b1_cancel_bridge import cancel_b1_chat_stream\n"
FUNCTION_ANCHOR = '''async def stop_item_tasks(redis: Redis, item_id: str):
    """
    Stop all tasks associated with a specific item ID.
    """
'''
FUNCTION_REPLACEMENT = FUNCTION_ANCHOR + "    await cancel_b1_chat_stream(item_id)\n"


def patch_tasks(path: Path = TASKS_PATH) -> None:
    source = path.read_text(encoding="utf-8")
    if IMPORT_LINE not in source:
        if IMPORT_ANCHOR not in source:
            raise RuntimeError("Open WebUI tasks import anchor not found")
        source = source.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + IMPORT_LINE, 1)
    if "    await cancel_b1_chat_stream(item_id)\n" not in source:
        if FUNCTION_ANCHOR not in source:
            raise RuntimeError("Open WebUI stop_item_tasks anchor not found")
        source = source.replace(FUNCTION_ANCHOR, FUNCTION_REPLACEMENT, 1)
    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    patch_tasks()
