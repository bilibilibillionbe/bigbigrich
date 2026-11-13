"""组合回测引擎：多个币种共用一份资金，逻辑与实盘一致。

成交假设（偏保守）：
  - 信号在 K 线收盘产生，下一根 K 线开盘价成交，并加滑点
  - 止损在 K 线内触及即按止损价成交（跳空则按开盘价），加滑点
  - 同一根 K 线内既可能触发止损又可能创新高时，按先止损处理
  - 双边吃单手续费 taker_fee；资金费率未计入（趋势单持仓时间较长，实盘会有小幅偏差）
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .risk import RiskConfig, RiskState, position_size
from .strategy import Params, indicators, initial_stop, trail_stop


@dataclass
class Pos:
    side: int
    qty: float
    entry: float
    stop: float
    extreme: float
    entry_time: pd.Timestamp
    fee: float


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    stats: dict = field(default_factory=dict)


def run(data: dict[str, pd.DataFrame], p: Params, rc: RiskConfig, capital: float = 1000.0,
        taker_fee: float = 0.0005, slippage: float = 0.0002) -> Result:
    ind = {s: indicators(df, p) for s, df in data.items()}
    index = sorted(set().union(*[d.index for d in ind.values()]))
    cash = capital
    pos: dict[str, Pos] = {}
    pending: dict[str, int] = {}        # 下一根开盘要执行的动作：+1/-1 开仓，0 平仓
    pending_atr: dict[str, float] = {}
    rs = RiskState()
    trades, curve = [], []
    killed_at = None
    last: dict[str, float] = {}        # 各币种最新价格，用于计算组合净值

    def equity_now():
        return cash + sum(x.side * (last[k] - x.entry) * x.qty for k, x in pos.items())

    def close(sym, price, t, reason):
        nonlocal cash
        ps = pos.pop(sym)
        fill = price * (1 - ps.side * slippage)
        fee = fill * ps.qty * taker_fee
        pnl = ps.side * (fill - ps.entry) * ps.qty - fee - ps.fee
        cash += ps.side * (fill - ps.entry) * ps.qty - fee
        rs.record_trade(pnl, int(t.value // 1_000_000), rc)
        trades.append(dict(symbol=sym, side="多" if ps.side > 0 else "空", entry_time=ps.entry_time,
                           exit_time=t, entry=ps.entry, exit=fill, qty=ps.qty, pnl=pnl, reason=reason))

    cols = ("open", "high", "low", "close", "atr", "ready", "long_entry", "short_entry", "long_exit", "short_exit")
    arr = {s: {k: d[k].to_numpy() for k in cols} for s, d in ind.items()}
    pos_of = {s: {t: i for i, t in enumerate(d.index)} for s, d in ind.items()}
    day_of = {t: t.strftime("%Y-%m-%d") for t in index}

    for t in index:
        now_ms = int(t.value // 1_000_000)
        day = day_of[t]
        for sym in ind:
            i = pos_of[sym].get(t)
            if i is None:
                continue
            a = arr[sym]
            o, h, l, c = a["open"][i], a["high"][i], a["low"][i], a["close"][i]
            last[sym] = o

            # 1) 执行上一根收盘产生的动作（本根开盘成交）
            act = pending.pop(sym, None)
            if act == 0 and sym in pos:
                close(sym, o, t, "趋势反转")
            elif act in (1, -1) and sym not in pos:
                fill = o * (1 + act * slippage)
                stop = initial_stop(act, fill, pending_atr[sym], p)
                eq = equity_now()
                used = sum(x.qty * x.entry for x in pos.values())
                qty = position_size(eq, fill, stop, rc, used)
                if qty * fill >= 5:
                    fee = fill * qty * taker_fee
                    cash -= fee
                    pos[sym] = Pos(act, qty, fill, stop, fill, t, fee)

            # 2) K 线内止损检查
            if sym in pos:
                ps = pos[sym]
                hit = (l <= ps.stop) if ps.side > 0 else (h >= ps.stop)
                if hit:
                    gap = (o < ps.stop) if ps.side > 0 else (o > ps.stop)
                    close(sym, o if gap else ps.stop, t, "止损/移动止损")

            # 3) 收盘：更新移动止损 / 产生信号
            last[sym] = c
            if not a["ready"][i]:
                continue
            if sym in pos:
                ps = pos[sym]
                ps.extreme = max(ps.extreme, h) if ps.side > 0 else min(ps.extreme, l)
                ps.stop = trail_stop(ps.side, ps.stop, ps.extreme, a["atr"][i], p)
                if (ps.side > 0 and a["long_exit"][i]) or (ps.side < 0 and a["short_exit"][i]):
                    pending[sym] = 0
            else:
                sig = 1 if a["long_entry"][i] else (-1 if a["short_entry"][i] else 0)
                if sig and len(pos) < rc.max_positions:
                    eq = equity_now()
                    rs.update_equity(eq, day)
                    ok, _ = rs.can_open(eq, now_ms, rc)
                    if rs.killed and killed_at is None:
                        killed_at = t
                    if ok:
                        pending[sym] = sig
                        pending_atr[sym] = a["atr"][i]

        eq = equity_now()
        rs.update_equity(eq, day)
        curve.append((t, eq))

    equity = pd.Series([e for _, e in curve], index=[t for t, _ in curve], name="equity")
    tr = pd.DataFrame(trades)
    st = stats(equity, tr, capital)
    st["回撤熔断"] = f"{killed_at:%Y-%m-%d} 触发，之后停止开仓（实盘需人工复查后重置）" if killed_at is not None else "未触发"
    return Result(equity, tr, st)


def stats(equity: pd.Series, tr: pd.DataFrame, capital: float) -> dict:
    if equity.empty:
        return {}
    dd = equity / equity.cummax() - 1
    days = max((equity.index[-1] - equity.index[0]).total_seconds() / 86400, 1)
    total = equity.iloc[-1] / capital - 1
    daily = equity.resample("1D").last().pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(365)) if daily.std() > 0 else 0.0
    s = {
        "起始资金": capital,
        "最终资金": round(float(equity.iloc[-1]), 2),
        "总收益率": f"{total:.2%}",
        "年化收益率": f"{(1 + total) ** (365 / days) - 1:.2%}" if total > -1 else "-100%",
        "最大回撤": f"{dd.min():.2%}",
        "夏普比率": round(sharpe, 2),
        "交易次数": int(len(tr)),
    }
    if len(tr):
        wins = tr[tr.pnl > 0]
        loss = tr[tr.pnl <= 0]
        s["胜率"] = f"{len(wins) / len(tr):.1%}"
        s["盈亏比(均盈/均亏)"] = round(wins.pnl.mean() / -loss.pnl.mean(), 2) if len(wins) and len(loss) and loss.pnl.mean() < 0 else None
        s["利润因子"] = round(wins.pnl.sum() / -loss.pnl.sum(), 2) if len(loss) and loss.pnl.sum() < 0 else None
        s["最大连亏笔数"] = int(_max_streak(tr.pnl <= 0))
    s["_total"] = float(total)
    s["_maxdd"] = float(dd.min())
    s["_sharpe"] = sharpe
    return s


def _max_streak(mask: pd.Series) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best
