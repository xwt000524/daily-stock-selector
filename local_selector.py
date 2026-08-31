# -*- coding: utf-8 -*-
"""本地竞价选股器。

用途：
    在竞价结束后运行三份聚宽策略的选股部分，只输出候选股票，不执行任何交易。

默认数据源：
    AkShare -> 东方财富日线、涨停池和盘前分时数据。

说明：
    聚宽的 valuation、概念和证券状态接口没有直接的本地等价物。
    本程序用昨日涨停池缩小每日扫描范围，并用涨停池的所属行业作为热点概念代理。
    这样适合每天 09:26 后运行；如需完整复刻全市场扫描，可替换 Provider 的候选池实现。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time as time_module
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import akshare as ak
import numpy as np
import pandas as pd


LOGGER = logging.getLogger("local_selector")


def to_float(value: Any, default: float = float("nan")) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def clean_code(value: Any) -> str:
    return str(value).strip().zfill(6)


def is_mainboard(code: str) -> bool:
    code = clean_code(code)
    return code.startswith(("00", "60"))


def limit_price(prev_close: float, limit_pct: float = 0.10) -> float:
    """计算非 ST 主板的涨停价，避免 Python round 的二进制边界误差。"""
    return math.floor(prev_close * (1 + limit_pct) * 100 + 0.5) / 100


def is_limit_close(close: float, prev_close: float, tolerance: float = 0.002) -> bool:
    if close <= 0 or prev_close <= 0:
        return False
    return close >= limit_price(prev_close) * (1 - tolerance)


def normalize_name(name: Any) -> str:
    return str(name or "").strip().upper()


def is_excluded_name(name: Any) -> bool:
    normalized = normalize_name(name)
    return "ST" in normalized or "*" in normalized or "退" in normalized


def parse_date(value: str | date | None) -> date:
    if value is None:
        return datetime.now().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def json_safe(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if pd.isna(value):
        return None
    return value


@dataclass
class Auction:
    code: str
    trade_date: str
    price: float
    volume: float
    amount: float
    timestamp: str


@dataclass
class Pick:
    strategy: str
    trade_date: str
    code: str
    name: str
    score: float
    prev_close: float
    auction_price: float
    auction_change_pct: float
    auction_volume: float
    auction_volume_ratio: float
    auction_timestamp: str
    turnover_pct: float | None = None
    consecutive_boards: int | None = None
    matched_rule: str | None = None
    notes: str = ""


class Cache:
    def __init__(self, root: Path, refresh: bool = False) -> None:
        self.root = root
        self.refresh = refresh
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
        return self.root / f"{safe_key}.pkl"

    def read(self, key: str) -> Any:
        path = self.path(key)
        if self.refresh or not path.exists():
            return None
        try:
            return pd.read_pickle(path)
        except Exception:
            LOGGER.warning("缓存损坏，重新请求: %s", path)
            return None

    def write(self, key: str, value: Any) -> Any:
        value.to_pickle(self.path(key))
        return value


class AkshareProvider:
    """对 AkShare 做一层稳定化和缓存封装。"""

    def __init__(
        self,
        cache_dir: Path,
        request_interval: float = 0.15,
        refresh: bool = False,
    ) -> None:
        self.cache = Cache(cache_dir, refresh=refresh)
        self.request_interval = request_interval
        self._last_request = 0.0

    def _call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            elapsed = time_module.monotonic() - self._last_request
            if elapsed < self.request_interval:
                time_module.sleep(self.request_interval - elapsed)
            try:
                result = func(*args, **kwargs)
                self._last_request = time_module.monotonic()
                return result
            except Exception as exc:
                last_error = exc
                self._last_request = time_module.monotonic()
                if attempt < 2:
                    time_module.sleep(1.0 * (attempt + 1))
        raise RuntimeError(
            f"数据请求失败: {getattr(func, '__name__', func)}: {last_error}"
        ) from last_error

    def trade_days(self) -> list[date]:
        cached = self.cache.read("trade_days")
        if cached is None:
            cached = self._call(ak.tool_trade_date_hist_sina)
            self.cache.write("trade_days", cached)
        return [pd.Timestamp(v).date() for v in cached["trade_date"].tolist()]

    def previous_trade_day(self, current: date) -> date:
        days = [v for v in self.trade_days() if v < current]
        if not days:
            raise ValueError(f"找不到 {current} 之前的交易日")
        return days[-1]

    def limit_up_pool(self, trade_date: date) -> pd.DataFrame:
        key = f"zt_pool_{trade_date:%Y%m%d}"
        cached = self.cache.read(key)
        if cached is not None:
            return cached
        result = self._call(ak.stock_zt_pool_em, date=trade_date.strftime("%Y%m%d"))
        if result is None:
            result = pd.DataFrame()
        result = result.copy()
        if not result.empty:
            result["代码"] = result["代码"].map(clean_code)
            for column in ("最新价", "涨跌幅", "成交额", "流通市值", "总市值", "换手率", "连板数"):
                if column in result:
                    result[column] = pd.to_numeric(result[column], errors="coerce")
        return self.cache.write(key, result)

    def stock_names(self) -> pd.DataFrame:
        cached = self.cache.read("stock_names")
        if cached is None:
            cached = self._call(ak.stock_info_a_code_name)
            cached = cached.copy()
            cached["code"] = cached["code"].map(clean_code)
            self.cache.write("stock_names", cached)
        return cached

    def daily_history(self, code: str, end_date: date, count: int = 260) -> pd.DataFrame:
        code = clean_code(code)
        cache_count = max(count, 260)
        cache_start_date = end_date - timedelta(days=max(cache_count * 2, 400))
        key = f"daily_{code}_{end_date:%Y%m%d}_full"
        cached = self.cache.read(key)
        if cached is None:
            result = self._call(
                ak.stock_zh_a_hist,
                symbol=code,
                start_date=cache_start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="",
            )
            cached = self._normalize_daily(result, code)
            self.cache.write(key, cached)
        return cached.tail(count).reset_index(drop=True)

    @staticmethod
    def _normalize_daily(data: pd.DataFrame, code: str) -> pd.DataFrame:
        if data is None or data.empty:
            return pd.DataFrame(
                columns=["date", "open", "close", "high", "low", "volume", "amount", "turnover"]
            )
        rename = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "换手率": "turnover",
        }
        result = data.rename(columns=rename).copy()
        required = ["date", "open", "close", "high", "low", "volume", "amount", "turnover"]
        for column in required:
            if column not in result:
                result[column] = np.nan
        result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
        for column in required[1:]:
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result["code"] = code
        return result[required + ["code"]].dropna(subset=["date"]).sort_values("date")

    def auction(self, code: str, trade_date: date) -> Auction | None:
        key = f"auction_{clean_code(code)}_{trade_date:%Y%m%d}"
        cached = self.cache.read(key)
        if cached is None:
            result = self._call(
                ak.stock_zh_a_hist_pre_min_em,
                symbol=clean_code(code),
                start_time="09:15:00",
                end_time="09:26:00",
            )
            cached = result if result is not None else pd.DataFrame()
            self.cache.write(key, cached)
        if cached is None or cached.empty:
            return None
        data = cached.copy()
        data["时间"] = pd.to_datetime(data["时间"], errors="coerce")
        data = data[data["时间"].dt.time <= datetime.strptime("09:26:00", "%H:%M:%S").time()]
        data = data.dropna(subset=["时间"]).sort_values("时间")
        if data.empty:
            return None
        row = data.iloc[-1]
        # 09:26 行的“收盘”是该分钟最终成交价；“最新价”可能是接口快照字段。
        price = to_float(row.get("收盘"), to_float(row.get("最新价")))
        volume = to_float(row.get("成交量"), 0.0)
        amount = to_float(row.get("成交额"), 0.0)
        if price <= 0:
            return None
        return Auction(
            code=clean_code(code),
            trade_date=trade_date.isoformat(),
            price=price,
            volume=max(volume, 0.0),
            amount=max(amount, 0.0),
            timestamp=pd.Timestamp(row["时间"]).isoformat(),
        )


class Selector:
    def __init__(self, provider: AkshareProvider) -> None:
        self.provider = provider
        self.funnel: dict[str, dict[str, int]] = {}

    def _log_funnel(self, strategy: str, **values: int) -> None:
        self.funnel[strategy] = values

    @staticmethod
    def _pool_rows(pool: pd.DataFrame, mainboard_only: bool = True) -> list[dict[str, Any]]:
        if pool is None or pool.empty:
            return []
        rows: list[dict[str, Any]] = []
        for _, row in pool.iterrows():
            code = clean_code(row.get("代码"))
            name = str(row.get("名称", code))
            if mainboard_only and not is_mainboard(code):
                continue
            if is_excluded_name(name):
                continue
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "market_cap": to_float(row.get("流通市值")),
                    "total_market_cap": to_float(row.get("总市值")),
                    "amount": to_float(row.get("成交额")),
                    "turnover": to_float(row.get("换手率")),
                    "boards": int(to_float(row.get("连板数"), 1)),
                    "industry": str(row.get("所属行业", "")),
                }
            )
        return rows

    def _history(self, code: str, previous_day: date, count: int) -> pd.DataFrame:
        try:
            return self.provider.daily_history(code, previous_day, count=count)
        except RuntimeError as exc:
            LOGGER.warning("跳过历史数据失败的股票 %s: %s", code, exc)
            return pd.DataFrame()

    @staticmethod
    def _is_first_board(history: pd.DataFrame) -> bool:
        if len(history) < 2:
            return False
        current = history.iloc[-1]
        previous = history.iloc[-2]
        return bool(
            is_limit_close(to_float(current["close"]), to_float(previous["close"]))
            and not is_limit_close(to_float(previous["close"]), to_float(history.iloc[-3]["close"]))
            if len(history) >= 3
            else is_limit_close(to_float(current["close"]), to_float(previous["close"]))
        )

    @staticmethod
    def _consecutive_boards(history: pd.DataFrame) -> int:
        if len(history) < 2:
            return 0
        count = 0
        for index in range(len(history) - 1, 0, -1):
            current = history.iloc[index]
            previous = history.iloc[index - 1]
            if is_limit_close(to_float(current["close"]), to_float(previous["close"])):
                count += 1
            else:
                break
        return count

    @staticmethod
    def _annual_limit_count(history: pd.DataFrame) -> int:
        count = 0
        for index in range(1, len(history)):
            if is_limit_close(to_float(history.iloc[index]["close"]), to_float(history.iloc[index - 1]["close"])):
                count += 1
        return count

    @staticmethod
    def _extreme_limit_count(history: pd.DataFrame) -> int:
        count = 0
        for index in range(1, len(history)):
            row = history.iloc[index]
            previous_close = to_float(history.iloc[index - 1]["close"])
            limit = limit_price(previous_close)
            is_limit = to_float(row["close"]) >= limit * 0.998
            is_yizi = to_float(row["low"]) >= limit * 0.998 and is_limit
            is_tizi = to_float(row["open"]) >= limit * 0.998 and is_limit and to_float(row["low"]) < limit * 0.998
            count += int(is_yizi or is_tizi)
        return count

    def _auction_metrics(
        self, code: str, previous_day: date, trade_date: date
    ) -> tuple[float, Auction | None, float]:
        history = self._history(code, previous_day, count=2)
        if history.empty:
            return float("nan"), None, float("nan")
        previous_close = to_float(history.iloc[-1]["close"])
        previous_volume = to_float(history.iloc[-1]["volume"])
        try:
            auction = self.provider.auction(code, trade_date)
        except RuntimeError as exc:
            LOGGER.warning("跳过竞价数据失败的股票 %s: %s", code, exc)
            return previous_close, None, float("nan")
        ratio = auction.volume / previous_volume if auction and previous_volume > 0 else float("nan")
        return previous_close, auction, ratio

    def select_low_first_board(self, trade_date: date, previous_day: date) -> list[Pick]:
        pool = self.provider.limit_up_pool(previous_day)
        rows = self._pool_rows(pool)
        base_rows = [
            row for row in rows
            if 3_000_000_000 <= row["market_cap"] <= 30_000_000_000
        ]
        first_board: list[dict[str, Any]] = []
        for row in base_rows:
            history = self._history(row["code"], previous_day, count=4)
            if self._is_first_board(history):
                first_board.append(row)

        volume_price: list[dict[str, Any]] = []
        for row in first_board:
            history = self._history(row["code"], previous_day, count=35)
            if len(history) < 6:
                continue
            last, d1, d2, d3 = history.iloc[-1], history.iloc[-2], history.iloc[-3], history.iloc[-4]
            if not 5e8 <= to_float(last["amount"]) <= 30e8:
                continue
            if to_float(d1["volume"]) <= 0 or to_float(last["volume"]) < to_float(d1["volume"]) * 2:
                continue
            if to_float(d2["volume"]) <= 0 or to_float(d1["volume"]) > to_float(d2["volume"]) * 2:
                continue
            if not (to_float(d1["close"]) > to_float(d1["open"]) and to_float(d2["close"]) > to_float(d2["open"])):
                continue
            if to_float(d2["close"]) <= 0 or to_float(d3["close"]) <= 0:
                continue
            gain_1 = to_float(d1["close"]) / to_float(d2["close"]) - 1
            gain_2 = to_float(d2["close"]) / to_float(d3["close"]) - 1
            if gain_1 >= 0.05 or gain_2 >= 0.05:
                continue
            volume_price.append(row)

        volatility: list[dict[str, Any]] = []
        for row in volume_price:
            history = self._history(row["code"], previous_day, count=5)
            if history.empty:
                continue
            low = to_float(history["low"].min())
            high = to_float(history["high"].max())
            if low > 0 and (high - low) / low <= 0.20:
                volatility.append(row)

        picks: list[Pick] = []
        for row in volatility:
            previous_close, auction, auction_ratio = self._auction_metrics(row["code"], previous_day, trade_date)
            if auction is None or previous_close <= 0 or not math.isfinite(auction_ratio):
                continue
            price_ratio = auction.price / previous_close
            if auction_ratio < 0.03 or not (1.00 < price_ratio < 1.06):
                continue
            picks.append(
                Pick(
                    strategy="低位首阳必买",
                    trade_date=trade_date.isoformat(),
                    code=row["code"],
                    name=row["name"],
                    score=auction_ratio,
                    prev_close=previous_close,
                    auction_price=auction.price,
                    auction_change_pct=(price_ratio - 1) * 100,
                    auction_volume=auction.volume,
                    auction_volume_ratio=auction_ratio,
                    auction_timestamp=auction.timestamp,
                    turnover_pct=to_float(row["turnover"], None),
                    consecutive_boards=1,
                    matched_rule="竞价量>=昨日成交量3%，竞价涨幅0%~6%",
                    notes="聚宽 filter_first_board + filter_volume_price + filter_high_volatility",
                )
            )
        self._log_funnel(
            "低位首阳必买",
            涨停池=len(rows),
            流通市值=len(base_rows),
            首板=len(first_board),
            量价=len(volume_price),
            波动=len(volatility),
            竞价=len(picks),
        )
        return picks

    def _hot_industries(self, pool_rows: list[dict[str, Any]]) -> set[str]:
        scores: dict[str, int] = {}
        for row in pool_rows:
            industry = row["industry"].strip()
            if industry and row["boards"] >= 2:
                scores[industry] = scores.get(industry, 0) + row["boards"]
        return {name for name, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:5]}

    @staticmethod
    def _volume_surge_ok(history: pd.DataFrame) -> bool:
        """迁移竞价量比策略的 calculate_zyts + 历史放量条件。"""
        if len(history) < 3:
            return False
        highs = history["high"].to_numpy(dtype=float)
        volumes = history["volume"].to_numpy(dtype=float)
        previous_high = highs[-1]
        lookback = 100
        for index, high in enumerate(highs[-3::-1], 2):
            if high >= previous_high:
                lookback = index - 1
                break
        window = volumes[-(lookback + 5):]
        if len(window) < 2:
            return False
        return volumes[-1] > np.max(window[:-1]) * 0.9

    def select_board_breakout(self, trade_date: date, previous_day: date) -> list[Pick]:
        pool = self.provider.limit_up_pool(previous_day)
        rows = self._pool_rows(pool)
        # 市场情绪统计使用完整涨停池；候选股票仍只保留主板。
        all_pool_rows = self._pool_rows(pool, mainboard_only=False)
        max_boards = max((row["boards"] for row in all_pool_rows), default=0)
        if max_boards < 4:
            self._log_funnel("打板", 昨日主板涨停=len(rows), 市场最高连板=max_boards, 情绪空仓=1, 竞价=0)
            return []

        hot_industries = self._hot_industries(rows)
        ma_rows: list[dict[str, Any]] = []
        limit_rows: list[dict[str, Any]] = []
        board_rows: list[dict[str, Any]] = []
        annual_rows: list[dict[str, Any]] = []
        turnover_rows: list[dict[str, Any]] = []
        slope_rows: list[dict[str, Any]] = []
        theme_rows: list[dict[str, Any]] = []

        for row in rows:
            history = self._history(row["code"], previous_day, count=260)
            if len(history) < 30:
                continue
            close = history["close"].to_numpy(dtype=float)
            ma5 = np.mean(close[-5:])
            ma10 = np.mean(close[-10:])
            ma30 = np.mean(close[-30:])
            if not (ma5 > ma10 > ma30):
                continue
            ma_rows.append(row)
            if not is_limit_close(close[-1], close[-2]):
                continue
            limit_rows.append(row)
            boards = self._consecutive_boards(history)
            if boards > 3:
                continue
            board_rows.append(row)
            if self._annual_limit_count(history.tail(250)) < 11:
                continue
            annual_rows.append(row)
            turnover = to_float(history.iloc[-1]["turnover"])
            if not 5 <= turnover <= 15:
                continue
            row = dict(row)
            row["turnover"] = turnover
            turnover_rows.append(row)
            simulated_ma5 = np.mean(list(close[-4:]) + [limit_price(close[-1])])
            slope = (simulated_ma5 - ma5) / ma5 * 100 if ma5 else 0
            if slope < 2:
                continue
            row["slope"] = slope
            slope_rows.append(row)
            if boards == 3 and row["industry"].strip() not in hot_industries:
                continue
            row["boards_calculated"] = boards
            theme_rows.append(row)

        picks: list[Pick] = []
        excluded = 0
        for row in theme_rows:
            previous_close, auction, auction_ratio = self._auction_metrics(row["code"], previous_day, trade_date)
            if auction is None or previous_close <= 0:
                excluded += 1
                continue
            gap = auction.price / previous_close - 1
            hl = limit_price(previous_close)
            if gap < 0.03 or auction.price >= hl * 0.998:
                excluded += 1
                continue
            picks.append(
                Pick(
                    strategy="打板",
                    trade_date=trade_date.isoformat(),
                    code=row["code"],
                    name=row["name"],
                    score=to_float(row.get("slope"), 0),
                    prev_close=previous_close,
                    auction_price=auction.price,
                    auction_change_pct=gap * 100,
                    auction_volume=auction.volume,
                    auction_volume_ratio=auction_ratio,
                    auction_timestamp=auction.timestamp,
                    turnover_pct=to_float(row.get("turnover"), None),
                    consecutive_boards=int(row.get("boards_calculated", row["boards"])),
                    matched_rule="高开>=3%，非一字板；3板需命中热点行业代理",
                    notes="聚宽概念映射用昨日涨停池所属行业作为热点代理",
                )
            )
        self._log_funnel(
            "打板",
            昨日主板涨停=len(rows),
            市场最高连板=max_boards,
            MA多头=len(ma_rows),
            涨停=len(limit_rows),
            三板以内=len(board_rows),
            年涨停大于10=len(annual_rows),
            换手5到15=len(turnover_rows),
            斜率大于2=len(slope_rows),
            热点过滤=len(theme_rows),
            竞价=len(picks),
            竞价剔除=excluded,
        )
        return picks

    def select_auction_ratio(self, trade_date: date, previous_day: date) -> list[Pick]:
        pool = self.provider.limit_up_pool(previous_day)
        rows = self._pool_rows(pool)
        step_1: list[dict[str, Any]] = []
        step_2: list[dict[str, Any]] = []
        step_3: list[dict[str, Any]] = []
        step_4: list[dict[str, Any]] = []
        step_5: list[dict[str, Any]] = []
        for row in rows:
            history = self._history(row["code"], previous_day, count=110)
            if len(history) < 6:
                continue
            if to_float(history.iloc[-1]["close"]) / to_float(history.iloc[-2]["close"]) - 1 <= 0.07:
                continue
            step_1.append(row)
            if self._extreme_limit_count(history.tail(10)) >= 3:
                continue
            step_2.append(row)
            recent = history.tail(5)
            low = to_float(recent["low"].min())
            high = to_float(recent["high"].max())
            if low <= 0 or (high - low) / low > 0.40:
                continue
            step_3.append(row)
            if sum(
                is_limit_close(to_float(recent.iloc[i]["close"]), to_float(recent.iloc[i - 1]["close"]))
                for i in range(1, len(recent))
            ) >= 4:
                continue
            step_4.append(row)
            previous_close = to_float(history.iloc[-1]["close"])
            old_high = to_float(history.iloc[:-1]["high"].tail(100).max())
            if old_high <= 0 or previous_close < old_high * 0.90:
                continue
            step_5.append(row)

        picks: list[Pick] = []
        condition_rules = [
            ("C", 1.04, 1.07, 0.03, 0.07),
            ("D", 1.04, 1.07, 0.10, 0.20),
            ("E", 1.00, 1.04, 0.03, 0.07),
            ("F", 1.00, 1.04, 0.07, 0.10),
        ]
        for row in step_5:
            previous_close, auction, auction_ratio = self._auction_metrics(row["code"], previous_day, trade_date)
            if auction is None or previous_close <= 0 or not math.isfinite(auction_ratio):
                continue
            history = self._history(row["code"], previous_day, count=260)
            if history.empty:
                continue
            previous = history.iloc[-1]
            amount = to_float(previous["amount"])
            volume = to_float(previous["volume"])
            average_change = (
                amount / volume / previous_close * 1.1 - 1
                if volume > 0 and previous_close > 0
                else float("nan")
            )
            if not math.isfinite(average_change) or average_change < 0.07:
                continue
            if auction.price <= 3:
                continue
            if (
                to_float(row.get("total_market_cap")) < 1_000_000_000
                or to_float(row.get("market_cap")) > 52_000_000_000
            ):
                continue
            if not 1e8 <= amount <= 15e8:
                continue
            if not self._volume_surge_ok(history):
                continue
            current_ratio = auction.price / previous_close
            matched = next(
                (
                    name
                    for name, open_low, open_high, volume_low, volume_high in condition_rules
                    if open_low < current_ratio <= open_high and volume_low <= auction_ratio <= volume_high
                ),
                None,
            )
            if matched is None:
                continue
            picks.append(
                Pick(
                    strategy="竞价量比",
                    trade_date=trade_date.isoformat(),
                    code=row["code"],
                    name=row["name"],
                    score=auction_ratio,
                    prev_close=previous_close,
                    auction_price=auction.price,
                    auction_change_pct=(current_ratio - 1) * 100,
                    auction_volume=auction.volume,
                    auction_volume_ratio=auction_ratio,
                    auction_timestamp=auction.timestamp,
                    turnover_pct=to_float(row["turnover"], None),
                    consecutive_boards=int(row["boards"]),
                    matched_rule=matched,
                    notes="原策略 C/D/E/F 竞价规则；候选范围按昨日涨停池收敛",
                )
            )
        self._log_funnel(
            "竞价量比",
            昨日主板涨停=len(rows),
            昨日涨幅大于7=len(step_1),
            去极端涨停=len(step_2),
            去五日波动=len(step_3),
            去五日连板=len(step_4),
            近100日高=len(step_5),
            竞价=len(picks),
        )
        return picks

    def select_all(self, trade_date: date, strategies: Sequence[str]) -> list[Pick]:
        previous_day = self.provider.previous_trade_day(trade_date)
        LOGGER.info("选股日期: %s，前一交易日: %s", trade_date, previous_day)
        result: list[Pick] = []
        selected = set(strategies)
        if "低位首阳必买" in selected or "all" in selected:
            result.extend(self.select_low_first_board(trade_date, previous_day))
        if "打板" in selected or "all" in selected:
            result.extend(self.select_board_breakout(trade_date, previous_day))
        if "竞价量比" in selected or "all" in selected:
            result.extend(self.select_auction_ratio(trade_date, previous_day))
        return sorted(result, key=lambda item: (item.strategy, -item.score, item.code))


def write_results(picks: list[Pick], funnel: dict[str, dict[str, int]], output_dir: Path, trade_date: date) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_dir / f"picks_{trade_date:%Y%m%d}"
    csv_path = stem.with_suffix(".csv")
    json_path = stem.with_suffix(".json")
    columns = [
        "strategy", "trade_date", "code", "name", "score", "prev_close",
        "auction_price", "auction_change_pct", "auction_volume",
        "auction_volume_ratio", "turnover_pct", "consecutive_boards",
        "auction_timestamp", "matched_rule", "notes",
    ]
    pd.DataFrame([asdict(pick) for pick in picks], columns=columns).to_csv(
        csv_path, index=False, encoding="utf-8-sig"
    )
    payload = {
        "trade_date": trade_date.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(picks),
        "funnel": funnel,
        "picks": [json_safe(asdict(pick)) for pick in picks],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="竞价结束后本地选股器")
    parser.add_argument("--date", help="选股日期，格式 YYYY-MM-DD，默认今天")
    parser.add_argument(
        "--strategy",
        nargs="+",
        choices=["all", "低位首阳必买", "打板", "竞价量比"],
        default=["all"],
        help="要运行的策略，默认全部",
    )
    parser.add_argument("--cache-dir", default="cache", help="行情缓存目录")
    parser.add_argument("--output-dir", default="output", help="CSV/JSON 输出目录")
    parser.add_argument("--request-interval", type=float, default=0.50, help="请求间隔秒数")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，重新拉取行情")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        trade_date = parse_date(args.date)
        provider = AkshareProvider(
            Path(args.cache_dir),
            request_interval=args.request_interval,
            refresh=args.refresh,
        )
        selector = Selector(provider)
        picks = selector.select_all(trade_date, args.strategy)
        csv_path, json_path = write_results(picks, selector.funnel, Path(args.output_dir), trade_date)
        print("\n选股结果")
        print("=" * 72)
        if not picks:
            print("没有符合条件的股票。")
        else:
            for pick in picks:
                print(
                    f"[{pick.strategy}] {pick.code} {pick.name} "
                    f"竞价涨幅={pick.auction_change_pct:.2f}% "
                    f"竞价量比={pick.auction_volume_ratio:.2%} "
                    f"时间={pick.auction_timestamp} "
                    f"规则={pick.matched_rule or '-'}"
                )
        print("=" * 72)
        print(f"CSV : {csv_path}")
        print(f"JSON: {json_path}")
        print("\n选股漏斗")
        for strategy, values in selector.funnel.items():
            print(f"  {strategy}: " + ", ".join(f"{key}={value}" for key, value in values.items()))
        return 0
    except Exception as exc:
        LOGGER.error("%s", exc)
        if args.verbose:
            LOGGER.exception("详细错误")
        return 1


if __name__ == "__main__":
    sys.exit(main())
