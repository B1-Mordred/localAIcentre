from __future__ import annotations

import logging
import os

import aiohttp


log = logging.getLogger(__name__)


async def cancel_b1_chat_stream(chat_id: str) -> None:
    """Tell the B1 controller to stop the provider stream before Open WebUI cancels its task."""
    api_base_url = os.getenv("B1_OPEN_WEBUI_API_BASE_URL", "").rstrip("/")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_base_url or not api_key or not chat_id:
        return

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{api_base_url}/open-webui/chat/cancel",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"chat_id": chat_id},
            ) as response:
                if response.status >= 400:
                    detail = (await response.text())[:500]
                    log.warning("B1 provider cancellation failed with HTTP %s: %s", response.status, detail)
    except Exception as exc:
        # Local task cancellation must remain available if the controller is
        # temporarily unreachable. The controller's disconnect cleanup is the
        # fallback in that case.
        log.warning("B1 provider cancellation request failed: %s", exc.__class__.__name__)
