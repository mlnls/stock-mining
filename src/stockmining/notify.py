"""Messaging: Slack (default, Socket Mode DM), Telegram (long polling), or console.

Every backend connects *out* from the server, so no inbound port is ever needed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time

import requests

log = logging.getLogger(__name__)
LIMIT = 3900  # Telegram max is 4096; Slack recommends ≤ 4,000 chars per message


def to_mrkdwn(text: str) -> str:
    """Markdown → Slack mrkdwn: headings/bold → *bold*, [t](u) → <u|t>, '- ' bullets → '• '."""
    import re
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"(?m)^#{1,6}\s*(.+?)\s*$", r"*\1*", text)
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)
    return text


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
    """Posts to the DM between the bot and ``user_id`` (opened on first use).

    App-level tokens may be rotating (``xoxe.xapp-...`` + refresh token, expiring after ~12 h). Then the
    refresh token, client id and client secret mint a new pair; the newest pair is kept in the DB (and
    written back to SSM best-effort, because each refresh token works only once)."""
    name = "slack"

    def __init__(self, bot_token: str | None, app_token: str | None, user_id: str | None,
                 refresh_token: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, db=None):
        self.bot_token, self.app_token, self.user_id = bot_token, app_token, user_id
        self.refresh_token, self.client_id, self.client_secret, self.db = refresh_token, client_id, client_secret, db
        self.enabled = bool(bot_token and user_id)
        self._web = None
        self._dm: str | None = None

    def current_app_token(self) -> str | None:
        """Valid app-level token, rotating it when it is missing or expires within 30 minutes."""
        if not (self.refresh_token and self.client_id and self.client_secret):
            return self.app_token
        state = json.loads(self.db.get("slack_app_token") or "{}") if self.db else {}
        if state.get("token") and state.get("expires_at", 0) - time.time() > 1800:
            return state["token"]
        from slack_sdk.web import WebClient
        refresh = state.get("refresh") or self.refresh_token
        try:
            r = WebClient().oauth_v2_access(client_id=self.client_id, client_secret=self.client_secret,
                                            grant_type="refresh_token", refresh_token=refresh)
        except Exception as e:
            log.error("slack app token rotation failed: %s", e)
            return state.get("token") or self.app_token
        state = {"token": r["access_token"], "refresh": r["refresh_token"],
                 "expires_at": time.time() + int(r.get("expires_in", 43200))}
        if self.db:
            self.db.set("slack_app_token", json.dumps(state))
        try:  # keep SSM's copy current so a rebuilt server can still rotate
            subprocess.run(["aws", "ssm", "put-parameter", "--overwrite", "--type", "SecureString",
                            "--name", "/stockmining/SLACK_APP_REFRESH_TOKEN", "--value", state["refresh"]],
                           capture_output=True, timeout=30, check=False)
        except Exception as e:
            log.warning("could not write refresh token to SSM: %s", e)
        log.info("slack app token rotated (expires in %ss)", r.get("expires_in"))
        return state["token"]

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
        for chunk in split(to_mrkdwn(text)):
            try:
                self.web.chat_postMessage(channel=channel or self.dm_channel(), text=chunk, unfurl_links=False,
                                          unfurl_media=False)
            except Exception as e:
                log.error("slack send failed: %s", e)


class Prefixed:
    """Wraps a sender so every message starts with ``prefix`` (tells two market instances apart)."""

    def __init__(self, inner, prefix: str):
        self.inner, self.prefix = inner, prefix

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def send(self, text: str, *a, **kw) -> None:
        self.inner.send(self.prefix + text, *a, **kw)


def make(cfg: dict, db=None):
    s = _make(cfg, db)
    return Prefixed(s, cfg["notify_prefix"]) if cfg.get("notify_prefix") else s


def _make(cfg: dict, db=None):
    kind = cfg.get("notifier", "slack")
    if kind == "slack":
        e = os.environ.get
        return Slack(e("SLACK_BOT_TOKEN"), e("SLACK_APP_TOKEN"), e("SLACK_USER_ID"),
                     e("SLACK_APP_REFRESH_TOKEN"), e("SLACK_CLIENT_ID"), e("SLACK_CLIENT_SECRET"), db)
    if kind == "telegram":
        return Telegram(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
    return Console()
