"""Telegram messaging (send + long-poll updates)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 3900  # Telegram max is 4096 chars


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None, enabled: bool = True):
        self.token, self.chat_id = token, chat_id
        self.enabled = bool(enabled and token and chat_id)

    def _post(self, method: str, timeout: int = 30, **data):
        r = requests.post(API.format(token=self.token, method=method), json=data, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def send(self, text: str, chat_id: str | None = None) -> None:
        if not self.enabled:
            print(text)
            return
        for chunk in split(text):
            try:
                self._post("sendMessage", chat_id=chat_id or self.chat_id, text=chunk,
                           disable_web_page_preview=True)
            except requests.RequestException as e:
                log.error("telegram send failed: %s", e)

    def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        r = requests.post(API.format(token=self.token, method="getUpdates"),
                          json={"offset": offset, "timeout": timeout}, timeout=timeout + 10)
        r.raise_for_status()
        return r.json().get("result", [])


def split(text: str, limit: int = LIMIT) -> list[str]:
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
            cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)
    return chunks or [""]
