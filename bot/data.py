"""历史 K 线下载（回测用）。

优先从币安官方公开数据仓库 data.binance.vision 下载月度压缩包（快、免 API Key），
当月数据再用 REST 接口补齐。下载结果缓存在 data/ 目录。
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from .strategy import klines_to_df

log = logging.getLogger(__name__)
VISION = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/{i}/{s}-{i}-{y}-{m:02d}.zip"
REST_URLS = ["https://fapi.binance.com", "https://testnet.binancefuture.com"]
DATA_DIR = Path("data")


def _month_iter(start: datetime, end: datetime):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _read_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        with z.open(z.namelist()[0]) as f:
            raw = pd.read_csv(f, header=None)
    if not str(raw.iloc[0, 0]).isdigit():  # 新版文件带表头
        raw = raw.iloc[1:]
    return klines_to_df(raw.values.tolist())


def _rest_tail(symbol: str, interval: str, start_ms: int) -> pd.DataFrame:
    for base in REST_URLS:
        rows, cur = [], start_ms
        try:
            while True:
                r = requests.get(base + "/fapi/v1/klines", params={
                    "symbol": symbol, "interval": interval, "startTime": cur, "limit": 1500}, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                rows += batch
                cur = batch[-1][0] + 1
                if len(batch) < 1500:
                    break
                time.sleep(0.2)
            if base != REST_URLS[0]:
                log.warning("主站接口不可用，最近一个月数据取自测试网（价格可能与实盘略有差异）")
            return klines_to_df(rows) if rows else pd.DataFrame()
        except requests.RequestException as e:
            log.info("%s 获取失败：%s", base, e)
    return pd.DataFrame()


def load(symbol: str, interval: str, start: str, end: str | None = None) -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    st = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    en = (datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end
          else datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    frames = []
    missing_from = None
    for y, m in _month_iter(st, en):
        if (y, m) == (now.year, now.month):
            missing_from = missing_from or datetime(y, m, 1, tzinfo=timezone.utc)
            continue
        cache = DATA_DIR / f"{symbol}-{interval}-{y}-{m:02d}.pkl"
        if cache.exists():
            frames.append(pd.read_pickle(cache))
            continue
        url = VISION.format(s=symbol, i=interval, y=y, m=m)
        try:
            r = requests.get(url, timeout=60)
        except requests.RequestException as e:
            log.warning("下载失败 %s: %s", url, e)
            missing_from = missing_from or datetime(y, m, 1, tzinfo=timezone.utc)
            continue
        if r.status_code == 404:
            # 上月包可能还没生成，用 REST 补
            if (y, m) >= (now.year, now.month - 1 if now.month > 1 else 12):
                missing_from = missing_from or datetime(y, m, 1, tzinfo=timezone.utc)
            continue
        r.raise_for_status()
        df = _read_zip(r.content)
        df.to_pickle(cache)
        frames.append(df)
        log.info("已下载 %s %s %d-%02d (%d 根)", symbol, interval, y, m, len(df))
    if missing_from is not None and missing_from < en:
        tail = _rest_tail(symbol, interval, int(missing_from.timestamp() * 1000))
        if not tail.empty:
            frames.append(tail)
    if not frames:
        raise RuntimeError(f"没有拿到 {symbol} 的历史数据，请检查网络")
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[(df.index >= st) & (df.index <= en)]
