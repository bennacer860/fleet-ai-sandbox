"""Telegram notification utility."""

import asyncio
import aiohttp
from typing import Any, Optional
from ..logging_config import get_logger

logger = get_logger(__name__)

class TelegramNotifier:
    """Sends messages to a Telegram chat via the Bot API."""

    def __init__(self, token: str, chat_id: str, enabled: bool = True) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def push_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured chat."""
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            session = await self._get_session()
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error("Telegram API error: %d - %s", resp.status, error_text)
                    return False
                return True
        except Exception:
            logger.exception("Failed to send Telegram message")
            return False

    async def stop(self) -> None:
        """Close the underlying session."""
        if self._session is not None:
            if not self._session.closed:
                await self._session.close()
            self._session = None
