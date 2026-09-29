# -*- coding: utf-8 -*-
"""
余票查询服务。

【核心机制：城市对查询 + 多键缓存】
实测发现，12306 的余票接口会把传入的电报码**按城市解析**：
传 AOH(上海虹桥) 和传 SHH(上海) 返回的是**完全相同**的 311 行，里面覆盖了
上海所有车站 × 无锡所有车站的组合（每个市内站对各占一行）。

这带来两个巨大的好处：

  1. **一次请求就够**。要拿「上海虹桥→无锡」，不需要按站发请求，发一次城市对
     查询再按 from/to 电报码过滤行即可。朴素做法是 O(车次 × 站数) 次请求，
     实际只需要 O(候选城市数) 次。

  2. **一次响应能填充几十个缓存键**。所以这里把响应里出现的**每一个**
     (上车站, 下车站) 组合都登记进缓存，后续探测直接命中内存。

第 2 点是请求预算优化的落点，改动时务必留意：如果改成"只缓存请求时用的那个
站对"，请求数会成倍上涨。

另外还做了**负缓存**：如果某个城市对已经抓过，而目标站对不在里面，就说明确实
没有该组合的车次，不必再发一次请求。
#20260928Abel
"""

from __future__ import annotations

from typing import Any

from . import config as C
from . import logs
from .client import HttpSession, TrainTicketError
from .parser import TicketRow, parse_rows
from .stations import StationTable


class LeftTicketService:
    """余票查询 + 缓存。整个程序共用一个实例。"""

    def __init__(self, session: HttpSession, stations: StationTable) -> None:
        self.session = session
        self.stations = stations

        # (date, from_tele, to_tele) -> 该站对的车次列表
        self._by_pair: dict[tuple[str, str, str], list[TicketRow]] = {}
        # (date, from_city, to_city) -> 是否已抓过（用于负缓存）
        self._fetched_cities: set[tuple[str, str, str]] = set()
        # 电报码 -> 中文名（来自接口返回的 map，比本地车站表更贴近本次数据）
        self.station_map: dict[str, str] = {}
        # 解析失败的行摘要，最后统一汇报
        self.parse_failures: list[str] = []

    # ------------------------------------------------------------------
    def _city_key(self, telecode: str) -> str:
        """取电报码所属城市；查不到就用电报码本身（只是少了合并缓存的机会）。"""
        return self.stations.city_of(telecode) or telecode

    # ------------------------------------------------------------------
    def query_raw(self, date: str, from_tele: str, to_tele: str) -> list[TicketRow]:
        """
        按**城市对**发起一次真实请求，并把结果登记进所有相关站对的缓存。

        参数里的 from_tele / to_tele 只是用来确定城市对，用哪个站都一样。
        """
        query = (
            f"leftTicketDTO.train_date={date}"
            f"&leftTicketDTO.from_station={from_tele}"
            f"&leftTicketDTO.to_station={to_tele}"
            f"&purpose_codes=ADULT"
        )
        payload = self.session.get_left_ticket_json(query)
        data = payload.get("data") or {}
        raw_rows = data.get("result") or []

        # 接口会附带一个 电报码->中文名 的小字典，收下来给展示层用
        for code, name in (data.get("map") or {}).items():
            self.station_map.setdefault(code, name)

        rows, failed = parse_rows(raw_rows, self.station_map)
        if failed:
            self.parse_failures.extend(failed)
            logs.warn(
                f"有 {len(failed)} 行余票数据解析失败（已跳过，不影响其余结果）。"
                f"若数量较多，请运行 python tools/schema_probe.py 排查。"
            )

        # 【关键】把响应里出现的每个站对都登记进缓存
        for row in rows:
            self._by_pair.setdefault((date, row.from_tele, row.to_tele), []).append(row)

        # 标记该城市对已抓取（即使一行都没有，也是有效信息）
        self._fetched_cities.add(
            (date, self._city_key(from_tele), self._city_key(to_tele))
        )
        # 万一某行的城市不同（跨境车次等），也一并标记，避免重复抓
        for row in rows:
            self._fetched_cities.add(
                (date, self._city_key(row.from_tele), self._city_key(row.to_tele))
            )

        logs.detail(
            f"余票查询 {date} {from_tele}->{to_tele}：{len(raw_rows)} 行"
            f"，登记 {len({(r.from_tele, r.to_tele) for r in rows})} 个站对"
        )
        return rows

    # ------------------------------------------------------------------
    def get(self, date: str, from_tele: str, to_tele: str) -> list[TicketRow]:
        """
        查询指定站对的余票。

        先查缓存；缓存没有再看这个城市对是否抓过（负缓存）；都没有才发请求。
        """
        key = (date, from_tele, to_tele)
        if key in self._by_pair:
            self.session.budget.hit_cache()
            logs.detail(f"缓存命中 {from_tele}->{to_tele}")
            return list(self._by_pair[key])

        city_key = (date, self._city_key(from_tele), self._city_key(to_tele))
        if city_key in self._fetched_cities:
            # 该城市对已经抓过，里面没有这个站对 —— 确实没有该组合的车次
            self.session.budget.hit_cache()
            self._by_pair[key] = []
            logs.detail(f"负缓存命中 {from_tele}->{to_tele}（该城市对已抓取，无此站对）")
            return []

        self.query_raw(date, from_tele, to_tele)
        return list(self._by_pair.get(key, []))

    # ------------------------------------------------------------------
    def get_train(
        self, date: str, from_tele: str, to_tele: str, train_code: str
    ) -> TicketRow | None:
        """在某个站对里找特定车次。"""
        for row in self.get(date, from_tele, to_tele):
            if row.train_code == train_code:
                return row
        return None

    # ------------------------------------------------------------------
    def rows_for(
        self, date: str, from_allowed: set[str], to_allowed: set[str]
    ) -> list[TicketRow]:
        """
        从缓存里取出符合给定站集合的全部行。

        用于用户输入城市名（展开成多个车站）时汇总结果。之所以不加站对过滤外的
        额外请求：城市对查询已经把全市组合都拿回来了。
        """
        out: list[TicketRow] = []
        for (d, f, t), group in self._by_pair.items():
            if d == date and f in from_allowed and t in to_allowed:
                out.extend(group)
        return out

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """给 JSON 输出用的缓存统计。"""
        return {
            "cached_station_pairs": len(self._by_pair),
            "fetched_city_pairs": len(self._fetched_cities),
            "parse_failures": len(self.parse_failures),
        }
