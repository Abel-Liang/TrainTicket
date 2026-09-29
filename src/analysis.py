# -*- coding: utf-8 -*-
"""
替代方案分析 —— 本项目的核心功能。

当用户想坐的车次全程无票时，看看有没有"曲线救国"的办法。共四条路：

  A 买短乘长（上车补票）
      买 S_a→S_j 的票（j 在目的地之前），先上车，再找列车长补到 S_b。
      风险：补票段通常无座，且列车超员时列车长有权拒绝补票。

  B 买长乘短
      买一段比实际行程更长的票，在 S_a 上车、S_b 下车，两头没坐的区段作废。
      子类：B1 提前上车（多买上游）、B2 全程票（始发→终到）、B3 后退下车。
      **实测这是区间限售下最常奏效的一条**——短区间被锁死时，全程票往往还开着。

  C 同车分段两张票
      买 S_a→S_j 和 S_j→S_b 两张票，同一台车，中途换个座位。有座，比补票稳。

  D 候补
      该车次支持候补时告知用户。**只报告，不代下单**（候补需要登录）。

【请求预算的关键设计】
朴素做法是"每趟车 × 每个可能区间"各发一次请求。但实测发现余票接口按城市对返回
数据，一次请求就覆盖该区段所有车次、所有市内站组合。所以这里把探测区间**按站对
去重**收集起来再统一执行：一个区间只发一次请求，多趟车共用结果。
开销因此从 O(车次 × 站数) 降到 O(去重后的区间数)。

【诚实降级】
没找到方案时，必须把"探测过哪些区间"原样打印出来。绝不能让用户误以为程序找到了
方案，也绝不能因为探测没跑全就宣称"没有方案"。
#20260928Abel
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config as C
from . import logs
from .client import Budget, HttpSession, RequestBudgetExceeded, TrainTicketError
from .parser import SeatOffer, TicketRow
from .route import RouteStop, TrainRouteService, index_of_station
from .stations import StationTable
from .ticket import LeftTicketService

# 策略标识
STRAT_BUY_SHORT = "buy_short"
STRAT_BUY_LONG = "buy_long"
STRAT_SPLIT = "same_train_split"
STRAT_HOUBU = "houbu"

STRATEGY_CN = {
    STRAT_BUY_SHORT: "买短乘长（上车补票）",
    STRAT_BUY_LONG: "买长乘短",
    STRAT_SPLIT: "同车分段（两张票）",
    STRAT_HOUBU: "候补购票",
}


@dataclass
class BuySegment:
    """方案里实际要买的一段票。"""

    from_name: str
    from_tele: str
    from_time: str
    to_name: str
    to_tele: str
    to_time: str
    seat: SeatOffer

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": {"name": self.from_name, "telecode": self.from_tele, "time": self.from_time},
            "to": {"name": self.to_name, "telecode": self.to_tele, "time": self.to_time},
            "seat": self.seat.to_dict(),
        }


@dataclass
class Plan:
    """一条完整的替代方案。"""

    strategy: str
    strategy_cn: str
    train_code: str
    internal_train_no: str

    board_name: str          # 用户实际上车的站
    board_time: str
    alight_name: str         # 用户实际下车的站
    alight_time: str

    buy_segments: list[BuySegment] = field(default_factory=list)
    total_price: float | None = None
    uncovered_stops: int = 0       # 需要站着的站数（买短乘长时 = 补票跨越的站数）
    onboard_segment: str = ""      # 需要上车补票的区段（策略 A）
    wasted_segment: str = ""       # 票面买了但不会乘坐的区段（策略 B1/B2）
    overrun_segment: str = ""      # 坐到票面到站之后的区段（策略 B3）
    houbu_seats: list[str] = field(default_factory=list)   # 策略 D
    risks: list[str] = field(default_factory=list)
    evidence: str = ""             # 这条结论是怎么得出来的（可追溯）
    rank_key: tuple = field(default_factory=tuple, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_cn": self.strategy_cn,
            "train_code": self.train_code,
            "board": {"name": self.board_name, "time": self.board_time},
            "alight": {"name": self.alight_name, "time": self.alight_time},
            "buy_segments": [s.to_dict() for s in self.buy_segments],
            "total_price": self.total_price,
            "uncovered_stops": self.uncovered_stops,
            "onboard_segment": self.onboard_segment or None,
            "wasted_segment": self.wasted_segment or None,
            "overrun_segment": self.overrun_segment or None,
            "houbu_seats": self.houbu_seats or None,
            "risks": self.risks,
            "evidence": self.evidence,
        }


@dataclass
class Unmatched:
    """某个策略没能给出方案，以及为什么（用于诚实降级展示）。"""

    train_code: str
    strategy: str
    strategy_cn: str
    probed: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_code": self.train_code,
            "strategy": self.strategy,
            "strategy_cn": self.strategy_cn,
            "probed": self.probed,
            "reason": self.reason,
        }


@dataclass
class AnalysisResult:
    plans: list[Plan] = field(default_factory=list)
    analyzed_trains: list[str] = field(default_factory=list)
    skipped_trains: list[tuple[str, str]] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)
    probes_run: list[str] = field(default_factory=list)
    probes_dropped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trains_analyzed": self.analyzed_trains,
            "trains_skipped": [{"train_code": c, "reason": r} for c, r in self.skipped_trains],
            "plans": [p.to_dict() for p in self.plans],
            "unmatched": [u.to_dict() for u in self.unmatched],
            "probes_run": self.probes_run,
            "probes_dropped": self.probes_dropped,
            "notes": self.notes,
        }


@dataclass
class Options:
    """分析相关的可调参数。"""

    max_analyze_trains: int = C.DEFAULT_MAX_ANALYZE_TRAINS
    max_candidate_stops: int = C.DEFAULT_MAX_CANDIDATE_STOPS
    max_back_stops: int = C.DEFAULT_MAX_BACK_STOPS
    allow_buy_short: bool = True
    allow_buy_long: bool = True
    allow_split: bool = True
    allow_houbu: bool = True


@dataclass
class _Probe:
    """一个待探测的站对。"""

    from_tele: str
    to_tele: str
    from_name: str
    to_name: str
    kinds: set[str] = field(default_factory=set)   # 哪些策略需要它
    consumers: set[str] = field(default_factory=set)  # 哪些车次需要它
    gap: int = 0        # 覆盖行程的长短，用于排序


class Analyzer:
    """替代方案分析器。"""

    def __init__(
        self,
        session: HttpSession,
        stations: StationTable,
        tickets: LeftTicketService,
        routes: TrainRouteService,
        options: Options | None = None,
    ) -> None:
        self.session = session
        self.stations = stations
        self.tickets = tickets
        self.routes = routes
        self.opt = options or Options()

    # ------------------------------------------------------------------
    def analyze(
        self,
        date: str,
        from_tele: str,
        to_tele: str,
        tickets: list[TicketRow],
    ) -> AnalysisResult:
        """
        对无票车次做替代方案分析。

        tickets —— 直达查询的结果（已经过滤到用户指定的站对和时间窗）
        """
        result = AnalysisResult()

        # 只分析无票的车次；有票的已经能直接买，分析是锦上添花
        no_ticket = [t for t in tickets if not t.has_ticket]
        # 无票的排前面，其次按发车时间
        no_ticket.sort(key=lambda t: t.start_time)

        budget: Budget = self.session.budget
        picked: list[TicketRow] = []
        for idx, row in enumerate(no_ticket):
            if idx >= self.opt.max_analyze_trains:
                result.skipped_trains.append((row.train_code, "max_analyze_trains"))
                continue
            if budget.left <= 0:
                result.skipped_trains.append((row.train_code, "request_budget"))
                continue
            picked.append(row)

        if not picked:
            return result

        # ---- 取经停站 ----
        route_map: dict[str, list[RouteStop]] = {}
        for i, row in enumerate(picked):
            if budget.left <= 0:
                for rest in picked[i:]:
                    result.skipped_trains.append((rest.train_code, "request_budget"))
                break
            try:
                route_map[row.train_code] = self.routes.get_route(date, row.internal_train_no)
            except TrainTicketError as exc:
                logs.warn(f"{row.train_code} 经停站获取失败，跳过分析：{exc}")
                result.skipped_trains.append((row.train_code, "route_unavailable"))

        analyzed = [r for r in picked if r.train_code in route_map]
        result.analyzed_trains = [r.train_code for r in analyzed]
        if not analyzed:
            return result

        # ---- 枚举探测区间（按站对去重）----
        probes, positions = self._enumerate_probes(analyzed, route_map, result)

        # ---- 执行探测（按优先级，受预算约束）----
        self._run_probes(date, probes, result)

        # ---- 组装方案 ----
        for row in analyzed:
            stops = route_map[row.train_code]
            ia, ib = positions[row.train_code]
            self._build_plans(row, stops, ia, ib, date, probes, result)

        self._rank(result)
        if result.probes_dropped:
            result.notes.append(
                f"因请求预算不足，有 {len(result.probes_dropped)} 个区间未能探测，"
                f"结果可能不完整。可用 --max-requests 调大后重跑。"
            )
        return result

    # ------------------------------------------------------------------
    def _enumerate_probes(
        self,
        trains: list[TicketRow],
        route_map: dict[str, list[RouteStop]],
        result: AnalysisResult,
    ) -> tuple[dict[tuple[str, str], _Probe], dict[str, tuple[int, int]]]:
        """
        收集所有需要探测的站对，按 (上站, 下站) 去重。

        同一区间若被多趟车需要，只会出现在字典里一次——这是预算优化的关键。
        """
        probes: dict[tuple[str, str], _Probe] = {}
        positions: dict[str, tuple[int, int]] = {}

        def add(kind: str, train_code: str, a: RouteStop, b: RouteStop, gap: int) -> None:
            if not a.telecode or not b.telecode:
                return  # 车站表里查不到电报码，无法探测
            if a.telecode == b.telecode:
                return
            key = (a.telecode, b.telecode)
            probe = probes.get(key)
            if probe is None:
                probe = _Probe(a.telecode, b.telecode, a.name, b.name, gap=gap)
                probes[key] = probe
            probe.kinds.add(kind)
            probe.consumers.add(train_code)
            probe.gap = max(probe.gap, gap)

        for row in trains:
            stops = route_map[row.train_code]
            ia = index_of_station(stops, row.from_tele, "")
            ib = index_of_station(stops, row.to_tele, "")
            if ia is None or ib is None or ia >= ib:
                logs.warn(
                    f"{row.train_code} 的经停站里找不到 "
                    f"{row.from_tele} 或 {row.to_tele}，跳过该车次分析"
                )
                continue
            positions[row.train_code] = (ia, ib)

            # 策略 A / C：目的地之前的落客站，取离目的地最近的若干个
            middle = [k for k in range(ia + 1, ib) if stops[k].can_alight]
            if self.opt.max_candidate_stops > 0:
                middle = middle[-self.opt.max_candidate_stops :]
            for k in middle:
                # A：S_a -> S_j（买短）
                add(STRAT_BUY_SHORT, row.train_code, stops[ia], stops[k], gap=ib - k)
                # C：S_j -> S_b（分段的后半程）
                if self.opt.allow_split or self.opt.allow_buy_short:
                    add(STRAT_SPLIT, row.train_code, stops[k], stops[ib], gap=ib - k)

            # 策略 B：上游的办客站（提前上车）
            if self.opt.allow_buy_long:
                start = max(0, ia - self.opt.max_back_stops)
                for k in range(start, ia):
                    if stops[k].can_board:
                        add(STRAT_BUY_LONG, row.train_code, stops[k], stops[ib], gap=ib - k)

                # 策略 B2：始发 -> 终到。
                # 实测这是区间限售下**命中率最高**的一条，短区间被锁死时全程票往往还开着，
                # 所以即使它在最后一行也要确保被优先探测（由排序保证）。
                add(STRAT_BUY_LONG, row.train_code, stops[0], stops[-1], gap=ib - ia)

                # 策略 B3：坐到票面到站之后
                for k in range(ib + 1, min(len(stops), ib + self.opt.max_back_stops + 1)):
                    if stops[k].can_alight:
                        add(STRAT_BUY_LONG, row.train_code, stops[ia], stops[k], gap=ib - ia)

        return probes, positions

    # ------------------------------------------------------------------
    def _probe_priority(self, probe: _Probe) -> tuple:
        """
        探测优先级。预算不够时先跑这些。

        顺序：全程票最优先 —— 实测区间限售下短区间全被锁死，只有"始发→终到"有票，
        所以这条线命中率最高，先跑它性价比最好。然后是买短乘长（覆盖行程最长者优先）。
        """
        is_full_run = STRAT_BUY_LONG in probe.kinds and probe.gap == 0
        if is_full_run:
            kind_rank = 0
        elif STRAT_BUY_SHORT in probe.kinds:
            kind_rank = 1
        elif STRAT_SPLIT in probe.kinds:
            kind_rank = 2
        else:
            kind_rank = 3
        # gap 越小表示覆盖的行程越长（到达站离目的地越近），优先
        return (kind_rank, probe.gap, -len(probe.consumers))

    # ------------------------------------------------------------------
    def _run_probes(
        self,
        date: str,
        probes: dict[tuple[str, str], _Probe],
        result: AnalysisResult,
    ) -> None:
        """按优先级执行探测，受请求预算约束。截断绝不静默。"""
        ordered = sorted(probes.values(), key=self._probe_priority)
        budget: Budget = self.session.budget

        for i, probe in enumerate(ordered):
            label = f"{probe.from_name}→{probe.to_name}"
            if budget.left <= 0:
                result.probes_dropped.extend(
                    f"{p.from_name}→{p.to_name}" for p in ordered[i:]
                )
                logs.warn(
                    f"请求预算已用尽，剩余 {len(ordered) - i} 个区间未探测："
                    f"{'、'.join(p.from_name + '→' + p.to_name for p in ordered[i:])}"
                )
                break
            try:
                self.tickets.get(date, probe.from_tele, probe.to_tele)
                result.probes_run.append(label)
            except RequestBudgetExceeded:
                result.probes_dropped.extend(
                    f"{p.from_name}→{p.to_name}" for p in ordered[i:]
                )
                break
            except TrainTicketError as exc:
                logs.warn(f"区间 {label} 探测失败：{exc}")
                result.probes_dropped.append(label)

    # ------------------------------------------------------------------
    def _build_plans(
        self,
        row: TicketRow,
        stops: list[RouteStop],
        ia: int,
        ib: int,
        date: str,
        probes: dict[tuple[str, str], _Probe],
        result: AnalysisResult,
    ) -> None:
        """为一趟无票车次组装所有可行方案。"""
        code = row.train_code

        if self.opt.allow_buy_short:
            self._plan_buy_short(row, stops, ia, ib, date, result)
        if self.opt.allow_buy_long:
            self._plan_buy_long(row, stops, ia, ib, date, result)
        if self.opt.allow_split:
            self._plan_split(row, stops, ia, ib, date, result)
        if self.opt.allow_houbu and row.can_houbu:
            self._plan_houbu(row, stops[ia].name, stops[ib].name, result)

    # ------------------------------------------------------------------
    def _lookup(
        self, date: str, from_tele: str, to_tele: str, train_code: str
    ) -> TicketRow | None:
        """在已探测的缓存里找某车次在某站对上的行（不再发新请求）。"""
        try:
            return self.tickets.get_train(date, from_tele, to_tele, train_code)
        except TrainTicketError:
            return None

    # ------------------------------------------------------------------
    def _plan_buy_short(
        self,
        row: TicketRow,
        stops: list[RouteStop],
        ia: int,
        ib: int,
        date: str,
        result: AnalysisResult,
    ) -> None:
        """策略 A：买 S_a→S_j，然后上车补票到 S_b。"""
        code = row.train_code
        found = False
        probed: list[str] = []

        candidates = [k for k in range(ia + 1, ib) if stops[k].can_alight]
        if self.opt.max_candidate_stops > 0:
            candidates = candidates[-self.opt.max_candidate_stops :]

        for k in candidates:
            sj = stops[k]
            probed.append(f"{stops[ia].name}→{sj.name}")
            seg = self._lookup(date, stops[ia].telecode, sj.telecode, code)
            if seg is None or not seg.has_ticket:
                continue
            best = seg.cheapest_available()
            if best is None:
                continue
            found = True
            result.plans.append(
                Plan(
                    strategy=STRAT_BUY_SHORT,
                    strategy_cn=STRATEGY_CN[STRAT_BUY_SHORT],
                    train_code=code,
                    internal_train_no=row.internal_train_no,
                    board_name=stops[ia].name,
                    board_time=seg.start_time,
                    alight_name=stops[ib].name,
                    alight_time=row.arrive_time,
                    buy_segments=[
                        BuySegment(
                            from_name=stops[ia].name,
                            from_tele=stops[ia].telecode,
                            from_time=seg.start_time,
                            to_name=sj.name,
                            to_tele=sj.telecode,
                            to_time=seg.arrive_time,
                            seat=best,
                        )
                    ],
                    total_price=best.price,
                    # 补票要跨越的站数：越少越好，这是买短乘长最重要的一条排序依据
                    uncovered_stops=ib - k,
                    onboard_segment=f"{sj.name} → {stops[ib].name}",
                    risks=[
                        f"补票段（{sj.name}→{stops[ib].name}）通常只能补到无座票",
                        "必须在票面到站前主动找列车长声明补票，不要等查票",
                        "列车超员时列车长有权拒绝补票并要求下车",
                        "补票费用与手续费以 12306 官方规定和现场为准，本程序不做估算",
                    ],
                    evidence=(
                        f"区间「{stops[ia].name}→{sj.name}」实测有票"
                        f"（该次区间查询同时覆盖该线路多趟车次）"
                    ),
                )
            )

        if not found and probed:
            result.unmatched.append(
                Unmatched(
                    train_code=code,
                    strategy=STRAT_BUY_SHORT,
                    strategy_cn=STRATEGY_CN[STRAT_BUY_SHORT],
                    probed=probed,
                    reason="以上区间该车次均无票（常见于「区间限售」：短区间被锁死）",
                )
            )

    # ------------------------------------------------------------------
    def _plan_buy_long(
        self,
        row: TicketRow,
        stops: list[RouteStop],
        ia: int,
        ib: int,
        date: str,
        result: AnalysisResult,
    ) -> None:
        """策略 B：买比实际行程更长的一段票（提前上车 / 全程票 / 后退下车）。"""
        code = row.train_code
        found = False
        probed: list[str] = []

        # (起始站下标, 目的站下标, 子类名)
        combos: list[tuple[int, int, str]] = []
        # B2：始发 -> 终到（区间限售下命中率最高，排最前）
        combos.append((0, len(stops) - 1, "全程票（始发→终到）"))
        # B1：提前上车
        for k in range(max(0, ia - self.opt.max_back_stops), ia):
            if stops[k].can_board:
                combos.append((k, ib, "提前上车"))
        # B3：后退下车
        for k in range(ib + 1, min(len(stops), ib + self.opt.max_back_stops + 1)):
            if stops[k].can_alight:
                combos.append((ia, k, "后退下车"))

        seen: set[tuple[int, int]] = set()
        for i, j, kind in combos:
            if (i, j) in seen or (i == ia and j == ib):
                continue
            seen.add((i, j))
            si, sj = stops[i], stops[j]
            probed.append(f"{si.name}→{sj.name}")
            seg = self._lookup(date, si.telecode, sj.telecode, code)
            if seg is None or not seg.has_ticket:
                continue
            best = seg.cheapest_available()
            if best is None:
                continue
            found = True

            risks = []
            wasted = ""
            overrun = ""
            if i < ia:
                wasted = f"{si.name} → {stops[ia].name}"
                risks.append(
                    f"票面发站是 {si.name}，您将在 {stops[ia].name} 上车。"
                    f"闸机可能不放行，建议走人工通道并向工作人员说明"
                )
                risks.append(f"未乘区间（{wasted}）的席位在开车后可能被列车长处置")
            if j > ib:
                overrun = f"{stops[ib].name} → {sj.name}"
                risks.append(f"您将在 {stops[ib].name} 提前下车，票面到站之后的区间（{overrun}）不退票款")
            risks.append("买长乘短的实付金额高于实际行程票价，差价不退")

            result.plans.append(
                Plan(
                    strategy=STRAT_BUY_LONG,
                    strategy_cn=f"{STRATEGY_CN[STRAT_BUY_LONG]} · {kind}",
                    train_code=code,
                    internal_train_no=row.internal_train_no,
                    board_name=stops[ia].name,
                    board_time=row.start_time,
                    alight_name=stops[ib].name,
                    alight_time=row.arrive_time,
                    buy_segments=[
                        BuySegment(
                            from_name=si.name,
                            from_tele=si.telecode,
                            from_time=seg.start_time,
                            to_name=sj.name,
                            to_tele=sj.telecode,
                            to_time=seg.arrive_time,
                            seat=best,
                        )
                    ],
                    total_price=best.price,
                    wasted_segment=wasted,
                    overrun_segment=overrun,
                    risks=risks,
                    evidence=f"区间「{si.name}→{sj.name}」实测有票，而直达区间无票",
                )
            )

        if not found and probed:
            result.unmatched.append(
                Unmatched(
                    train_code=code,
                    strategy=STRAT_BUY_LONG,
                    strategy_cn=STRATEGY_CN[STRAT_BUY_LONG],
                    probed=probed,
                    reason="以上区间该车次均无票",
                )
            )

    # ------------------------------------------------------------------
    def _plan_split(
        self,
        row: TicketRow,
        stops: list[RouteStop],
        ia: int,
        ib: int,
        date: str,
        result: AnalysisResult,
    ) -> None:
        """策略 C：同车买两张票，中途换座。"""
        code = row.train_code
        probed: list[str] = []
        found = False

        candidates = [k for k in range(ia + 1, ib) if stops[k].can_alight]
        if self.opt.max_candidate_stops > 0:
            candidates = candidates[-self.opt.max_candidate_stops :]

        for k in candidates:
            sj = stops[k]
            label = f"{stops[ia].name}→{sj.name} + {sj.name}→{stops[ib].name}"
            probed.append(label)
            leg1 = self._lookup(date, stops[ia].telecode, sj.telecode, code)
            leg2 = self._lookup(date, sj.telecode, stops[ib].telecode, code)
            if not (leg1 and leg1.has_ticket and leg2 and leg2.has_ticket):
                continue
            seat1 = leg1.cheapest_available()
            seat2 = leg2.cheapest_available()
            if seat1 is None or seat2 is None:
                continue
            found = True
            result.plans.append(
                Plan(
                    strategy=STRAT_SPLIT,
                    strategy_cn=STRATEGY_CN[STRAT_SPLIT],
                    train_code=code,
                    internal_train_no=row.internal_train_no,
                    board_name=stops[ia].name,
                    board_time=leg1.start_time,
                    alight_name=stops[ib].name,
                    alight_time=row.arrive_time,
                    buy_segments=[
                        BuySegment(stops[ia].name, stops[ia].telecode, leg1.start_time,
                                   sj.name, sj.telecode, sj.arrive_time, seat1),
                        BuySegment(sj.name, sj.telecode, leg2.start_time,
                                   stops[ib].name, stops[ib].telecode, leg2.arrive_time, seat2),
                    ],
                    total_price=round(seat1.price + seat2.price, 1),
                    risks=[
                        f"两段票的座位极可能不在同一车厢，需要在 {sj.name} 换座",
                        f"{sj.name} 停靠 {sj.change_minutes} 分钟，请提前收拾行李",
                        "换座期间列车可能已经开动，注意安全",
                    ],
                    evidence=f"两段「{stops[ia].name}→{sj.name}」与「{sj.name}→{stops[ib].name}」均实测有票",
                )
            )

        if not found and probed:
            result.unmatched.append(
                Unmatched(
                    train_code=code,
                    strategy=STRAT_SPLIT,
                    strategy_cn=STRATEGY_CN[STRAT_SPLIT],
                    probed=probed,
                    reason="未找到两段同时有票的组合",
                )
            )

    # ------------------------------------------------------------------
    def _plan_houbu(
        self, row: TicketRow, board_name: str, alight_name: str, result: AnalysisResult
    ) -> None:
        """
        策略 D：候补。

        数据就在直达查询那一行里，不需要任何额外请求。
        **只报告，不代下单**——提交候补需要登录 12306。
        """
        seats = row.houbu_seat_names
        result.plans.append(
            Plan(
                strategy=STRAT_HOUBU,
                strategy_cn=STRATEGY_CN[STRAT_HOUBU],
                train_code=row.train_code,
                internal_train_no=row.internal_train_no,
                board_name=board_name,
                board_time=row.start_time,
                alight_name=alight_name,
                alight_time=row.arrive_time,
                houbu_seats=seats,
                risks=[
                    "候补需要在 12306 登录后手动提交，本程序只读、不代下单",
                    "候补能否兑现取决于退票与放票情况，不保证成功",
                ],
                evidence=(
                    f"12306 在该区段给出了可候补席别（{'、'.join(seats)}），"
                    f"说明该车次这一段支持候补"
                ),
            )
        )

    # ------------------------------------------------------------------
    def _rank(self, result: AnalysisResult) -> None:
        """
        排序。越靠前越推荐。

        优先级：买短乘长（能坐完全程且最省钱）> 同车分段（有座更稳）>
                买长乘短（贵但有票）> 候补（最不确定）

        同策略内部，**先看要站多久，再看花多少钱**。
        只按价格排会把「买上海虹桥→太仓 ¥31」排到「买上海虹桥→苏州 ¥46」前面，
        可前者要在车上多站两个站补票，体验差得多。所以 uncovered_stops 排在价格之前。
        """
        base = {
            STRAT_BUY_SHORT: 0,
            STRAT_SPLIT: 1,
            STRAT_BUY_LONG: 2,
            STRAT_HOUBU: 3,
        }

        def key(p: Plan) -> tuple:
            standing = sum(1 for seg in p.buy_segments if seg.seat.is_standing)
            return (
                base.get(p.strategy, 9),
                1 if p.total_price is None else 0,   # 价格已知的排前面
                p.uncovered_stops,                   # 要站多久：越少越好
                standing,
                p.total_price if p.total_price is not None else 0.0,
                p.board_time or "99:99",
            )

        result.plans.sort(key=key)
