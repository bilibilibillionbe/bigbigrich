"""风控：仓位计算 + 熔断规则。回测和实盘共用。

  - 每笔亏损上限：止损被打掉时只亏总资金的 risk_per_trade（默认 1%）
  - 杠杆上限：总名义价值不超过 资金 × max_leverage
  - 日内熔断：当天（UTC）亏损达到 daily_loss_limit 后当天不再开新仓
  - 连亏暂停：连续亏损 max_consecutive_losses 笔后暂停 pause_hours 小时
  - 回撤熔断：资金从最高点回撤 max_drawdown_kill 后停止开仓，需手动重置
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01
    max_leverage: int = 3
    max_positions: int = 2
    daily_loss_limit: float = 0.03
    max_consecutive_losses: int = 4
    pause_hours: float = 24
    max_drawdown_kill: float = 0.20

    @classmethod
    def from_dict(cls, d: dict | None):
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# 预设模式。custom = 使用 config.json 里 "risk" 的自定义数值
PRESETS = {
    "standard": RiskConfig(),
    "aggressive": RiskConfig(risk_per_trade=0.03, max_leverage=10, max_positions=2,
                             daily_loss_limit=0.15, max_consecutive_losses=6, pause_hours=12,
                             max_drawdown_kill=0.60),
    # 极度激进：每笔赌 10%，不设日内/连亏限制，资金只剩峰值的 10% 时停止
    "extreme": RiskConfig(risk_per_trade=0.10, max_leverage=20, max_positions=3,
                          daily_loss_limit=1.0, max_consecutive_losses=10_000, pause_hours=0,
                          max_drawdown_kill=0.90),
}


def resolve(cfg: dict) -> RiskConfig:
    mode = cfg.get("mode", "custom" if "risk" in cfg else "standard")
    if mode == "custom":
        return RiskConfig.from_dict(cfg.get("risk"))
    if mode not in PRESETS:
        raise ValueError(f"未知模式 {mode}，可选：{', '.join(PRESETS)}, custom")
    return RiskConfig(**PRESETS[mode].__dict__)


def position_size(equity: float, entry: float, stop: float, cfg: RiskConfig,
                  used_notional: float = 0.0) -> float:
    """按“止损亏损 = 资金 × risk_per_trade”反推数量，再受杠杆上限约束。"""
    dist = abs(entry - stop)
    if dist <= 0 or equity <= 0:
        return 0.0
    qty = equity * cfg.risk_per_trade / dist
    max_notional = equity * cfg.max_leverage * 0.95 - used_notional
    if max_notional <= 0:
        return 0.0
    return min(qty, max_notional / entry)


class RiskState:
    """可序列化的风控状态（实盘会存到 state.json，重启不丢）。"""

    def __init__(self, d: dict | None = None):
        d = d or {}
        self.peak_equity: float = d.get("peak_equity", 0.0)
        self.day: str = d.get("day", "")
        self.day_start_equity: float = d.get("day_start_equity", 0.0)
        self.consecutive_losses: int = d.get("consecutive_losses", 0)
        self.pause_until_ms: int = d.get("pause_until_ms", 0)
        self.killed: bool = d.get("killed", False)

    def to_dict(self):
        return dict(self.__dict__)

    def update_equity(self, equity: float, day: str):
        if day != self.day:
            self.day = day
            self.day_start_equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    def record_trade(self, pnl: float, now_ms: int, cfg: RiskConfig):
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= cfg.max_consecutive_losses:
                self.pause_until_ms = now_ms + int(cfg.pause_hours * 3600_000)
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

    def can_open(self, equity: float, now_ms: int, cfg: RiskConfig) -> tuple[bool, str]:
        if self.killed:
            return False, "回撤熔断已触发（删除 state.json 中 killed 或设为 false 后恢复）"
        if self.peak_equity > 0 and equity <= self.peak_equity * (1 - cfg.max_drawdown_kill):
            self.killed = True
            return False, f"资金回撤超过 {cfg.max_drawdown_kill:.0%}，停止开仓"
        if self.day_start_equity > 0 and equity <= self.day_start_equity * (1 - cfg.daily_loss_limit):
            return False, f"今日亏损达 {cfg.daily_loss_limit:.0%}，今天不再开仓"
        if now_ms < self.pause_until_ms:
            return False, "连续亏损，暂停中"
        return True, ""
