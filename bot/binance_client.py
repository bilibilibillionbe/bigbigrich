"""币安 U 本位合约 (USDⓈ-M Futures) REST 客户端。

只依赖 requests。条件单（止损）使用 2025-12-09 之后强制的 Algo 接口
POST /fapi/v1/algoOrder；旧的 /fapi/v1/order 下 STOP_MARKET 会被拒绝 (-4120)。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

LIVE_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"


class BinanceError(Exception):
    def __init__(self, status, code, msg):
        super().__init__(f"HTTP {status} code={code} msg={msg}")
        self.status = status
        self.code = code
        self.msg = msg


class SymbolRules:
    """交易对精度规则：数量步长、价格步长、最小名义价值。"""

    def __init__(self, info: dict):
        self.symbol = info["symbol"]
        f = {x["filterType"]: x for x in info["filters"]}
        self.tick = Decimal(f["PRICE_FILTER"]["tickSize"])
        lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
        self.step = Decimal(lot["stepSize"])
        self.min_qty = Decimal(lot["minQty"])
        self.max_qty = Decimal(lot["maxQty"])
        self.min_notional = Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "5"))

    @staticmethod
    def _fmt(d: Decimal) -> str:
        s = format(d.normalize(), "f")
        return s

    def qty(self, q: float) -> str:
        d = (Decimal(str(q)) / self.step).to_integral_value(ROUND_DOWN) * self.step
        d = min(d, self.max_qty)
        return self._fmt(d)

    def price(self, p: float, up: bool = False) -> str:
        mode = ROUND_UP if up else ROUND_DOWN
        d = (Decimal(str(p)) / self.tick).to_integral_value(mode) * self.tick
        return self._fmt(d)


class BinanceFutures:
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = True,
                 timeout: int = 15, recv_window: int = 10000):
        self.base = TESTNET_URL if testnet else LIVE_URL
        self.key = api_key
        self.secret = api_secret.encode()
        self.timeout = timeout
        self.recv_window = recv_window
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-MBX-APIKEY"] = api_key
        self.time_offset = 0
        self._rules: dict[str, SymbolRules] = {}

    # ------------------------------------------------------------------ core
    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False, retries: int = 3):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(retries):
            p = dict(params)
            if signed:
                p["timestamp"] = int(time.time() * 1000) + self.time_offset
                p["recvWindow"] = self.recv_window
                qs = urlencode(p)
                sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
                qs += "&signature=" + sig
            else:
                qs = urlencode(p)
            url = self.base + path + ("?" + qs if qs else "")
            try:
                r = self.session.request(method, url, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("网络错误 %s %s: %s (第%d次)", method, path, e, attempt + 1)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r.json()
            try:
                j = r.json()
                code, msg = j.get("code"), j.get("msg")
            except ValueError:
                code, msg = None, r.text[:200]
            # 时间戳偏差：重新同步后重试
            if code == -1021:
                self.sync_time()
                continue
            # 限频 / 服务器错误：退避重试
            if r.status_code in (418, 429) or r.status_code >= 500:
                log.warning("服务器/限频 %s %s -> %s %s", method, path, r.status_code, msg)
                time.sleep(5 * (attempt + 1))
                continue
            raise BinanceError(r.status_code, code, msg)
        raise BinanceError(0, None, f"{method} {path} 多次重试失败")

    def sync_time(self):
        t = self._request("GET", "/fapi/v1/time", retries=3)["serverTime"]
        self.time_offset = int(t - time.time() * 1000)
        log.info("与服务器时间偏差 %d ms", self.time_offset)

    # ---------------------------------------------------------------- public
    def rules(self, symbol: str) -> SymbolRules:
        if not self._rules:
            info = self._request("GET", "/fapi/v1/exchangeInfo")
            for s in info["symbols"]:
                if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING":
                    self._rules[s["symbol"]] = SymbolRules(s)
        if symbol not in self._rules:
            raise ValueError(f"找不到可交易的永续合约 {symbol}")
        return self._rules[symbol]

    def klines(self, symbol: str, interval: str, limit: int = 500,
               start_time: int | None = None, end_time: int | None = None):
        return self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
            "startTime": start_time, "endTime": end_time})

    def mark_price(self, symbol: str) -> float:
        return float(self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])

    # --------------------------------------------------------------- account
    def usdt_balance(self) -> tuple[float, float]:
        """返回 (钱包余额, 可用余额)。"""
        for b in self._request("GET", "/fapi/v2/balance", signed=True):
            if b["asset"] == "USDT":
                return float(b["balance"]), float(b["availableBalance"])
        return 0.0, 0.0

    def position(self, symbol: str) -> dict:
        """返回 {amt, entry, upnl}；amt>0 多头，<0 空头，0 无持仓（单向持仓模式）。"""
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        amt = entry = upnl = 0.0
        for r in rows:
            if r["symbol"] == symbol and r.get("positionSide", "BOTH") == "BOTH":
                amt = float(r["positionAmt"])
                entry = float(r["entryPrice"])
                upnl = float(r.get("unRealizedProfit", 0))
        return {"amt": amt, "entry": entry, "upnl": upnl}

    def is_hedge_mode(self) -> bool:
        return bool(self._request("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"])

    def set_leverage(self, symbol: str, leverage: int):
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_isolated(self, symbol: str):
        try:
            self._request("POST", "/fapi/v1/marginType",
                          {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BinanceError as e:
            if e.code != -4046:  # -4046: 已经是逐仓，无需修改
                raise

    def realized_pnl_since(self, symbol: str, start_ms: int) -> float:
        """自 start_ms 起该币种的已实现盈亏 + 手续费 + 资金费（净值）。"""
        total = 0.0
        for t in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            rows = self._request("GET", "/fapi/v1/income", {
                "symbol": symbol, "incomeType": t, "startTime": start_ms, "limit": 1000}, signed=True)
            total += sum(float(r["income"]) for r in rows)
        return total

    # ---------------------------------------------------------------- orders
    def market_order(self, symbol: str, side: str, qty: str, reduce_only: bool = False) -> dict:
        return self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
            "reduceOnly": "true" if reduce_only else None,
            "newOrderRespType": "RESULT"}, signed=True)

    def place_stop(self, symbol: str, side: str, trigger_price: str) -> int:
        """服务器端止损（整仓平仓），用标记价格触发，防插针误触。返回 algoId。"""
        r = self._request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "triggerPrice": trigger_price,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
        }, signed=True)
        return int(r["algoId"])

    def cancel_algo(self, algo_id: int):
        try:
            self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)
        except BinanceError as e:
            log.info("撤销止损单 %s 失败（可能已触发/已撤）：%s", algo_id, e)

    def cancel_all_algo(self, symbol: str):
        try:
            self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)
        except BinanceError as e:
            log.info("撤销 %s 全部条件单：%s", symbol, e)

    def open_algo_orders(self, symbol: str) -> list[dict]:
        r = self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        if isinstance(r, dict):
            r = r.get("orders") or r.get("rows") or []
        return r
