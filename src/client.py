# -*- coding: utf-8 -*-
"""
HTTP 会话层 —— 所有网络请求都从这里出去。

这一层负责四件事，缺一不可：
  1. 伪装成浏览器（UA / Referer / Accept-Language），并先访问 init 页拿 cookie。
  2. 动态发现余票查询路径。12306 的路径末端字母会轮换（queryG / queryZ / ...），
     写死就会失效——init 页的 JS 里带着当前有效的那个。
  3. 限速 + 重试 + 预算控制。12306 大约 3~5 请求/秒就会风控，必须压住。
     被 403 时**绝不重试**，重试只会让封禁更深。
  4. 统一识别"HTTP 200 但内容是错误页"这种坑（参考项目只在部分接口处理了）。

#20260928Abel
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

import requests

from . import config as C
from . import logs


class TrainTicketError(Exception):
    """本项目的统一异常基类。"""


class ApiFormatError(TrainTicketError):
    """接口返回了 200，但内容不是我们预期的结构（多半是错误页或改版）。"""


class RiskControlError(TrainTicketError):
    """触发了 12306 风控（403/429）。这种情况不要重试。"""


class RequestBudgetExceeded(TrainTicketError):
    """请求次数超过预算上限。"""


class Budget:
    """
    请求预算。

    只统计**真正发出去的**网络请求；缓存命中的次数单独记，不算预算。
    这样用户看到的"发了 N 次"就是真实的外网请求次数。
    """

    def __init__(self, max_requests: int = C.DEFAULT_MAX_REQUESTS) -> None:
        self.max_requests = int(max_requests)
        self.used = 0
        self.cached_hits = 0
        self.by_kind: dict[str, int] = {}

    @property
    def left(self) -> int:
        """还剩多少次额度。"""
        return max(0, self.max_requests - self.used)

    def spend(self, kind: str = "other") -> None:
        """记一次请求。超预算就抛异常，由上层决定怎么降级。"""
        if self.used >= self.max_requests:
            raise RequestBudgetExceeded(
                f"已达请求上限 {self.max_requests} 次，停止继续探测。"
                f"可用 --max-requests 调大后重跑。"
            )
        self.used += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def hit_cache(self) -> None:
        """记一次缓存命中（不消耗预算）。"""
        self.cached_hits += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_requests": self.max_requests,
            "used": self.used,
            "cached_hits": self.cached_hits,
            "by_kind": dict(self.by_kind),
        }


class HttpSession:
    """
    带限速、重试、预算的请求会话。

    用法：整个程序共用一个实例（在 cli.py 里创建后往下传），
    这样限速才是全局生效的，而不是每个模块各限各的。
    """

    def __init__(
        self,
        budget: Budget | None = None,
        min_interval: float = C.MIN_REQUEST_INTERVAL,
        dry_run: bool = False,
    ) -> None:
        self.budget = budget or Budget()
        # 命令行也不允许把间隔调到硬下限以下——那是在拿 IP 冒险
        self.min_interval = max(float(min_interval), C.MIN_INTERVAL_HARD_FLOOR)
        self.dry_run = dry_run

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": C.USER_AGENT,
                "Referer": C.REFERER,
                "Accept-Language": C.ACCEPT_LANGUAGE,
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self._last_request_at = 0.0
        self._initialized = False
        self._left_ticket_path: str | None = None
        self._path_rotated_warned = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        """限速：保证两次请求之间至少间隔 min_interval 秒（带抖动）。"""
        wait = self.min_interval + random.uniform(0, C.REQUEST_JITTER)
        elapsed = time.time() - self._last_request_at
        if elapsed < wait:
            time.sleep(wait - elapsed)

    def _raw_get(self, url: str, kind: str) -> requests.Response:
        """真正发请求的地方。预算、限速、重试都在这里收口。"""
        if self.dry_run:
            logs.detail(f"[dry-run] 本应请求：{url}")
            raise ApiFormatError("dry-run 模式不发真实请求")

        last_error: Exception | None = None
        for attempt in range(1, C.MAX_RETRY + 1):
            self.budget.spend(kind)
            self._throttle()
            try:
                logs.detail(f"GET {url}")
                resp = self._session.get(
                    url, timeout=(C.CONNECT_TIMEOUT, C.READ_TIMEOUT)
                )
                self._last_request_at = time.time()
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._last_request_at = time.time()
                last_error = exc
                backoff = C.RETRY_BACKOFF_BASE**attempt
                logs.warn(f"网络异常（第 {attempt}/{C.MAX_RETRY} 次）：{exc}，{backoff:.0f}s 后重试")
                time.sleep(backoff)
                continue

            # 风控：不重试。继续撞只会让封禁更久。
            if resp.status_code in (403, 429):
                raise RiskControlError(
                    f"12306 返回 {resp.status_code}，已触发风控。"
                    f"请等待 5~15 分钟再试，或用 --min-interval 2 降低频率。"
                )
            if resp.status_code >= 500:
                last_error = ApiFormatError(f"HTTP {resp.status_code}")
                backoff = C.RETRY_BACKOFF_BASE**attempt
                logs.warn(f"服务端 {resp.status_code}（第 {attempt}/{C.MAX_RETRY} 次），{backoff:.0f}s 后重试")
                time.sleep(backoff)
                continue

            resp.raise_for_status()
            return resp

        raise ApiFormatError(f"请求重试 {C.MAX_RETRY} 次仍失败：{url}（{last_error}）")

    def get_text(self, url: str, kind: str = "other") -> str:
        """取纯文本（用于 JS 数据文件）。"""
        return self._raw_get(url, kind).text

    def get_json(self, url: str, kind: str = "other") -> dict[str, Any]:
        """
        取 JSON，并统一处理 12306 的几种"假成功"：
          · 200 但 body 根本不是 JSON 对象
          · 200 但 data 字段是字符串（那是错误页 HTML）
          · 返回体里带 c_url —— 表示查询路径轮换了，要用新的重试
        """
        resp = self._raw_get(url, kind)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiFormatError(
                f"响应不是 JSON（可能是错误页）：{resp.text[:120]!r}"
            ) from exc

        if not isinstance(payload, dict):
            raise ApiFormatError(f"响应不是 JSON 对象：{str(payload)[:120]}")

        # 路径轮换：把新的路径交回给调用方重试
        if payload.get("c_url"):
            raise LeftTicketPathRotated(str(payload["c_url"]))

        data = payload.get("data")
        if isinstance(data, str):
            raise ApiFormatError(f"接口返回错误页（data 是字符串）：{data[:120]!r}")
        return payload

    # ------------------------------------------------------------------
    # 会话初始化：cookie + 轮换路径
    # ------------------------------------------------------------------
    def ensure_initialized(self) -> None:
        """
        首次调用时访问 init 页：拿 cookie，并解析出当前有效的余票查询路径。

        参考项目是在模块导入时就 await 这个动作，导致 12306 不可达时整个进程
        直接崩。这里改成按需惰性执行，失败也能给出人话提示。
        """
        if self._initialized:
            return

        try:
            html = self.get_text(C.LEFT_TICKET_INIT_URL, kind="init")
        except TrainTicketError as exc:
            raise ApiFormatError(f"访问 12306 初始化页失败，请检查网络：{exc}") from exc

        match = re.search(r"CLeftTicketUrl\s*=\s*'([^']+)'", html)
        if match:
            self._left_ticket_path = match.group(1).strip()
            logs.detail(f"发现余票查询路径：{self._left_ticket_path}")
        else:
            # 解析不到就退回默认值，后面靠 c_url 自愈兜底
            self._left_ticket_path = C.DEFAULT_LEFT_TICKET_PATH
            logs.warn(
                "未能从初始化页解析出余票查询路径，改用默认值 "
                f"{C.DEFAULT_LEFT_TICKET_PATH}（若失败会尝试 c_url 自愈）"
            )

        self._initialized = True

    @property
    def left_ticket_path(self) -> str:
        self.ensure_initialized()
        assert self._left_ticket_path is not None
        return self._left_ticket_path

    def set_left_ticket_path(self, path: str) -> None:
        """用 c_url 返回的新路径更新内部状态。"""
        cleaned = path.strip()
        if cleaned.startswith("/otn/"):
            cleaned = cleaned[len("/otn/") :]
        elif cleaned.startswith("otn/"):
            cleaned = cleaned[len("otn/") :]
        if cleaned.startswith("/"):
            cleaned = cleaned[1:]
        self._left_ticket_path = cleaned

    def left_ticket_url(self, query: str) -> str:
        """拼出完整的余票查询 URL。"""
        return f"{C.API_BASE}/otn/{self.left_ticket_path}?{query}"

    def get_left_ticket_json(self, query: str) -> dict[str, Any]:
        """
        查余票，并处理路径轮换的自愈。

        实测：请求到失效路径时，12306 会返回
            {"c_url": "leftTicket/queryG", "c_name": "CLeftTicketUrl", "status": false}
        读出 c_url 换个路径重试即可，不用人工介入。
        """
        self.ensure_initialized()
        try:
            return self.get_json(self.left_ticket_url(query), kind="leftTicket")
        except LeftTicketPathRotated as rotated:
            if not self._path_rotated_warned:
                logs.warn(
                    f"余票查询路径已轮换：{self._left_ticket_path} -> {rotated.new_path}，已自动切换"
                )
                self._path_rotated_warned = True
            self.set_left_ticket_path(rotated.new_path)
            return self.get_json(self.left_ticket_url(query), kind="leftTicket")


class LeftTicketPathRotated(ApiFormatError):
    """内部信号：响应里带 c_url，说明查询路径变了。"""

    def __init__(self, new_path: str) -> None:
        super().__init__(f"余票查询路径已轮换为 {new_path}")
        self.new_path = new_path
