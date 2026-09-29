# -*- coding: utf-8 -*-
"""
分析器离线测试。

用构造出来的数据验证四种策略的判定与排序，不联网、不依赖线上实时票务。
线上的"有没有票"每天都在变，但"给定这些票务事实应当推出什么结论"是确定的，
所以这部分可以且应当被锁死。

场景对齐实测的 D2928：
    直达 上海虹桥→无锡        无票
    买短乘长 上海虹桥→昆山南   有票   <- 应当给出方案
    买长乘长 上海松江→烟台     有票   <- 应当给出方案（实测只有全程票有票）
    候补                      支持
#20260928Abel
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis import (  # noqa: E402
    STRAT_BUY_LONG,
    STRAT_BUY_SHORT,
    STRAT_HOUBU,
    STRAT_SPLIT,
    Analyzer,
    Options,
)
from src.client import Budget  # noqa: E402
from src.parser import SeatOffer, TicketRow  # noqa: E402
from src.route import RouteStop  # noqa: E402

# 简化版的 D2928 经停站（站序与电报码对齐实测数据）
ROUTE = [
    ("01", "上海松江", "IMH"),
    ("02", "上海虹桥", "AOH"),
    ("03", "昆山南", "KNH"),
    ("04", "苏州", "SZH"),
    ("05", "无锡", "WXH"),
    ("06", "常州", "CZH"),
    ("07", "烟台", "YAK"),
]
TIMES = {
    "IMH": ("----", "06:23"),
    "AOH": ("06:40", "06:52"),
    "KNH": ("07:12", "07:14"),
    "SZH": ("07:42", "07:44"),
    "WXH": ("07:59", "08:01"),
    "CZH": ("08:16", "08:18"),
    "YAK": ("13:56", "13:56"),
}


def make_stops() -> list[RouteStop]:
    stops = []
    for i, (no, name, tele) in enumerate(ROUTE):
        arr, dep = TIMES[tele]
        stops.append(
            RouteStop(
                station_no=int(no),
                name=name,
                telecode=tele,
                arrive_time=arr,
                start_time=dep,
                is_first=(i == 0),
                is_last=(i == len(ROUTE) - 1),
            )
        )
    return stops


def seat(name: str, short: str, price: float, count: int, code: str = "O") -> SeatOffer:
    from src.parser import format_count

    return SeatOffer(
        code=code,
        name=name,
        short=short,
        price=price,
        count=count,
        count_text=format_count(count),
        is_standing=(code == "W"),
    )


def row(
    f: str,
    t: str,
    fno: int,
    tno: int,
    seats: list[SeatOffer],
    houbu_flag: str = "0",
    houbu_seats: str = "",
) -> TicketRow:
    return TicketRow(
        train_code="D2928",
        internal_train_no="5q000D292810",
        from_tele=f,
        to_tele=t,
        start_tele="IMH",
        end_tele="YAK",
        from_station_no=fno,
        to_station_no=tno,
        start_time="06:52",
        arrive_time="07:59",
        lishi="01:07",
        can_web_buy="N",
        sale_time="202609181445",
        houbu_flag=houbu_flag,
        houbu_seats=houbu_seats,
        seats=seats,
    )


class FakeSession:
    def __init__(self, budget: Budget) -> None:
        self.budget = budget


class FakeTickets:
    """假的余票服务：只回答预先设定的站对，不发请求。"""

    def __init__(self, segments: dict[tuple[str, str], list[TicketRow]]) -> None:
        self.segments = segments

    def get(self, date: str, f: str, t: str) -> list[TicketRow]:
        return list(self.segments.get((f, t), []))

    def get_train(self, date: str, f: str, t: str, code: str) -> TicketRow | None:
        for item in self.segments.get((f, t), []):
            if item.train_code == code:
                return item
        return None


class FakeRoutes:
    def __init__(self, stops: list[RouteStop]) -> None:
        self.stops = stops

    def get_route(self, date: str, internal_train_no: str) -> list[RouteStop]:
        return self.stops


def build(
    direct_seats: list[SeatOffer],
    extra: dict[tuple[str, str], tuple[int, int, list[SeatOffer]]] | None = None,
    **kw,
) -> Analyzer:
    """
    组装一个分析器。

    direct_seats —— 直达区间的席别（通常全无票）
    extra        —— {(上站, 下站): (上站序, 下站序, 该区间的席别)}，代表探测结果
    """
    segments: dict[tuple[str, str], list[TicketRow]] = {
        ("AOH", "WXH"): [row("AOH", "WXH", 2, 5, direct_seats, **kw)],
    }
    for (f, t), (fno, tno, seats) in (extra or {}).items():
        segments[(f, t)] = [row(f, t, fno, tno, seats)]
    tickets = FakeTickets(segments)
    routes = FakeRoutes(make_stops())
    session = FakeSession(Budget(60))
    return Analyzer(session, None, tickets, routes, Options())  # type: ignore[arg-type]


NONE_AVAILABLE = [
    seat("二等座", "ze", 56.0, 0),
    seat("一等座", "zy", 89.0, 0),
]


class TestBuyShort(unittest.TestCase):
    """策略 A：买短乘长。"""

    def test_found_when_short_segment_has_ticket(self) -> None:
        analyzer = build(
            NONE_AVAILABLE,
            extra={("AOH", "KNH"): (2, 3, [seat("二等座", "ze", 22.0, 32)])},
        )
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        shorts = [p for p in result.plans if p.strategy == STRAT_BUY_SHORT]
        self.assertEqual(len(shorts), 1)
        plan = shorts[0]
        self.assertEqual(plan.buy_segments[0].from_name, "上海虹桥")
        self.assertEqual(plan.buy_segments[0].to_name, "昆山南")
        self.assertEqual(plan.onboard_segment, "昆山南 → 无锡")
        self.assertAlmostEqual(plan.total_price, 22.0)

    def test_unmatched_reports_probed_segments(self) -> None:
        """
        全部无票时必须给出"已探测区间"清单。

        这是诚实降级的硬要求：用户得能判断程序是真的确认没方案，
        还是根本没搜完。没有这个清单，沉默就等于撒谎。
        """
        analyzer = build(
            NONE_AVAILABLE,
            extra={("AOH", "KNH"): (2, 3, [seat("二等座", "ze", 22.0, 0)])},
        )
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        self.assertEqual([p for p in result.plans if p.strategy == STRAT_BUY_SHORT], [])
        unmatched = [u for u in result.unmatched if u.strategy == STRAT_BUY_SHORT]
        self.assertEqual(len(unmatched), 1)
        self.assertTrue(any("昆山南" in s for s in unmatched[0].probed))


class TestBuyLong(unittest.TestCase):
    """策略 B：买长乘短（实测区间限售下最常奏效的一条）。"""

    def test_full_run_ticket_found(self) -> None:
        analyzer = build(
            NONE_AVAILABLE,
            extra={("IMH", "YAK"): (1, 7, [seat("无座", "wz", 419.0, 30, code="W")])},
        )
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        longs = [p for p in result.plans if p.strategy == STRAT_BUY_LONG]
        self.assertEqual(len(longs), 1)
        plan = longs[0]
        self.assertEqual(plan.buy_segments[0].from_name, "上海松江")
        self.assertEqual(plan.buy_segments[0].to_name, "烟台")
        self.assertEqual(plan.board_name, "上海虹桥", "上车点应是用户实际所在站")
        self.assertEqual(plan.alight_name, "无锡", "下车点应是用户目的地")
        self.assertEqual(plan.wasted_segment, "上海松江 → 上海虹桥")
        self.assertEqual(plan.overrun_segment, "无锡 → 烟台")

    def test_full_run_probed_first(self) -> None:
        """
        「始发→终到」必须排在最前面探测。

        实测区间限售会把短区间全锁死，只有全程票开着。预算不够时先跑它，
        性价比最高。
        """
        analyzer = build(NONE_AVAILABLE)
        stops = make_stops()
        from src.analysis import _Probe

        probes = {
            "a": _Probe("AOH", "KNH", "上海虹桥", "昆山南", kinds={STRAT_BUY_SHORT}, gap=2),
            "b": _Probe("IMH", "YAK", "上海松江", "烟台", kinds={STRAT_BUY_LONG}, gap=0),
            "c": _Probe("KNH", "WXH", "昆山南", "无锡", kinds={STRAT_SPLIT}, gap=2),
        }
        ordered = sorted(probes.values(), key=analyzer._probe_priority)
        self.assertEqual(ordered[0].from_name, "上海松江")
        self.assertEqual(ordered[0].to_name, "烟台")


class TestSplitAndHoubu(unittest.TestCase):
    """策略 C 与 D。"""

    def test_split_needs_both_legs(self) -> None:
        analyzer = build(
            NONE_AVAILABLE,
            extra={
                ("AOH", "KNH"): (2, 3, [seat("二等座", "ze", 22.0, 9)]),
                ("KNH", "WXH"): (3, 5, [seat("二等座", "ze", 34.0, 5)]),
            },
        )
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        splits = [p for p in result.plans if p.strategy == STRAT_SPLIT]
        self.assertEqual(len(splits), 1)
        self.assertAlmostEqual(splits[0].total_price, 56.0)
        self.assertEqual(len(splits[0].buy_segments), 2)

    def test_split_skipped_when_second_leg_empty(self) -> None:
        """只买得到前半程时不能算成分段方案——那样等于让用户中途下车。"""
        analyzer = build(
            NONE_AVAILABLE,
            extra={
                ("AOH", "KNH"): (2, 3, [seat("二等座", "ze", 22.0, 9)]),
                ("KNH", "WXH"): (3, 5, [seat("二等座", "ze", 34.0, 0)]),
            },
        )
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        self.assertEqual([p for p in result.plans if p.strategy == STRAT_SPLIT], [])

    def test_houbu_plan_has_no_buy_segments(self) -> None:
        """
        候补方案只做提示：没有可买的分段，也不该出现价格。

        程序只读、不代下单，所以必须明确告知用户需要自己去 12306 操作。
        """
        analyzer = build(NONE_AVAILABLE, houbu_flag="1", houbu_seats="OM")
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE,
                                       houbu_flag="1", houbu_seats="OM")])
        houbu = [p for p in result.plans if p.strategy == STRAT_HOUBU]
        self.assertEqual(len(houbu), 1)
        self.assertEqual(houbu[0].houbu_seats, ["二等座", "一等座"])
        self.assertEqual(houbu[0].buy_segments, [])
        self.assertIsNone(houbu[0].total_price)

    def test_no_houbu_plan_when_flag_zero(self) -> None:
        analyzer = build(NONE_AVAILABLE, houbu_flag="0")
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE,
                                       houbu_flag="0")])
        self.assertEqual([p for p in result.plans if p.strategy == STRAT_HOUBU], [])


class TestRanking(unittest.TestCase):
    """排序：能坐完全程且省钱的方式应该排在前面。"""

    def test_buy_short_ranks_before_buy_long(self) -> None:
        analyzer = build(
            NONE_AVAILABLE,
            extra={
                ("AOH", "KNH"): (2, 3, [seat("二等座", "ze", 22.0, 32)]),
                ("IMH", "YAK"): (1, 7, [seat("无座", "wz", 419.0, 30, code="W")]),
            },
        )
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        self.assertGreaterEqual(len(result.plans), 2)
        self.assertEqual(result.plans[0].strategy, STRAT_BUY_SHORT)

    def test_prefer_longer_coverage_over_cheaper_short_segment(self) -> None:
        """
        买短乘长要在「站得少」和「花钱少」之间取舍时，**站得少优先**。

        只按价格排会把「买上海虹桥→昆山南 ¥22」排到「买上海虹桥→苏州 ¥60」前面，
        但前者要多站两站补票。实测里 D2216 的「买太仓 ¥31」就曾经错误地压过
        「买苏州 ¥46」，这条用例锁死修正后的行为。
        """
        analyzer = build(
            NONE_AVAILABLE,
            extra={
                # 近的落客站便宜但补票距离长
                ("AOH", "KNH"): (2, 3, [seat("二等座", "ze", 22.0, 40)]),
                # 远的落客站贵但几乎不用站
                ("AOH", "SZH"): (2, 4, [seat("二等座", "ze", 60.0, 40)]),
            },
        )
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        shorts = [p for p in result.plans if p.strategy == STRAT_BUY_SHORT]
        self.assertEqual(len(shorts), 2)
        self.assertEqual(shorts[0].buy_segments[0].to_name, "苏州",
                         "覆盖行程更长（补票站数更少）的方案应排在前面")
        self.assertLess(shorts[0].uncovered_stops, shorts[1].uncovered_stops)

    def test_sold_out_train_not_analyzed_for_plans(self) -> None:
        """有票的车次不需要替代方案分析。"""
        available = [seat("二等座", "ze", 56.0, 10)]
        analyzer = build(available)
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, available)])
        self.assertEqual(result.plans, [])
        self.assertEqual(result.analyzed_trains, [])


class TestBudgetGuard(unittest.TestCase):
    """预算约束必须生效，且跳过要留痕。"""

    def test_skipped_trains_recorded(self) -> None:
        analyzer = build(NONE_AVAILABLE)
        analyzer.opt.max_analyze_trains = 0
        result = analyzer.analyze("2026-10-02", "AOH", "WXH",
                                  [row("AOH", "WXH", 2, 5, NONE_AVAILABLE)])
        self.assertEqual(result.plans, [])
        self.assertTrue(
            any(reason == "max_analyze_trains" for _, reason in result.skipped_trains),
            "被上限挡掉的车次必须记录在案，不能静默跳过",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
