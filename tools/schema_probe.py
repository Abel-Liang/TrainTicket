# -*- coding: utf-8 -*-
"""
字段结构探针 —— 排障用，独立于主流程。

用途：当程序报「席别自校验失败」或解析结果可疑时，跑这个脚本。它会拉一次
真实的余票响应，逐项打印并校验我们的字段锚点是否还对得上。

这是把"12306 改版了"变成五分钟修复的工具。任何一项校验失败就非零退出。

用法：
    .venv\\Scripts\\python.exe tools\\schema_probe.py
    .venv\\Scripts\\python.exe tools\\schema_probe.py --date 2026-10-02 --from AOH --to WXH
#20260928Abel
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config as C  # noqa: E402
from src import logs  # noqa: E402
from src.client import Budget, HttpSession, TrainTicketError  # noqa: E402
from src.parser import (  # noqa: E402
    SchemaError,
    decode_price_blob,
    locate_price_blob,
    parse_row,
)

_PRICE_BLOB_RE = re.compile(r"^(?:[0-9A-Z][0-9]{9})+$")

# 探针自身不设预算上限，但也不要打太多
PROBE_ROUNDS = 3


def probe_one(session: HttpSession, date: str, from_tele: str, to_tele: str) -> bool:
    """对一组站对做一次探测，返回是否全部校验通过。"""
    query = (
        f"leftTicketDTO.train_date={date}"
        f"&leftTicketDTO.from_station={from_tele}"
        f"&leftTicketDTO.to_station={to_tele}"
        f"&purpose_codes=ADULT"
    )
    print(f"\n{'=' * 78}")
    print(f"探测：{date}  {from_tele} -> {to_tele}")
    print("=" * 78)

    payload = session.get_left_ticket_json(query)
    data = payload.get("data") or {}
    rows = data.get("result") or []
    if not rows:
        print("  ⚠ 没有返回任何行（该站对无车次，或日期超出预售期）")
        return True

    print(f"  共 {len(rows)} 行")

    # ---- 1. 行长分布 ----
    lengths = collections.Counter(len(r.split("|")) for r in rows)
    print(f"\n  [1] 行长分布：{dict(sorted(lengths.items()))}")
    if len(lengths) > 1:
        print("      ↑ 长度不一致是正常的！正因为如此才必须用「尾部倒数」定位字段，")
        print("        写死绝对下标会静默算错。")
    else:
        print("      ↑ 本次长度一致，但不代表下次也一致——仍按尾部锚定解析。")

    # ---- 2. 价格位图锚点位置分布 ----
    def blob_index(row: str) -> int | None:
        fields = row.split("|")
        try:
            return locate_price_blob(fields)[0]
        except SchemaError:
            return None

    idx_dist = collections.Counter(
        i for i in (blob_index(r) for r in rows) if i is not None
    )
    print(f"\n  [2] 价格位图所在下标：{dict(sorted(idx_dist.items()))}")
    expected = {len(r.split('|')) - C.TAIL_PRICE_BLOB for r in rows}
    print(f"      预期值 len-{C.TAIL_PRICE_BLOB}：{sorted(expected)}")
    if set(idx_dist) != expected:
        print("      ❌ 不一致！锚点可能已失效")
        return False
    print("      ✅ 一致")

    # ---- 3. 逐行完整校验 ----
    print(f"\n  [3] 逐行解析（含 seat_types 自校验）")
    passed = failed = 0
    failures: list[str] = []
    for row in rows:
        try:
            parse_row(row, strict=True)
            passed += 1
        except SchemaError as exc:
            failed += 1
            if len(failures) < 5:
                failures.append(f"{row.split('|')[3] if len(row.split('|')) > 3 else '?'}: {exc}")
    print(f"      通过 {passed} / 失败 {failed}")
    for line in failures:
        print(f"      ❌ {line}")
    if failed:
        print("      ❌ 存在无法解析的行，锚点需要重新核对")
        return False
    print("      ✅ 全部通过")

    # ---- 4. 候补字段取值形态 ----
    print(f"\n  [4] 候补字段（尾部锚定）")
    flag_dist = collections.Counter()
    seat_dist = collections.Counter()
    for row in rows:
        fields = row.split("|")
        flag_dist[_tail(fields, C.TAIL_HOUBU_FLAG)] += 1
        seat_dist[_tail(fields, C.TAIL_HOUBU_SEAT_LIMIT)] += 1
    print(f"      houbu_train_flag  (len-{C.TAIL_HOUBU_FLAG})  : {dict(flag_dist.most_common(5))}")
    print(f"      houbu_seat_limit  (len-{C.TAIL_HOUBU_SEAT_LIMIT})  : {dict(seat_dist.most_common(5))}")
    binary_ok = set(flag_dist) <= {"0", "1", ""}
    seat_ok = all(
        all(ch in C.SEAT_CODE_MAP for ch in s) for s in seat_dist if s
    )
    print(f"      ✅ 开关是二值" if binary_ok else "      ⚠ 开关取值形态异常")
    print(f"      ✅ 席别串合法" if seat_ok else "      ⚠ 席别串含未知码")
    if not (binary_ok and seat_ok):
        return False

    # ---- 5. 抽样解码目视 ----
    print(f"\n  [5] 抽样解码（人工核对用）")
    for row in rows[:3]:
        fields = row.split("|")
        _, blob = locate_price_blob(fields)
        seats = decode_price_blob(blob)
        seat_str = " | ".join(f"{s.name} ¥{s.price} {s.count_text}" for s in seats)
        print(
            f"      {fields[C.IDX_PUBLIC_TRAIN_CODE]:<7}"
            f"{fields[C.IDX_FROM_STATION_TELECODE]}->{fields[C.IDX_TO_STATION_TELECODE]} "
            f"{fields[C.IDX_START_TIME]}  {seat_str}"
        )

    return True


def _tail(fields: list[str], offset: int) -> str:
    idx = len(fields) - offset
    return fields[idx] if 0 <= idx < len(fields) else ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="12306 余票字段结构探针（排障用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", default="2026-10-02", help="查询日期 YYYY-MM-DD")
    parser.add_argument("--from", dest="from_tele", default="AOH", help="出发站电报码")
    parser.add_argument("--to", dest="to_tele", default="WXH", help="到达站电报码")
    parser.add_argument(
        "--min-interval", type=float, default=C.MIN_REQUEST_INTERVAL, help="请求间隔（秒）"
    )
    args = parser.parse_args()

    logs.setup_console()

    session = HttpSession(
        budget=Budget(max_requests=PROBE_ROUNDS + 2), min_interval=args.min_interval
    )

    try:
        all_ok = probe_one(session, args.date, args.from_tele, args.to_tele)
    except TrainTicketError as exc:
        print(f"\n❌ 探测失败：{exc}")
        return 1

    print(f"\n{'=' * 78}")
    if all_ok:
        print("✅ 全部校验通过，解析器的字段锚点可信。")
        return 0
    print("❌ 存在校验失败项。请根据上面输出的实际取值，更新 src/config.py 里的尾部锚点常量。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
