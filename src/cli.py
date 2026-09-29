# -*- coding: utf-8 -*-
"""
命令行入口 —— 参数解析 + 主流程编排。

流程：
    解析日期/车站 -> 查余票 -> 过滤（时间窗/车型）-> 对无票车次做替代方案分析
    -> 按格式输出

【时间窗口径】窗口始终作用于**用户在出发站实际开始乘车的时刻**，也就是该行在
上车站的发车时间。不用票面更早的始发站时刻，也不用途经站的时刻——否则会把用户
真正想坐的车次过滤掉。
#20260928Abel
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date as _date, datetime, timedelta
from typing import Any

from . import config as C
from . import logs
from . import output as out
from .analysis import AnalysisResult, Analyzer, Options
from .client import Budget, HttpSession, RiskControlError, TrainTicketError
from .parser import TicketRow
from .route import TrainRouteService
from .stations import Resolution, StationTable
from .ticket import LeftTicketService


class PresaleError(Exception):
    """日期超出预售期或已过去。这种情况不该浪费请求。"""


# ----------------------------------------------------------------------
def resolve_date(text: str, force: bool = False) -> str:
    """
    把用户输入的日期解析成 YYYY-MM-DD，并检查预售期。

    支持：2026-10-02 / 20261002 / today / tomorrow / +3
    默认是明天（今天大多车次已经发车，查了也没用）。
    """
    raw = (text or "").strip().lower()
    today = _date.today()

    if raw in ("", "tomorrow", "明天"):
        target = today + timedelta(days=1)
    elif raw in ("today", "今天"):
        target = today
    elif raw.startswith("+"):
        try:
            target = today + timedelta(days=int(raw[1:]))
        except ValueError as exc:
            raise PresaleError(f"无法识别的日期写法：{text}") from exc
    else:
        cleaned = raw.replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%m-%d"):
            try:
                parsed = datetime.strptime(cleaned, fmt)
                target = parsed.date() if fmt != "%m-%d" else parsed.replace(year=today.year).date()
                break
            except ValueError:
                continue
        else:
            raise PresaleError(
                f"无法识别的日期：{text}（支持 2026-10-02 / 20261002 / today / tomorrow / +3）"
            )

    if force:
        return target.isoformat()

    delta = (target - today).days
    if delta < 0:
        raise PresaleError(f"日期 {target} 已经过去了（今天是 {today}）")
    if delta > C.PRESALE_DAYS:
        raise PresaleError(
            f"日期 {target} 距今天 {delta} 天，超出 12306 预售期（{C.PRESALE_DAYS} 天）。\n"
            f"       当前可查范围：{today} ~ {today + timedelta(days=C.PRESALE_DAYS)}\n"
            f"       建议：不要指定 --date 使用默认值（明天 {today + timedelta(days=1)}），"
            f"或改用 --force-date 跳过此检查。"
        )
    if delta == 0:
        logs.warn("查询的是今天，大部分车次可能已经发车，结果会很少")
    return target.isoformat()


def parse_window(
    time_text: str | None = None,
    start_text: str | None = None,
    end_text: str | None = None,
) -> tuple[str, str, str]:
    """
    解析时间窗，返回 (起始, 结束, 展示文本)。

    两种写法都支持：
        time_text="06:00-12:00"
        start_text="06:00", end_text="12:00"

    拆成独立参数是为了让图形界面也能直接调用，不必伪造一个 argparse 对象。
    """
    start, end = "00:00", "24:00"
    if time_text:
        text = time_text.replace("~", "-").replace("—", "-").strip()
        if "-" in text:
            parts = [p.strip() for p in text.split("-", 1)]
        elif "至" in text:
            parts = [p.strip() for p in text.split("至", 1)]
        else:
            raise PresaleError(f"时间窗格式应为 06:00-12:00，收到：{time_text}")
        start, end = (parts[0] or start), (parts[1] or end)
    if start_text:
        start = start_text
    if end_text:
        end = end_text

    def normalize(value: str, fallback: str) -> str:
        value = value.strip()
        if not value:
            return fallback
        if re.fullmatch(r"\d{1,2}", value):
            value = f"{int(value):02d}:00"
        if not re.fullmatch(r"\d{1,2}:\d{2}", value):
            raise PresaleError(f"时间格式应为 HH:MM，收到：{value}")
        return value

    start = normalize(start, "00:00")
    end = normalize(end, "24:00")
    if start >= end:
        raise PresaleError(f"时间窗起点 {start} 不早于终点 {end}")
    if start == "00:00" and end in ("24:00", "23:59"):
        return start, end, ""
    return start, end, f"{start}-{end}"


def in_window(row: TicketRow, start: str, end: str) -> bool:
    """车次是否落在时间窗内（按在出发站的发车时刻）。"""
    if not re.fullmatch(r"\d{2}:\d{2}", row.start_time or ""):
        return True
    return start <= row.start_time < end


def parse_train_types(text: str | None) -> tuple[set[str], set[str]]:
    """
    解析车型过滤。返回 (保留集合, 排除集合)。

        "GD"   -> 只保留 G 和 D 开头
        "-K"   -> 排除 K 开头
        空     -> 不过滤
    """
    if not text:
        return set(), set()
    text = text.strip().upper()
    if text.startswith("-"):
        return set(), set(text[1:])
    return set(text), set()


def matches_type(code: str, keep: set[str], drop: set[str]) -> bool:
    prefix = code[:1].upper()
    if drop and prefix in drop:
        return False
    if keep and prefix not in keep:
        return False
    return True


# ----------------------------------------------------------------------
def resolve_inputs(
    table: StationTable, text: str, exact: bool
) -> tuple[Resolution, list[str]]:
    """
    解析出发地/到达地，支持用 "|" 分隔多个。

    返回 (合并后的解析结果, 允许的电报码集合)。
    """
    pieces = [p.strip() for p in re.split(r"[|｜]", text) if p.strip()]
    if not pieces:
        raise PresaleError("出发地和到达地不能为空")

    merged = Resolution("station", [], source_text=text)
    allowed: list[str] = []
    kinds: set[str] = set()

    for piece in pieces:
        res = table.resolve(piece, exact_station=exact)
        if res.kind == "not_found":
            raise PresaleError(res.message)
        if res.kind == "ambiguous":
            names = "、".join(s.name for s in res.stations)
            raise PresaleError(f"{res.message}：{names}")
        merged.stations.extend(res.stations)
        kinds.add(res.kind)

    # 去重，保持顺序
    seen: set[str] = set()
    unique = []
    for st in merged.stations:
        if st.telecode not in seen:
            seen.add(st.telecode)
            unique.append(st)
            allowed.append(st.telecode)
    merged.stations = unique
    merged.kind = "city" if "city" in kinds else "station"
    return merged, allowed


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="TrainTicket",
        description=(
            "12306 车票查询 + 无票替代方案分析。\n"
            "无票时会分析该车的经停站，给出「买短乘长（上车补票）」、\n"
            "「买长乘短」、「同车分段」和「候补」四类方案。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py -d 2026-10-02 -f 上海虹桥 -t 无锡 --time 06:00-12:00 --analyze\n"
            "  python main.py -f 上海 -t 无锡 -d tomorrow\n"
            "  python main.py -f 上海虹桥 -t 无锡 -d 2026-10-02 --format json --max-requests 20\n"
            "\n"
            "说明：\n"
            "  · 时间窗按「在出发站实际乘车的时刻」过滤，不是票面始发站时刻。\n"
            "  · 输入城市名会展开为该市全部车站；指定具体站名则只看该站。\n"
            "  · 候补方案只做提示——提交候补需要登录 12306，本程序只读、不代下单。\n"
            "  · 补票费用与手续费以 12306 官方规定和现场为准，程序不做估算。\n"
        ),
    )
    parser.add_argument("-d", "--date", default="tomorrow",
                        help="乘车日期，支持 2026-10-02 / 20261002 / today / tomorrow / +3（默认明天）")
    parser.add_argument("--force-date", action="store_true",
                        help="跳过预售期检查（明知超出仍要尝试时使用）")
    parser.add_argument("-f", "--from", dest="from_station", required=True,
                        help="出发地：城市名或车站名，多个用 | 分隔")
    parser.add_argument("-t", "--to", dest="to_station", required=True,
                        help="到达地：城市名或车站名，多个用 | 分隔")
    parser.add_argument("--time", default=None, help="时间窗，如 06:00-12:00")
    parser.add_argument("--start", default=None, help="时间窗起点 HH:MM")
    parser.add_argument("--end", default=None, help="时间窗终点 HH:MM")
    parser.add_argument("--types", default=None,
                        help="车型过滤：GD 只保留 G/D 开头；-K 排除 K 开头")
    parser.add_argument("--exact-station", action="store_true",
                        help="输入按车站解析，不做城市展开")
    parser.add_argument("--trains", default=None,
                        help="只看指定车次，逗号分隔，如 D2928,G7032")

    parser.add_argument("-a", "--analyze", dest="analyze", action="store_true", default=None,
                        help="强制开启替代方案分析")
    parser.add_argument("--no-analyze", dest="analyze", action="store_false",
                        help="关闭替代方案分析")
    parser.add_argument("--no-buy-short", dest="allow_buy_short", action="store_false", default=True,
                        help="不分析「买短乘长」")
    parser.add_argument("--no-buy-long", dest="allow_buy_long", action="store_false", default=True,
                        help="不分析「买长乘短」")
    parser.add_argument("--no-split", dest="allow_split", action="store_false", default=True,
                        help="不分析「同车分段」")
    parser.add_argument("--no-houbu", dest="allow_houbu", action="store_false", default=True,
                        help="不提示「候补」")

    parser.add_argument("--max-requests", type=int, default=C.DEFAULT_MAX_REQUESTS,
                        help=f"请求次数上限（默认 {C.DEFAULT_MAX_REQUESTS}）")
    parser.add_argument("--max-analyze-trains", type=int, default=C.DEFAULT_MAX_ANALYZE_TRAINS,
                        help=f"最多分析几趟无票车次（默认 {C.DEFAULT_MAX_ANALYZE_TRAINS}）")
    parser.add_argument("--max-candidate-stops", type=int, default=C.DEFAULT_MAX_CANDIDATE_STOPS,
                        help=f"每趟车最多探测几个中途落客站（默认 {C.DEFAULT_MAX_CANDIDATE_STOPS}）")
    parser.add_argument("--max-back-stops", type=int, default=C.DEFAULT_MAX_BACK_STOPS,
                        help=f"买长乘短最多向上游看几站（默认 {C.DEFAULT_MAX_BACK_STOPS}）")
    parser.add_argument("--min-interval", type=float, default=C.MIN_REQUEST_INTERVAL,
                        help=f"请求最小间隔秒数（默认 {C.MIN_REQUEST_INTERVAL}，硬下限 "
                             f"{C.MIN_INTERVAL_HARD_FLOOR}）")

    parser.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    parser.add_argument("--refresh-stations", action="store_true", help="强制刷新车站表缓存")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求计划，不发请求")
    parser.add_argument("--ascii", action="store_true", help="用纯 ASCII 边框（终端渲染异常时使用）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    return parser


# ----------------------------------------------------------------------
def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.setup_console(ascii_only=args.ascii)
    logs.set_verbose(args.verbose)

    try:
        return _run(args)
    except PresaleError as exc:
        logs.error(str(exc))
        return 2
    except RiskControlError as exc:
        logs.error(str(exc))
        return 3
    except TrainTicketError as exc:
        logs.error(str(exc))
        return 1
    except KeyboardInterrupt:
        logs.error("用户中断")
        return 130


def _run(args: argparse.Namespace) -> int:
    # ---- 日期与时间窗 ----
    date = resolve_date(args.date, force=args.force_date)
    win_start, win_end, win_label = parse_window(args.time, args.start, args.end)
    keep_types, drop_types = parse_train_types(args.types)

    if args.dry_run:
        return _dry_run(args, date, win_label)

    # ---- 初始化各服务 ----
    budget = Budget(args.max_requests)
    session = HttpSession(budget=budget, min_interval=args.min_interval)
    stations = StationTable(session)
    stations.load(refresh=args.refresh_stations)
    tickets = LeftTicketService(session, stations)
    routes = TrainRouteService(session, stations)

    # ---- 解析出发地 / 到达地 ----
    from_res, from_allowed = resolve_inputs(stations, args.from_station, args.exact_station)
    to_res, to_allowed = resolve_inputs(stations, args.to_station, args.exact_station)
    logs.info(from_res.describe())
    logs.info(to_res.describe())

    # ---- 查余票 ----
    # 只发一次城市对查询即可覆盖所有市内站组合（接口按城市解析，实测确认）
    seed_from = from_allowed[0]
    seed_to = to_allowed[0]
    logs.info(f"查询 {date}  {from_res.source_text} → {to_res.source_text} …")
    try:
        tickets.query_raw(date, seed_from, seed_to)
    except TrainTicketError as exc:
        logs.error(f"余票查询失败：{exc}")
        return 1

    from_set, to_set = set(from_allowed), set(to_allowed)
    rows = tickets.rows_for(date, from_set, to_set)

    # 用户显式指定车次时先按车次过滤
    wanted = {c.strip().upper() for c in (args.trains or "").split(",") if c.strip()}
    if wanted:
        rows = [r for r in rows if r.train_code.upper() in wanted]

    rows = [r for r in rows if in_window(r, win_start, win_end)
            and matches_type(r.train_code, keep_types, drop_types)]
    # 同一车次可能因同城多站出现多行，按 (车次, 上车站) 去重
    dedup: dict[tuple[str, str], TicketRow] = {}
    for row in rows:
        key = (row.train_code, row.from_tele)
        if key not in dedup:
            dedup[key] = row
    rows = sorted(dedup.values(), key=lambda r: (r.start_time, r.train_code))

    station_names = dict(tickets.station_map)
    for code in set(from_allowed) | set(to_allowed):
        station_names[code] = stations.name_of(code, station_names)

    # ---- 输出查询结果 ----
    from_label = f"{from_res.source_text}({len(from_allowed)}站)" if from_res.kind == "city" else from_res.source_text
    to_label = f"{to_res.source_text}({len(to_allowed)}站)" if to_res.kind == "city" else to_res.source_text

    if args.format == "text":
        print(out.render_query_table(rows, date, from_label, to_label, win_label, station_names))

    # ---- 分析 ----
    no_ticket_count = sum(1 for r in rows if not r.has_ticket)
    do_analyze = args.analyze if args.analyze is not None else no_ticket_count > 0

    analysis = AnalysisResult()
    if do_analyze and rows:
        options = Options(
            max_analyze_trains=args.max_analyze_trains,
            max_candidate_stops=args.max_candidate_stops,
            max_back_stops=args.max_back_stops,
            allow_buy_short=args.allow_buy_short,
            allow_buy_long=args.allow_buy_long,
            allow_split=args.allow_split,
            allow_houbu=args.allow_houbu,
        )
        analyzer = Analyzer(session, stations, tickets, routes, options)
        analysis = analyzer.analyze(date, seed_from, seed_to, rows)

        if args.format == "text" and analysis.analyzed_trains:
            print(out.render_analysis(analysis, station_names))

    # ---- 输出 ----
    if args.format == "json":
        payload: dict[str, Any] = {
            "meta": {
                "date": date,
                "from_input": from_res.source_text,
                "to_input": to_res.source_text,
                "from_resolved": [s.to_dict() for s in from_res.stations],
                "to_resolved": [s.to_dict() for s in to_res.stations],
                "time_window": win_label or None,
                "train_types": args.types,
                "analyzed": bool(analysis.analyzed_trains),
            },
            "trains": [_row_to_dict(r, station_names) for r in rows],
            "analysis": analysis.to_dict(),
            "stats": {
                "budget": budget.snapshot(),
                "cache": tickets.snapshot(),
            },
        }
        out.emit_json(payload)
    else:
        print(out.render_stats(budget.snapshot(), tickets.snapshot(),
                               analysis.probes_run, analysis.probes_dropped))

    return 0


def _row_to_dict(row: TicketRow, station_names: dict[str, str]) -> dict[str, Any]:
    return {
        "train_code": row.train_code,
        "internal_train_no": row.internal_train_no,
        "from_station": station_names.get(row.from_tele, row.from_tele),
        "from_telecode": row.from_tele,
        "to_station": station_names.get(row.to_tele, row.to_tele),
        "to_telecode": row.to_tele,
        "start_time": row.start_time,
        "arrive_time": row.arrive_time,
        "lishi": row.lishi,
        "from_station_no": row.from_station_no,
        "to_station_no": row.to_station_no,
        "can_web_buy": row.can_web_buy,
        "has_ticket": row.has_ticket,
        "can_houbu": row.can_houbu,
        "houbu_seats": row.houbu_seat_names,
        "tags": row.tags,
        "seats": [s.to_dict() for s in row.seats],
    }


def _dry_run(args: argparse.Namespace, date: str, win_label: str) -> int:
    """只打印请求计划，不碰网络。"""
    print("【dry-run】不会发出任何真实请求。本次运行的计划如下：")
    print(f"  日期        : {date}")
    print(f"  出发地      : {args.from_station}")
    print(f"  到达地      : {args.to_station}")
    print(f"  时间窗      : {win_label or '全天'}")
    print(f"  车站解析    : {'按车站（不展开城市）' if args.exact_station else '城市优先（会展开为全市车站）'}")
    print(f"  车型过滤    : {args.types or '不限'}")
    print(f"  请求预算    : {args.max_requests} 次")
    print(f"  分析开关    : {'开' if args.analyze is not False else '关'}")
    print(f"  启用策略    : 买短乘长={args.allow_buy_short}  买长乘短={args.allow_buy_long}"
          f"  同车分段={args.allow_split}  候补={args.allow_houbu}")
    print()
    print("预计请求序列：")
    print("  1. GET /otn/leftTicket/init                 （拿 cookie 与轮换后的查询路径）")
    print("  2. GET station_name.js                      （仅在车站表缓存过期时）")
    print("  3. GET /otn/leftTicket/<轮换路径>            （一次城市对查询覆盖全部站组合）")
    print("  4. GET /otn/queryTrainInfo/query            （每趟待分析车次一次，有缓存）")
    print("  5. GET /otn/leftTicket/<轮换路径>            （每个待探测区间一次，按站对去重）")
    print()
    print("提示：去掉 --dry-run 即可实际执行。")
    return 0
