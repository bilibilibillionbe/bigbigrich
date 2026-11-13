"""趋势跟踪策略（回测与实盘共用同一份代码，保证结果一致）。

思路来自经典的实战趋势系统（海龟通道突破 + 均线多空过滤 + ADX 趋势强度 +
ATR 吊灯止损），核心是“亏小赚大”：胜率通常只有 35%~45%，靠少数大趋势盈利。

  做多条件（K 线收盘时判断）：
    1. 收盘价 > EMA(trend)            —— 只在大方向向上时做多
    2. EMA(fast) > EMA(slow)          —— 中期趋势向上
    3. ADX > adx_min                  —— 有趋势，过滤震荡行情
    4. 收盘价突破前 N 根 K 线最高价     —— 唐奇安通道突破入场
  做空完全对称。

  出场：
    - 初始止损：入场价 -/+ stop_atr × ATR（下到交易所服务器上）
    - 移动止损：持仓期间最高(低)价 -/+ trail_atr × ATR，只收紧不放松
    - 趋势反转：EMA(fast) 与 EMA(slow) 反向交叉时平仓
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass
class Params:
    ema_fast: int = 21
    ema_slow: int = 55
    ema_trend: int = 200
    donchian: int = 20
    adx_len: int = 14
    adx_min: float = 20.0
    atr_len: int = 14
    stop_atr: float = 2.0
    trail_atr: float = 3.0

    @classmethod
    def from_dict(cls, d: dict | None):
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self):
        return asdict(self)


def _rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder 平滑（ATR / ADX 标准算法）。"""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """df 需含 open/high/low/close 列，按时间升序。返回带指标与信号的新 DataFrame。"""
    d = df.copy()
    h, l, c = d["high"], d["low"], d["close"]

    d["ema_fast"] = c.ewm(span=p.ema_fast, adjust=False).mean()
    d["ema_slow"] = c.ewm(span=p.ema_slow, adjust=False).mean()
    d["ema_trend"] = c.ewm(span=p.ema_trend, adjust=False).mean()

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    d["atr"] = _rma(tr, p.atr_len)

    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr_adx = _rma(tr, p.adx_len)
    plus_di = 100 * _rma(pd.Series(plus_dm, index=d.index), p.adx_len) / atr_adx
    minus_di = 100 * _rma(pd.Series(minus_dm, index=d.index), p.adx_len) / atr_adx
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    d["adx"] = _rma(dx.fillna(0), p.adx_len)

    # 前 N 根（不含当前）的通道
    d["dc_high"] = h.shift(1).rolling(p.donchian).max()
    d["dc_low"] = l.shift(1).rolling(p.donchian).min()

    trend_up = (c > d["ema_trend"]) & (d["ema_fast"] > d["ema_slow"])
    trend_dn = (c < d["ema_trend"]) & (d["ema_fast"] < d["ema_slow"])
    strong = d["adx"] > p.adx_min
    d["long_entry"] = trend_up & strong & (c > d["dc_high"])
    d["short_entry"] = trend_dn & strong & (c < d["dc_low"])
    d["long_exit"] = d["ema_fast"] < d["ema_slow"]
    d["short_exit"] = d["ema_fast"] > d["ema_slow"]

    warmup = max(p.ema_trend, p.ema_slow * 2, p.donchian + 1, p.adx_len * 3)
    d["ready"] = False
    d.iloc[warmup:, d.columns.get_loc("ready")] = True
    return d


def initial_stop(side: int, price: float, atr: float, p: Params) -> float:
    return price - side * p.stop_atr * atr


def trail_stop(side: int, current_stop: float, extreme: float, atr: float, p: Params) -> float:
    """extreme：持仓期间多头最高价 / 空头最低价。只收紧不放松。"""
    cand = extreme - side * p.trail_atr * atr
    return max(current_stop, cand) if side > 0 else min(current_stop, cand)


def klines_to_df(rows) -> pd.DataFrame:
    """币安 K 线数组 -> DataFrame（时间索引为开盘时间 UTC）。"""
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "qv", "trades", "tbv", "tqv", "ignore"]
    df = pd.DataFrame(rows, columns=cols[:len(rows[0])] if rows else cols)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["open_time", "close_time", "open", "high", "low", "close", "volume"]]
