"""Messaging: Slack (default, Socket Mode DM), Telegram (long polling), or console.

Every backend connects *out* from the server, so no inbound port is ever needed.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)
LIMIT = 3900  # Telegram max is 4096; Slack recommends ≤ 4,000 chars per message


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


class Console:
    name = "console"
    enabled = False

    def send(self, text: str) -> None:
        print(text)


class Telegram:
    name = "telegram"
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str | None, chat_id: str | None):
        self.token, self.chat_id = token, chat_id
        self.enabled = bool(token and chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            print(text)
            return
        for chunk in split(text):
            try:
                r = requests.post(self.API.format(token=self.token, method="sendMessage"), timeout=30,
                                  json={"chat_id": self.chat_id, "text": chunk, "disable_web_page_preview": True})
                r.raise_for_status()
            except requests.RequestException as e:
                log.error("telegram send failed: %s", e)

    def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        r = requests.post(self.API.format(token=self.token, method="getUpdates"),
                          json={"offset": offset, "timeout": timeout}, timeout=timeout + 10)
        r.raise_for_status()
        return r.json().get("result", [])


class Slack:
    """Posts to the DM between the bot and ``user_id`` (opened on first use)."""
    name = "slack"

    def __init__(self, bot_token: str | None, app_token: str | None, user_id: str | None):
        self.bot_token, self.app_token, self.user_id = bot_token, app_token, user_id
        self.enabled = bool(bot_token and user_id)
        self._web = None
        self._dm: str | None = None

    @property
    def web(self):
        if self._web is None:
            from slack_sdk.web import WebClient
            self._web = WebClient(token=self.bot_token)
        return self._web

    def dm_channel(self) -> str:
        if not self._dm:
            self._dm = self.web.conversations_open(users=self.user_id)["channel"]["id"]
        return self._dm

    def send(self, text: str, channel: str | None = None) -> None:
        if not self.enabled:
            print(text)
            return
        for chunk in split(text):
            try:
                self.web.chat_postMessage(channel=channel or self.dm_channel(), text=chunk, unfurl_links=False,
                                          unfurl_media=False)
            except Exception as e:
                log.error("slack send failed: %s", e)


def make(cfg: dict):
    kind = cfg.get("notifier", "slack")
    if kind == "slack":
        return Slack(os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_APP_TOKEN"),
                     os.environ.get("SLACK_USER_ID"))
    if kind == "telegram":
        return Telegram(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
    return Console()
