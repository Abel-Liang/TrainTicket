# -*- coding: utf-8 -*-
"""
输出层 —— 文本表格与 JSON。

设计要点：
  · JSON 模式下 stdout **只有 JSON**，所有日志走 stderr，这样才能安全地管道化。
  · 中文字符是双宽的，直接用 str.ljust 对齐会错位，所以自己算显示宽度。
  · 没找到方案时，把"探测过哪些区间"一并打印。这不是装饰——用户必须能判断
    程序是真的确认了没有方案，还是根本没搜完。
#20260928Abel
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import logs
from .analysis import (
    STRAT_BUY_LONG,
    STRAT_BUY_SHORT,
    STRAT_HOUBU,
    STRAT_SPLIT,
    AnalysisResult,
    Plan,
)
from . import config as C
from .parser import TicketRow
from .route import RouteStop

# 制表符宽度不同的终端可以用 --ascii 降级成纯 ASCII 边框
_BOX = {
    False: {"h": "─", "tl": "═", "sep": "═"},
    True: {"h": "-", "tl": "=", "sep": "="},
}
_MARK = {False: "✅", True: "[OK]"}
_CROSS = {False: "❌", True: "[--]"}


def _ascii() -> bool:
    return logs.ascii_only()


def display_width(text: str) -> int:
    """算字符串在终端里占几列。中日韩字符和全角标点算 2 列。"""
    width = 0
    for ch in text:
        code = ord(ch)
        if (
            0x1100 <= code <= 0x115F
            or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
            or 0x20000 <= code <= 0x3FFFD
        ):
            width += 2
        else:
            width += 1
    return width


def pad(text: str, width: int) -> str:
    """按显示宽度左侧对齐补空格。"""
    gap = width - display_width(text)
    return text + " " * max(0, gap)


def rule(char: str = "─", width: int = 96) -> str:
    return char * width


# ----------------------------------------------------------------------
def render_query_table(
    rows: list[TicketRow],
    date: str,
    from_label: str,
    to_label: str,
    window: str,
    station_names: dict[str, str],
) -> str:
    """渲染余票车次表。"""
    lines: list[str] = []
    lines.append(rule("═"))
    lines.append(f"  12306 车票查询  |  {date}  |  {from_label} → {to_label}")
    if window:
        lines.append(f"  时间窗：{window}")
    lines.append(rule("═"))

    if not rows:
        lines.append("  （没有符合条件的车次）")
        return "\n".join(lines)

    headers = ["车次", "上车站", "发车", "下车站", "到达", "历时", "席别"]
    body: list[list[str]] = []
    for row in rows:
        from_name = station_names.get(row.from_tele, row.from_tele)
        to_name = station_names.get(row.to_tele, row.to_tele)
        if row.seats:
            seat_str = " | ".join(
                f"{s.name} ¥{s.price} {s.count_text}" for s in row.seats
            )
        else:
            seat_str = "（无席别数据）"
        if not row.has_ticket:
            # 「未到起售时间」和「已售完」对用户是两回事：前者等等就能买，
            # 后者才需要考虑替代方案。必须分开标注。
            seat_str += "   ← 未到起售时间" if row.not_on_sale_yet else "   ← 无票"
        elif row.not_on_sale_yet:
            seat_str += "   ← 未到起售时间"
        body.append(
            [row.train_code, from_name, row.start_time, to_name,
             row.arrive_time, row.lishi, seat_str]
        )

    # 列宽按内容自适应（末列不补空格，免得行尾一堆空白）
    widths = [display_width(h) for h in headers]
    for line in body:
        for i in range(len(headers) - 1):
            widths[i] = max(widths[i], display_width(line[i]))

    lines.append("".join(pad(h, widths[i]) + "  " for i, h in enumerate(headers[:-1])) + headers[-1])
    lines.append(rule())
    for line in body:
        lines.append(
            "".join(pad(line[i], widths[i]) + "  " for i in range(len(headers) - 1))
            + line[-1]
        )

    total = len(rows)
    sold_out = sum(1 for r in rows if not r.has_ticket)
    lines.append(rule())
    lines.append(f"共 {total} 趟车次；有票 {total - sold_out} 趟，无票 {sold_out} 趟。")
    return "\n".join(lines)


# ----------------------------------------------------------------------
def render_analysis(
    result: AnalysisResult,
    station_names: dict[str, str],
    show_risks: bool = True,
) -> str:
    """
    渲染替代方案分析。

    show_risks=False 时隐藏每条方案下面的 ⚠ 风险提示（票面发站、不退票款、
    补票可能被拒等）。这些提示信息量很大但很占地方，图形界面默认收起，
    命令行默认展开。注意：**隐藏的只是显示，方案本身和排序不受影响**。
    """
    lines: list[str] = []
    lines.append("")
    lines.append(rule("═"))
    lines.append("  替代方案分析")
    lines.append(rule("═"))

    if not result.analyzed_trains:
        lines.append("  （没有需要分析的无票车次）")
        return "\n".join(lines)

    # 按车次分组展示方案
    by_train: dict[str, list[Plan]] = {}
    for plan in result.plans:
        by_train.setdefault(plan.train_code, []).append(plan)

    analyzed = set(result.analyzed_trains)
    for code in result.analyzed_trains:
        lines.append("")
        plans = by_train.get(code, [])
        if plans:
            lines.append(f"【{code}】找到 {len(plans)} 条可行方案")
            for i, plan in enumerate(plans, 1):
                lines.extend(_render_plan(plan, i, len(plans), show_risks))
        else:
            lines.append(f"【{code}】未找到可行方案")

    # 未找到的策略：把探测过的区间列出来（诚实降级的硬要求）
    unmatched = [u for u in result.unmatched if u.train_code in analyzed]
    if unmatched:
        lines.append("")
        lines.append("  未找到方案的策略（已探测区间如下，并非没有搜索）：")
        for item in unmatched:
            lines.append(f"    {_CROSS[_ascii()]} {item.train_code} · {item.strategy_cn}")
            if item.probed:
                lines.append(f"       已探测：{'、'.join(item.probed)}")
            lines.append(f"       结论：{item.reason}")

    if result.skipped_trains:
        # 按原因归类，压成几行。逐条列出来会刷屏几十行，反而看不清重点。
        by_reason: dict[str, list[str]] = {}
        for code, reason in result.skipped_trains:
            by_reason.setdefault(reason, []).append(code)
        reason_cn = {
            "max_analyze_trains": "超过 --max-analyze-trains 上限",
            "request_budget": "请求预算不足",
            "route_unavailable": "经停站数据获取失败",
        }
        lines.append("")
        for reason, codes in by_reason.items():
            lines.append(
                f"  ⚠ 以下 {len(codes)} 趟未做分析（{reason_cn.get(reason, reason)}）："
                f"{'、'.join(codes)}"
            )

    for note in result.notes:
        lines.append("")
        lines.append(f"  ⚠ {note}")

    return "\n".join(lines)


def _render_plan(
    plan: Plan, index: int, total: int, show_risks: bool = True
) -> list[str]:
    """渲染单条方案。"""
    lines: list[str] = []
    star = " ★推荐" if index == 1 and plan.strategy != STRAT_HOUBU else ""
    lines.append(f"")
    lines.append(f"    {_MARK[_ascii()]} 方案{index} · {plan.strategy_cn}{star}   车次 {plan.train_code}")
    lines.append(
        f"       上车 {plan.board_name} {plan.board_time}"
        f"   →   下车 {plan.alight_name} {plan.alight_time}"
    )

    if plan.strategy == STRAT_HOUBU:
        seats = "、".join(plan.houbu_seats) if plan.houbu_seats else "（未提供席别信息）"
        lines.append(f"       可候补席别：{seats}")
    else:
        for seg in plan.buy_segments:
            seat = seg.seat
            # 席别名本身可能就叫「无座」，别重复标注
            standing = "（无座）" if seat.is_standing and "无座" not in seat.name else ""
            lines.append(
                f"       购买：{seg.from_name} → {seg.to_name}   "
                f"{seat.name}{standing} ¥{seat.price} {seat.count_text}"
                f"   （{seg.from_time} 发 → {seg.to_time} 到）"
            )
        if plan.total_price is not None and len(plan.buy_segments) > 1:
            lines.append(f"       两段合计：¥{plan.total_price}")
        if plan.onboard_segment:
            stand_note = f"，需站 {plan.uncovered_stops} 站" if plan.uncovered_stops else ""
            lines.append(f"       上车后补票：{plan.onboard_segment}{stand_note}")
        if plan.wasted_segment:
            lines.append(f"       票面未乘区间（作废）：{plan.wasted_segment}")
        if plan.overrun_segment:
            lines.append(f"       票面未乘区间（作废）：{plan.overrun_segment}")

    if show_risks:
        for risk in plan.risks:
            lines.append(f"       ⚠ {risk}")
    if plan.evidence:
        lines.append(f"       依据：{plan.evidence}")
    return lines


# ----------------------------------------------------------------------
def render_route(stops: list[RouteStop]) -> str:
    """把经停站渲染成一行紧凑的文本。"""
    parts = []
    for stop in stops:
        if stop.is_first:
            t = f"{stop.start_time}发"
        elif stop.is_last:
            t = f"{stop.arrive_time}到"
        else:
            t = f"{stop.arrive_time}/{stop.start_time}"
        parts.append(f"{stop.name}{stop.station_no:02d}({t})")
    return " → ".join(parts)


def render_stats(
    budget_snapshot: dict[str, Any],
    ticket_snapshot: dict[str, Any],
    probes_run: list[str],
    probes_dropped: list[str],
) -> str:
    """渲染请求统计。用户应该能看清程序到底做了什么。"""
    kinds = budget_snapshot.get("by_kind") or {}
    kind_cn = {
        "init": "初始化",
        "stations": "车站表",
        "leftTicket": "余票查询",
        "route": "经停站",
    }
    detail = " + ".join(f"{kind_cn.get(k, k)} {v}" for k, v in kinds.items())
    lines = [
        "",
        rule(),
        f"请求统计：实际发出 {budget_snapshot['used']} 次"
        + (f"（{detail}）" if detail else ""),
        f"          预算上限 {budget_snapshot['max_requests']} 次"
        f"    缓存命中 {budget_snapshot['cached_hits']} 次",
    ]
    if probes_run:
        lines.append(f"          已探测区间 {len(probes_run)} 个：{'、'.join(probes_run)}")
    if probes_dropped:
        lines.append(f"          ⚠ 因预算跳过区间 {len(probes_dropped)} 个：{'、'.join(probes_dropped)}")
    if ticket_snapshot.get("parse_failures"):
        lines.append(
            f"          ⚠ 有 {ticket_snapshot['parse_failures']} 行余票数据解析失败"
            f"，请运行 python tools/schema_probe.py 排查"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
def emit_json(payload: dict[str, Any]) -> None:
    """把 JSON 打到 stdout。日志走 stderr，所以这里可以安全地管道化。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")


def render_station_resolution(label: str, description: str) -> str:
    return f"「{label}」{description}"


def seat_names_of(codes: str) -> str:
    """把席别码串翻成中文名，如 'OM' -> 二等座、一等座。"""
    names: list[str] = []
    for ch in codes:
        name = C.SEAT_CODE_MAP.get(ch, (ch, ""))[0]
        if name not in names:
            names.append(name)
    return "、".join(names)
