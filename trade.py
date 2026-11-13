"""自动交易入口：python trade.py

默认连接币安测试网（假钱）。要切换到真实资金：
  config.json 里设置 "testnet": false 并且 "live_confirm": "YES"
"""
from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.binance_client import BinanceError
from bot.trader import Trader


def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main():
    setup_logging()
    if not Path("config.json").exists():
        sys.exit("找不到 config.json：请把 config.example.json 复制一份改名为 config.json，并填入 API Key。")
    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("api_key") or "在这里" in cfg.get("api_key", ""):
        sys.exit("config.json 里还没有填 api_key / api_secret")
    if not cfg.get("testnet", True) and cfg.get("live_confirm") != "YES":
        sys.exit('要用真实资金，请在 config.json 中同时设置 "testnet": false 和 "live_confirm": "YES"')
    logging.info("模式：%s", "测试网（模拟资金）" if cfg.get("testnet", True) else "实盘（真实资金）")
    try:
        Trader(cfg).run_forever()
    except BinanceError as e:
        if e.code in (-2014, -2015, -1022, -2008):
            sys.exit(f"API Key 无效或权限不足：{e}\n请检查：Key/Secret 是否填对；测试网/实盘的 Key 不能混用；"
                     "是否开启了合约交易权限；IP 白名单是否包含本机。")
        raise


if __name__ == "__main__":
    main()
