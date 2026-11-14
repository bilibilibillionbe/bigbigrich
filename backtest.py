"""回测 / 参数优化入口。

  python backtest.py                         # 用 config.json 的币种和参数回测近 3 年
  python backtest.py --start 2022-01-01      # 指定起始日期
  python backtest.py --optimize              # 参数网格搜索 + 样本外验证（防止过拟合）

结果保存在 reports/ 目录（交易明细 CSV + 资金曲线 CSV）。
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import backtest as bt
from bot import data
from bot.risk import resolve
from bot.strategy import Params


def load_cfg():
    for name in ("config.json", "config.example.json"):
        if Path(name).exists():
            with open(name, encoding="utf-8") as f:
                return json.load(f)
    return {}


def show(title, s):
    print(f"\n===== {title} =====")
    for k, v in s.items():
        if not k.startswith("_"):
            print(f"  {k:<14}{v}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_cfg()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=cfg.get("symbols", ["BTCUSDT", "ETHUSDT"]))
    ap.add_argument("--interval", default=cfg.get("interval", "1h"))
    ap.add_argument("--start", default=(datetime.now(timezone.utc) - timedelta(days=3 * 365)).strftime("%Y-%m-%d"))
    ap.add_argument("--end", default=None)
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--mode", choices=["standard", "aggressive", "extreme", "custom"], default=None)
    ap.add_argument("--no-kill", action="store_true", help="关闭回撤熔断，看策略本身的表现")
    a = ap.parse_args()

    p = Params.from_dict(cfg.get("strategy"))
    rc = resolve({**cfg, **({"mode": a.mode} if a.mode else {})})
    if a.no_kill or a.optimize:   # 优化时关闭熔断，否则熔断后不再交易会扭曲评分
        rc.max_drawdown_kill = 1.0
    dfs = {s: data.load(s, a.interval, a.start, a.end) for s in a.symbols}
    for s, df in dfs.items():
        print(f"{s}: {len(df)} 根K线 {df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}")

    Path("reports").mkdir(exist_ok=True)
    if not a.optimize:
        r = bt.run(dfs, p, rc, a.capital)
        show(f"回测结果 {','.join(a.symbols)} {a.interval}", r.stats)
        if len(r.trades):
            r.trades.to_csv("reports/trades.csv", index=False, encoding="utf-8-sig")
            print("\n按币种：")
            print(r.trades.groupby("symbol").pnl.agg(["count", "sum"]).round(2).to_string())
        r.equity.to_csv("reports/equity.csv", encoding="utf-8-sig")
        print("\n明细已保存到 reports/trades.csv, reports/equity.csv")
        return

    # ---- 网格搜索：前 70% 数据选参数，后 30% 数据验证 ----
    cut = min(df.index[0] + (df.index[-1] - df.index[0]) * 0.7 for df in dfs.values())
    ins = {s: df[df.index < cut] for s, df in dfs.items()}
    # 样本外保留足够预热长度
    oos = {s: df[df.index >= cut - (df.index[1] - df.index[0]) * 400] for s, df in dfs.items()}
    grid = {
        "ema_fast": [13, 21, 34],
        "ema_slow": [55, 89],
        "donchian": [20, 40, 55],
        "adx_min": [15, 20, 25],
        "stop_atr": [1.5, 2.0, 3.0],
        "trail_atr": [2.5, 3.5, 4.5],
    }
    keys = list(grid)
    combos = list(itertools.product(*grid.values()))
    print(f"\n样本内: ~{cut:%Y-%m-%d}，样本外: {cut:%Y-%m-%d}~，共 {len(combos)} 组参数，计算中…")
    logging.getLogger().setLevel(logging.WARNING)
    res = []
    for i, combo in enumerate(combos, 1):
        pp = Params(**{**p.to_dict(), **dict(zip(keys, combo))})
        s = bt.run(ins, pp, rc, a.capital).stats
        if s.get("交易次数", 0) >= 30:
            # 评分：收益/回撤（Calmar 风格），回撤越小越好
            score = s["_total"] / max(abs(s["_maxdd"]), 0.05)
            res.append((score, pp, s))
        if i % 50 == 0:
            print(f"  {i}/{len(combos)}")
    res.sort(key=lambda x: x[0], reverse=True)
    print("\n样本内前 5 组参数，以及它们在样本外（没见过的数据）上的表现：")
    best = None
    for score, pp, s in res[:5]:
        o = bt.run(oos, pp, rc, a.capital).stats
        d = {k: pp.to_dict()[k] for k in keys}
        print(f"\n参数 {d}")
        print(f"  样本内  收益 {s['总收益率']:>9}  回撤 {s['最大回撤']:>8}  胜率 {s.get('胜率')}  交易 {s['交易次数']}")
        print(f"  样本外  收益 {o['总收益率']:>9}  回撤 {o['最大回撤']:>8}  胜率 {o.get('胜率')}  交易 {o['交易次数']}")
        oscore = o["_total"] / max(abs(o["_maxdd"]), 0.05)
        if best is None or oscore > best[0]:
            best = (oscore, pp)
    if best:
        Path("reports/best_params.json").write_text(
            json.dumps(best[1].to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        print("\n样本外表现最好的一组已保存到 reports/best_params.json")
        print("如需使用，把其中内容复制到 config.json 的 \"strategy\" 里。")
        print("提示：只有样本外也赚钱的参数才值得用；样本内很好、样本外亏钱 = 过拟合。")


if __name__ == "__main__":
    main()
