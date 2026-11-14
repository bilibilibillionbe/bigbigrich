"""实盘/测试网 自动交易主循环。

每 30 秒检查一次：
  - 持仓是否还有服务器端止损单（没有就立即补上），保证电脑断网/关机时也有止损保护
  - 止损是否已经被触发（记录盈亏、计入风控）
  - 每根 K 线收盘后：更新移动止损 / 检查反转平仓 / 检查开仓信号
所有状态写入 state_*.json，程序重启后可以接着管理原来的仓位。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .binance_client import BinanceError, BinanceFutures
from .notify import Notifier
from .risk import RiskState, position_size, resolve
from .strategy import Params, indicators, initial_stop, klines_to_df, trail_stop

log = logging.getLogger(__name__)

INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
               "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000}
TICK_SECONDS = 30


def now_ms() -> int:
    return int(time.time() * 1000)


class Trader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.testnet = bool(cfg.get("testnet", True))
        self.symbols = [s.upper() for s in cfg["symbols"]]
        self.interval = cfg.get("interval", "1h")
        if self.interval not in INTERVAL_MS:
            raise ValueError(f"不支持的周期 {self.interval}")
        self.ims = INTERVAL_MS[self.interval]
        self.p = Params.from_dict(cfg.get("strategy"))
        self.rc = resolve(cfg)
        self.mode = cfg.get("mode", "custom")
        self.capital_cap = float(cfg.get("capital_cap", 0) or 0)
        self.client = BinanceFutures(cfg["api_key"], cfg["api_secret"], testnet=self.testnet)
        tag = "[测试网] " if self.testnet else "[实盘] "
        self.notify = Notifier(cfg.get("notify"), prefix=tag)
        self.state_file = Path("state_testnet.json" if self.testnet else "state_live.json")
        self.state = self._load_state()
        self.risk = RiskState(self.state.get("risk"))
        self.manual: set[str] = set()   # 检测到的手动仓位，机器人不碰
        self.last_heartbeat = 0

    # ----------------------------------------------------------- 状态持久化
    def _load_state(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {"positions": {}, "last_bar": {}, "risk": {}}

    def _save(self):
        self.state["risk"] = self.risk.to_dict()
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)

    @property
    def positions(self) -> dict:
        return self.state["positions"]

    # --------------------------------------------------------------- 启动
    def setup(self):
        c = self.client
        c.sync_time()
        if c.is_hedge_mode():
            raise SystemExit("账户是“双向持仓”模式，请在币安合约设置里改成“单向持仓”后再运行。")
        wallet, avail = c.usdt_balance()
        # 首次启动不追已经过去的信号，从下一根 K 线收盘开始
        first = (now_ms() // self.ims) * self.ims - self.ims
        for s in self.symbols:
            self.state["last_bar"].setdefault(s, first)
        log.info("USDT 钱包余额 %.2f，可用 %.2f", wallet, avail)
        log.info("模式 %s：每笔风险 %.0f%%，杠杆上限 %dx，最多 %d 仓，交易本金 %s",
                 self.mode, self.rc.risk_per_trade * 100, self.rc.max_leverage, self.rc.max_positions,
                 f"{self.capital_cap:.0f} USDT" if self.capital_cap > 0 else "全部余额")
        for s in self.symbols:
            c.rules(s)
            pos = c.position(s)
            if pos["amt"] != 0 and s not in self.positions:
                self.manual.add(s)
                log.warning("%s 有一笔不是机器人开的仓位 (%s)，机器人不会碰它，平掉后才会交易该币种", s, pos["amt"])
                continue
            try:
                c.set_isolated(s)
                c.set_leverage(s, self.rc.max_leverage)
            except BinanceError as e:
                log.warning("%s 设置逐仓/杠杆失败：%s", s, e)
        self.notify.send("机器人已启动",
                         f"币种 {','.join(self.symbols)} 周期 {self.interval} 余额 {wallet:.2f} USDT")

    # --------------------------------------------------------------- 主循环
    def run_forever(self):
        self.setup()
        while True:
            try:
                self.tick()
            except BinanceError as e:
                log.error("币安接口错误：%s", e)
            except Exception:  # noqa: BLE001 — 任何异常都不能让机器人停下而丢掉止损管理
                log.exception("未知错误，30 秒后重试")
            time.sleep(TICK_SECONDS)

    def tick(self):
        c = self.client
        t = now_ms()
        wallet, _ = c.usdt_balance()
        live_pos = {s: c.position(s) for s in self.symbols}
        equity = float(wallet + sum(p["upnl"] for p in live_pos.values()))
        if self.capital_cap > 0:
            # 只用划定的本金交易：本金 + 机器人累计盈亏 + 机器人持仓浮盈，且不超过账户实际权益
            bot_upnl = sum(live_pos[k]["upnl"] for k in self.positions if k in live_pos)
            equity = min(equity, self.capital_cap + self.state.get("bot_pnl", 0.0) + bot_upnl)
        day = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")
        self.risk.update_equity(equity, day)

        for s in self.symbols:
            pos = live_pos[s]
            if s in self.manual:
                if pos["amt"] == 0:
                    self.manual.discard(s)
                    log.info("%s 手动仓位已平，机器人开始接管该币种", s)
                continue
            st = self.positions.get(s)
            if st and pos["amt"] == 0:
                self._on_closed_by_exchange(s, st)
                st = None
            elif st:
                self._ensure_stop(s, st, pos)

            last_closed_open = (t // self.ims) * self.ims - self.ims
            if self.state["last_bar"].get(s, 0) < last_closed_open:
                self._on_bar_close(s, st, pos, equity, live_pos)
                self.state["last_bar"][s] = last_closed_open
        self._save()

        if t - self.last_heartbeat > 3600_000:
            self.last_heartbeat = t
            held = {s: f"{'多' if v['side'] > 0 else '空'} 止损{v['stop']}" for s, v in self.positions.items()}
            log.info("心跳 | 权益 %.2f | 持仓 %s | 峰值 %.2f | 今日起始 %.2f",
                     equity, held or "无", self.risk.peak_equity, self.risk.day_start_equity)

    # --------------------------------------------------------- K 线收盘处理
    def _on_bar_close(self, s, st, pos, equity, live_pos):
        rows = self.client.klines(s, self.interval, limit=max(600, self.p.ema_trend * 3))
        df = klines_to_df(rows)
        df = df[df["close_time"] < now_ms()]          # 只用已收盘 K 线
        d = indicators(df, self.p)
        row = d.iloc[-1]
        if not row["ready"]:
            log.warning("%s K 线数量不足，跳过", s)
            return
        rules = self.client.rules(s)

        if st:  # ---- 持仓管理
            side = st["side"]
            since = d[d["open_time"] >= st["entry_bar"]]
            extreme = since["high"].max() if side > 0 else since["low"].min()
            st["extreme"] = float(extreme)
            if (side > 0 and row["long_exit"]) or (side < 0 and row["short_exit"]):
                self._close(s, st, pos, "趋势反转平仓")
                return
            new_stop = trail_stop(side, st["stop"], st["extreme"], float(row["atr"]), self.p)
            new_stop_s = rules.price(new_stop, up=side < 0)
            if (side > 0 and float(new_stop_s) > st["stop"]) or (side < 0 and float(new_stop_s) < st["stop"]):
                self._move_stop(s, st, pos, new_stop_s)
            return

        # ---- 开仓检查
        sig = 1 if row["long_entry"] else (-1 if row["short_entry"] else 0)
        if not sig:
            return
        if len(self.positions) >= self.rc.max_positions:
            log.info("%s 有%s信号，但持仓数已达上限 %d", s, "多" if sig > 0 else "空", self.rc.max_positions)
            return
        ok, why = self.risk.can_open(equity, now_ms(), self.rc)
        if not ok:
            log.info("%s 有信号但风控禁止开仓：%s", s, why)
            if self.risk.killed:
                self.notify.send("回撤熔断", why)
            return
        self._open(s, sig, row, equity, live_pos)

    # ---------------------------------------------------------------- 开仓
    def _open(self, s, side, row, equity, live_pos):
        c = self.client
        rules = c.rules(s)
        price = float(row["close"])
        atr = float(row["atr"])
        stop = initial_stop(side, price, atr, self.p)
        used = sum(abs(live_pos[k]["amt"]) * live_pos[k]["entry"] for k in self.positions)
        qty_s = rules.qty(position_size(equity, price, stop, self.rc, used))
        if float(qty_s) <= 0 or float(qty_s) * price < float(rules.min_notional):
            log.info("%s 资金不足以满足最小下单额 %s USDT，跳过", s, rules.min_notional)
            return
        # 逐仓杠杆：保证强平价远在止损价之外（先止损、不被强平），同时不超过上限
        stop_pct = abs(price - stop) / price
        lev = max(1, min(self.rc.max_leverage, int(1 / (stop_pct * 1.3 + 0.01))))
        need_margin = float(qty_s) * price / lev
        _, avail = c.usdt_balance()
        if need_margin > avail * 0.95:
            log.info("%s 可用保证金不足（需要 %.2f，可用 %.2f），跳过", s, need_margin, avail)
            return
        try:
            c.set_leverage(s, lev)
        except BinanceError as e:
            log.warning("%s 设置杠杆 %dx 失败：%s", s, lev, e)
            return
        order_side = "BUY" if side > 0 else "SELL"
        r = c.market_order(s, order_side, qty_s)
        fill = float(r.get("avgPrice") or 0) or price
        stop = initial_stop(side, fill, atr, self.p)
        stop_s = rules.price(stop, up=side < 0)
        st = {"side": side, "qty": qty_s, "entry": fill, "stop": float(stop_s), "extreme": fill,
              "entry_ms": now_ms() - 5000, "entry_bar": int(row["open_time"]) + self.ims, "algo_id": None}
        self.positions[s] = st
        self._save()
        try:
            st["algo_id"] = c.place_stop(s, "SELL" if side > 0 else "BUY", stop_s)
        except BinanceError as e:
            log.error("%s 止损单下单失败：%s —— 为安全立即平仓", s, e)
            c.market_order(s, "SELL" if side > 0 else "BUY", qty_s, reduce_only=True)
            self.positions.pop(s, None)
            self.notify.send(f"{s} 止损下单失败已平仓", str(e))
            return
        self._save()
        risk_usdt = abs(fill - float(stop_s)) * float(qty_s)
        self.notify.send(f"{s} 开{'多' if side > 0 else '空'}",
                         f"价格 {fill} 数量 {qty_s} 止损 {stop_s}，最大亏损约 {risk_usdt:.2f} USDT")

    # ------------------------------------------------------------- 止损维护
    def _ensure_stop(self, s, st, pos):
        """确认交易所上真的挂着止损单，没有就补。"""
        c = self.client
        try:
            ids = {int(o.get("algoId", 0)) for o in c.open_algo_orders(s)}
        except BinanceError as e:
            log.warning("%s 查询条件单失败：%s", s, e)
            return
        if st.get("algo_id") in ids:
            return
        side = st["side"]
        mark = c.mark_price(s)
        if (side > 0 and mark <= st["stop"]) or (side < 0 and mark >= st["stop"]):
            self._close(s, st, pos, "价格已越过止损，市价平仓")
            return
        log.warning("%s 没找到止损单，重新挂单 %s", s, st["stop"])
        rules = c.rules(s)
        try:
            st["algo_id"] = c.place_stop(s, "SELL" if side > 0 else "BUY",
                                         rules.price(st["stop"], up=side < 0))
        except BinanceError as e:
            log.error("%s 补挂止损失败：%s —— 市价平仓", s, e)
            self._close(s, st, pos, "无法挂止损，安全平仓")

    def _move_stop(self, s, st, pos, new_stop_s):
        c = self.client
        old_id = st.get("algo_id")
        if old_id:
            c.cancel_algo(old_id)
        st["algo_id"] = None
        try:
            st["algo_id"] = c.place_stop(s, "SELL" if st["side"] > 0 else "BUY", new_stop_s)
            log.info("%s 移动止损 %s -> %s", s, st["stop"], new_stop_s)
            st["stop"] = float(new_stop_s)
        except BinanceError as e:
            log.error("%s 移动止损失败：%s，下一轮会自动补挂", s, e)
        self._save()

    # ---------------------------------------------------------------- 平仓
    def _close(self, s, st, pos, reason):
        c = self.client
        c.cancel_all_algo(s)
        amt = pos["amt"] if pos else 0
        if amt == 0:
            amt = c.position(s)["amt"]
        if amt != 0:
            c.market_order(s, "SELL" if amt > 0 else "BUY", c.rules(s).qty(abs(amt)), reduce_only=True)
        time.sleep(2)
        self._finish(s, st, reason)

    def _on_closed_by_exchange(self, s, st):
        self.client.cancel_all_algo(s)
        self._finish(s, st, "止损/移动止损触发")

    def _finish(self, s, st, reason):
        try:
            pnl = self.client.realized_pnl_since(s, st["entry_ms"])
        except BinanceError as e:
            log.warning("查询盈亏失败：%s", e)
            pnl = 0.0
        self.risk.record_trade(pnl, now_ms(), self.rc)
        self.state["bot_pnl"] = self.state.get("bot_pnl", 0.0) + pnl
        self.positions.pop(s, None)
        self._save()
        self.notify.send(f"{s} 平仓 {'盈利' if pnl >= 0 else '亏损'} {pnl:+.2f} USDT", reason)
