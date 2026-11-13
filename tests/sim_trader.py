"""用历史数据 + 模拟交易所，把实盘 Trader 的完整逻辑跑一遍（不联网下单）。

  python tests/sim_trader.py
检查：开仓必有止损、移动止损只收紧、止损触发被正确记账、无异常。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import data, trader as trader_mod  # noqa: E402
from bot.binance_client import SymbolRules  # noqa: E402
from bot.trader import Trader  # noqa: E402

FEE = 0.0005


class Clock:
    t = 0


def fake_now():
    return Clock.t


class FakeClient:
    def __init__(self, dfs, balance=1000.0):
        self.dfs = dfs
        self.balance = balance
        self.pos = {s: [0.0, 0.0] for s in dfs}           # amt, entry
        self.algos = {}                                   # id -> (sym, side, trigger)
        self.next_id = 1
        self.income = []                                  # (ms, sym, amount)
        self.violations = []

    # --- helpers
    def _bar(self, s):
        df = self.dfs[s]
        return df[df["open_time"] <= Clock.t].iloc[-1]

    def _fill(self, s, side, qty, price):
        amt, entry = self.pos[s]
        signed = qty if side == "BUY" else -qty
        fee = price * qty * FEE
        self.income.append((Clock.t, s, -fee))
        self.balance -= fee
        if amt == 0 or (amt > 0) == (signed > 0):
            new = amt + signed
            entry = (amt * entry + signed * price) / new
            self.pos[s] = [new, entry]
        else:
            closed = min(abs(signed), abs(amt))
            pnl = closed * (price - entry) * (1 if amt > 0 else -1)
            self.balance += pnl
            self.income.append((Clock.t, s, pnl))
            new = amt + signed
            self.pos[s] = [new, entry if new else 0.0]

    def advance_bar_extremes(self, s, bar):
        """K 线内检查止损触发。"""
        for aid, (sym, side, trig) in list(self.algos.items()):
            if sym != s or self.pos[s][0] == 0:
                continue
            if side == "SELL" and bar["low"] <= trig:
                px = min(trig, bar["open"])
            elif side == "BUY" and bar["high"] >= trig:
                px = max(trig, bar["open"])
            else:
                continue
            self._fill(s, side, abs(self.pos[s][0]), px)
            self.algos = {k: v for k, v in self.algos.items() if v[0] != s}

    # --- API surface used by Trader
    def sync_time(self): pass
    def is_hedge_mode(self): return False
    def set_isolated(self, s): pass
    def set_leverage(self, s, lev): pass

    def rules(self, s):
        return SymbolRules({"symbol": s, "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100000"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]})

    def usdt_balance(self): return self.balance, self.balance

    def position(self, s):
        amt, entry = self.pos[s]
        px = self._bar(s)["open"]
        return {"amt": amt, "entry": entry, "upnl": amt * (px - entry)}

    def klines(self, s, interval, limit=500, **kw):
        df = self.dfs[s]
        df = df[df["open_time"] <= Clock.t].tail(limit)
        return [[r.open_time, r.open, r.high, r.low, r.close, r.volume, r.close_time]
                for r in df.itertuples()]

    def mark_price(self, s): return float(self._bar(s)["open"])

    def market_order(self, s, side, qty, reduce_only=False):
        px = float(self._bar(s)["open"])
        self._fill(s, side, float(qty), px)
        return {"avgPrice": str(px)}

    def place_stop(self, s, side, trig):
        amt = self.pos[s][0]
        if amt == 0:
            self.violations.append(f"{s} 无仓位却挂止损")
        if (amt > 0 and side != "SELL") or (amt < 0 and side != "BUY"):
            self.violations.append(f"{s} 止损方向错误")
        aid = self.next_id
        self.next_id += 1
        self.algos[aid] = (s, side, float(trig))
        return aid

    def cancel_algo(self, aid): self.algos.pop(aid, None)
    def cancel_all_algo(self, s): self.algos = {k: v for k, v in self.algos.items() if v[0] != s}
    def open_algo_orders(self, s): return [{"algoId": k} for k, v in self.algos.items() if v[0] == s]

    def realized_pnl_since(self, s, start):
        return sum(a for t, sym, a in self.income if sym == s and t >= start)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    syms = ["BTCUSDT", "ETHUSDT"]
    dfs = {s: data.load(s, "1h", "2024-01-01", "2024-12-31") for s in syms}
    cfg = {"testnet": True, "api_key": "x", "api_secret": "x", "symbols": syms, "interval": "1h",
           "risk": {"max_drawdown_kill": 0.5}}
    trader_mod.now_ms = fake_now
    trader_mod.time.sleep = lambda *_: None
    Path("state_testnet.json").unlink(missing_ok=True)
    fc = FakeClient(dfs)
    tr = Trader(cfg)
    tr.client = fc
    tr.state_file = Path("reports/sim_state.json")
    Path("reports").mkdir(exist_ok=True)
    tr.state = {"positions": {}, "last_bar": {}, "risk": {}}

    times = dfs[syms[0]]["open_time"].tolist()
    Clock.t = times[0] + 600 * 3600_000
    tr.setup()
    opens = closes = 0
    prev_stops = {}
    for t in times[600:]:
        Clock.t = t + 1000                       # 新 K 线开盘后 1 秒
        before = set(tr.positions)
        tr.tick()
        for s in syms:
            st = tr.positions.get(s)
            if st:
                if not any(v[0] == s for v in fc.algos.values()):
                    fc.violations.append(f"{s} 持仓没有止损单 @ {t}")
                ps = prev_stops.get(s)
                if ps and ps[0] == st["side"] and ps[1] != st["entry"]:
                    pass
                if ps and ps[2] == st["entry"]:
                    worse = st["stop"] < ps[1] if st["side"] > 0 else st["stop"] > ps[1]
                    if worse:
                        fc.violations.append(f"{s} 止损被放松 {ps[1]} -> {st['stop']}")
                prev_stops[s] = (st["side"], st["stop"], st["entry"])
            bar = dfs[s][dfs[s]["open_time"] == t].iloc[0]
            fc.advance_bar_extremes(s, bar)
        after = set(tr.positions)
        opens += len(after - before)
        closes += len(before - after)

    print(f"模拟结束：开仓 {opens} 次，平仓 {closes} 次，最终余额 {fc.balance:.2f} USDT")
    print(f"风控状态：{tr.risk.to_dict()}")
    if fc.violations:
        print("发现问题：")
        for v in fc.violations[:20]:
            print("  -", v)
        sys.exit(1)
    print("检查通过：每笔持仓都有止损单，止损只收紧不放松。")


if __name__ == "__main__":
    main()
