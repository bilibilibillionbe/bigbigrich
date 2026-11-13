"""手机通知（可选）：Server酱（推送到微信）或 Telegram。都不填就只写日志。"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: dict | None, prefix: str = ""):
        cfg = cfg or {}
        self.sc_key = cfg.get("serverchan_key", "").strip()
        self.tg_token = cfg.get("telegram_token", "").strip()
        self.tg_chat = str(cfg.get("telegram_chat_id", "")).strip()
        self.prefix = prefix

    def send(self, title: str, body: str = ""):
        title = self.prefix + title
        log.info("[通知] %s %s", title, body)
        if self.sc_key:
            try:
                requests.post(f"https://sctapi.ftqq.com/{self.sc_key}.send",
                              data={"title": title[:32], "desp": body}, timeout=10)
            except requests.RequestException as e:
                log.warning("Server酱推送失败: %s", e)
        if self.tg_token and self.tg_chat:
            try:
                requests.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                              data={"chat_id": self.tg_chat, "text": f"{title}\n{body}"}, timeout=10)
            except requests.RequestException as e:
                log.warning("Telegram 推送失败: %s", e)
