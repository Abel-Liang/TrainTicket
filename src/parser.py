# -*- coding: utf-8 -*-
"""
余票行解析 —— 本项目最需要小心的一块。

网上流传的那张固定下标表已经失效（详见 config.py 顶部说明）。这里采用
「头尾双锚定」+「自校验」两条腿走路：

  头尾双锚定：
    头部字段（0~19）按下标取；尾部字段一律用 len(行) - N 取。
    因为字段被省略时，绝对下标和行长会同时减 1，"倒数第 N 个"恒定不变。

  自校验（最关键的一条）：
    尾部有一个 seat_types 字段，它正好等于价格位图里各席别码的顺序拼接。
    两者不符 => 说明我们的锚点算错了 => **立刻报错，绝不猜测**。
    这是防止"静默输出错误票价"的最后一道闸门。

价格位图（每 10 个字符描述一个席别）：
    g[0]    席别码
    g[1:6]  票价 × 10（除以 10 得到元）
    g[6:10] 余票编码：>= 3000 表示该席别实为「无座」，余票数 = 值 - 3000

实测校验过（见 tests/fixtures）：
    G1786 -> 商务座 ¥167 / 一等座 ¥83 / 二等座 ¥49
    K528  -> 硬卧 ¥69.5 / 软卧 ¥104.5 / 硬座 ¥23.5 / 无座 ¥23.5
价格随里程单调递增，且无座票价恒等于二等座票价，符合 12306 定价规则。
#20260928Abel
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import config as C
from . import logs

# 价格位图的特征：若干个"1 位席别码 + 9 位数字"拼起来
_PRICE_BLOB_RE = re.compile(r"^(?:[0-9A-Z][0-9]{9})+$")
_PUBLIC_CODE_RE = re.compile(C.PUBLIC_CODE_RE)
_TIME_RE = re.compile(C.TIME_RE)

# 2/3 字段顺序若反转，只提示一次就够了，别刷屏
_swap_warned = False


class SchemaError(Exception):
    """
    字段布局与预期不符。

    抛出这个异常是**好事**——它意味着我们发现了问题并且拒绝输出错误数据，
    而不是安静地把错误价格打印给用户。
    """


@dataclass
class SeatOffer:
    """某个车次某个区段上，一种席别的报价与余票。"""

    code: str            # 原始席别码（无座会被归一成 'W'）
    name: str            # 中文名，如「二等座」
    short: str           # 短名，如 'ze'，用于去重与 JSON 键
    price: float         # 票价（元）
    count: int           # 余票数
    count_text: str      # 「无票」/「剩余12张」/「有票」
    is_standing: bool    # 是否无座

    @property
    def has_ticket(self) -> bool:
        return self.count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "short": self.short,
            "price": self.price,
            "count": self.count,
            "count_text": self.count_text,
            "is_standing": self.is_standing,
        }


@dataclass
class TicketRow:
    """余票接口返回的一行，代表「某车次在某站对上的发售情况」。"""

    train_code: str           # 车次号，如 D2928
    internal_train_no: str    # 内部车次号，如 5q000D292810
    from_tele: str            # 上车站电报码
    to_tele: str              # 下车站电报码
    start_tele: str           # 该车始发站电报码
    end_tele: str             # 该车终到站电报码
    from_station_no: int      # 该车自己的站序（01 起）
    to_station_no: int
    start_time: str
    arrive_time: str
    lishi: str                # 历时 hh:mm
    can_web_buy: str          # Y/N
    sale_time: str
    houbu_flag: str           # '1' 支持候补 / '0' 不支持
    houbu_seats: str          # 可候补的席别码串，如 'OM'
    tags: list[str] = field(default_factory=list)
    seats: list[SeatOffer] = field(default_factory=list)
    raw: str = ""

    # ---------------- 便捷属性 ----------------
    @property
    def has_ticket(self) -> bool:
        """本行是否有任何一个席别有票。"""
        return any(s.has_ticket for s in self.seats)

    @property
    def can_houbu(self) -> bool:
        """
        该区段是否可以候补。

        【判据是"席别串非空"，不是 flag == '1'】
        实测（2026-10-02 上海→无锡 311 行）：
          · flag='0'  的 84 趟**全部有票**，且席别串全为空——有票的车不需要候补。
          · flag='1' 的 227 趟里，只有 23 趟带着席别串，而这 23 趟正是售罄车次
            （21/23 是 canWebBuy=N 且无票）。
          · 其余 204 趟 flag='1' 但席别串为空，开不出"能候补哪个席别"。

        只有席别串才告诉用户**该候补哪个座位**。只看 flag 会输出一堆
        "可候补席别：未知"的空方案，对用户毫无用处。
        """
        return self.houbu_flag == "1" and bool(self.houbu_seats)

    @property
    def not_on_sale_yet(self) -> bool:
        """
        是否尚未到起售时间（区别于"已售完"）。

        实测 canWebBuy 会出现 'IS_TIME_NOT_BUY'。这两种情况对用户的含义完全不同：
        一个是"再等等就能买"，一个是"得想别的办法"。不能混为一谈。
        """
        return self.can_web_buy == "IS_TIME_NOT_BUY"

    @property
    def houbu_seat_names(self) -> list[str]:
        """可候补席别的中文名列表。"""
        out = []
        for ch in self.houbu_seats:
            name = C.SEAT_CODE_MAP.get(ch, (ch, ""))[0]
            if name not in out:
                out.append(name)
        return out

    def cheapest_available(self) -> SeatOffer | None:
        """有票席别里最便宜的那个。"""
        avail = [s for s in self.seats if s.has_ticket]
        return min(avail, key=lambda s: s.price) if avail else None


# ----------------------------------------------------------------------
# 位图定位与解码
# ----------------------------------------------------------------------
def _seat_types_field(fields: list[str]) -> str:
    """取 seat_types 字段（尾部锚点），越界返回空串。"""
    idx = len(fields) - C.TAIL_SEAT_TYPES
    return fields[idx] if 0 <= idx < len(fields) else ""


def _looks_like_blob(value: str) -> bool:
    """判断一个字段像不像价格位图。

    注意下限是 **1 组**（10 字符）而不是 2 组：只有单一席别的车次（例如只有
    软座的 K5822）位图就只有 10 个字符。早先写死 20 会把这些车次误判成解析失败。
    """
    return bool(
        value
        and len(value) >= C.BLOB_GROUP_LEN
        and len(value) % C.BLOB_GROUP_LEN == 0
        and _PRICE_BLOB_RE.match(value)
    )


def locate_price_blob(fields: list[str]) -> tuple[int, str]:
    """
    找到价格位图所在的字段。

    首选按尾部锚点 len-19；如果那里不像位图，就整行扫一遍。
    扫描时优先选**能通过 seat_types 自校验**的候选——这样自校验不只是事后检查，
    也参与了定位，能有效排除"某个恰好形似位图的字段"这种误命中。
    """
    declared = _seat_types_field(fields)

    idx = len(fields) - C.TAIL_PRICE_BLOB
    if 0 <= idx < len(fields) and _looks_like_blob(fields[idx]):
        return idx, fields[idx]

    fallback: tuple[int, str] | None = None
    for i, value in enumerate(fields):
        if not _looks_like_blob(value):
            continue
        derived = "".join(value[j] for j in range(0, len(value), C.BLOB_GROUP_LEN))
        if derived == declared:
            logs.detail(f"价格位图不在预期的 len-{C.TAIL_PRICE_BLOB}，实际在下标 {i}")
            return i, value
        if fallback is None:
            fallback = (i, value)

    if fallback is not None:
        # 形似位图但没通过自校验，交给后面 _validate 明确报错，不在这里放过
        return fallback

    raise SchemaError(
        f"在长度为 {len(fields)} 的行里找不到价格位图。"
        f"12306 可能改版了，请运行 python tools/schema_probe.py 排查。"
    )


def decode_price_blob(blob: str) -> list[SeatOffer]:
    """把价格位图解码成席别列表。"""
    if len(blob) % C.BLOB_GROUP_LEN != 0:
        raise SchemaError(f"价格位图长度 {len(blob)} 不是 {C.BLOB_GROUP_LEN} 的整数倍")

    offers: list[SeatOffer] = []
    for i in range(0, len(blob), C.BLOB_GROUP_LEN):
        group = blob[i : i + C.BLOB_GROUP_LEN]
        code = group[0]
        try:
            price = int(group[C.BLOB_PRICE_START : C.BLOB_PRICE_END]) / 10
            raw_count = int(group[C.BLOB_COUNT_START : C.BLOB_COUNT_END])
        except ValueError as exc:
            raise SchemaError(f"价格位图分段解析失败：{group!r}") from exc

        # >= 3000 表示这个席别实际卖的是无座（12306 的编码习惯，实测有效）
        is_standing = raw_count >= C.STANDING_OFFSET
        count = raw_count - C.STANDING_OFFSET if is_standing else raw_count
        if is_standing:
            code = "W"

        name, short = C.SEAT_CODE_MAP.get(code, (f"未知席别({code})", "qt"))
        offers.append(
            SeatOffer(
                code=code,
                name=name,
                short=short,
                price=price,
                count=count,
                count_text=format_count(count),
                is_standing=is_standing,
            )
        )
    return offers


def format_count(count: int) -> str:
    """
    余票数的展示文案。

    12306 页面上余票超过 20 张就显示「有」而不是具体数字，这里保持一致——
    既符合用户习惯，也避免把"21 张"这种无意义精度当成真实库存。
    """
    if count <= 0:
        return "无票"
    if count >= C.COUNT_HAVE_CAP:
        return "有票"
    return f"剩余{count}张"


def parse_dw_flags(raw_flag: str) -> list[str]:
    """解析车次标签串（复兴号/智能动车组等）。纯展示，失败不影响主流程。"""
    if not raw_flag:
        return []
    parts = raw_flag.split("#")
    tags: list[str] = []
    for pos, (expect, label) in C.DW_FLAG_TAGS.items():
        if len(parts) > pos and parts[pos] == expect:
            tags.append(label)
    # 静音车厢 / 温馨动卧：第 3 段以 Q / R 开头
    if len(parts) > 2 and parts[2]:
        head = parts[2][:1]
        if head == "Q":
            tags.append("静音车厢")
        elif head == "R":
            tags.append("温馨动卧")
    return tags


# ----------------------------------------------------------------------
# 行长校验与主解析
# ----------------------------------------------------------------------
def _validate(fields: list[str], blob: str, price_idx: int) -> None:
    """
    解析后的自检。任何一条不满足都直接抛错。

    宁可报错让用户重跑，也不能把错误的价格打印出来——用户是拿着这个结果去买票的。
    """
    global _swap_warned

    if len(fields) < C.ROW_MIN_LEN:
        raise SchemaError(f"行长 {len(fields)} 小于下限 {C.ROW_MIN_LEN}")

    # 2/3 字段是否反转（网上有些资料把这两个写反了，这里自动识别）。
    # 判别办法：内部号固定 12 位，车次号短得多。纯数字车次号（如 1462）
    # 和纯数字内部号（如 550000146209）都能被正则匹配，所以必须比长度。
    def _is_internal_no(value: str) -> bool:
        return len(value) == C.INTERNAL_NO_LEN and value.isalnum()

    def _is_public_code(value: str) -> bool:
        return 1 <= len(value) <= 8 and not _is_internal_no(value) and bool(
            _PUBLIC_CODE_RE.match(value)
        )

    if not _is_public_code(fields[C.IDX_PUBLIC_TRAIN_CODE]) and _is_public_code(
        fields[C.IDX_INTERNAL_TRAIN_NO]
    ):
        if not _swap_warned:
            logs.warn("检测到车次号字段顺序与预期相反，已自动纠正")
            _swap_warned = True
        fields[C.IDX_INTERNAL_TRAIN_NO], fields[C.IDX_PUBLIC_TRAIN_CODE] = (
            fields[C.IDX_PUBLIC_TRAIN_CODE],
            fields[C.IDX_INTERNAL_TRAIN_NO],
        )

    if not _is_public_code(fields[C.IDX_PUBLIC_TRAIN_CODE]):
        raise SchemaError(
            f"无法识别车次编码：字段2={fields[C.IDX_INTERNAL_TRAIN_NO]!r} "
            f"字段3={fields[C.IDX_PUBLIC_TRAIN_CODE]!r}"
        )

    start_time = fields[C.IDX_START_TIME]
    if not (_TIME_RE.match(start_time) or start_time == C.NO_TIME):
        raise SchemaError(f"发车时间格式异常：{start_time!r}")

    if len(blob) % C.BLOB_GROUP_LEN != 0:
        raise SchemaError(f"价格位图长度 {len(blob)} 非法")

    # ---- 核心自校验：seat_types 必须等于位图里各席别码的拼接 ----
    if len(fields) - C.TAIL_SEAT_TYPES < 0:
        raise SchemaError(f"seat_types 锚点越界（行长 {len(fields)}）")
    declared = _seat_types_field(fields)
    derived = "".join(blob[i] for i in range(0, len(blob), C.BLOB_GROUP_LEN))
    if declared != derived:
        raise SchemaError(
            f"席别自校验失败：seat_types={declared!r} 与价格位图推出的 {derived!r} 不一致。"
            f"这说明字段锚点已失效，12306 很可能改版了。"
        )

    try:
        from_no = int(fields[C.IDX_FROM_STATION_NO])
        to_no = int(fields[C.IDX_TO_STATION_NO])
    except ValueError as exc:
        raise SchemaError(
            f"站序不是数字：{fields[C.IDX_FROM_STATION_NO]!r} / {fields[C.IDX_TO_STATION_NO]!r}"
        ) from exc
    if from_no >= to_no:
        raise SchemaError(f"站序异常：上车站序 {from_no} 不小于下车站序 {to_no}")


def parse_row(
    raw: str,
    station_map: dict[str, str] | None = None,
    strict: bool = True,
) -> TicketRow:
    """
    解析一行余票数据。

    参数：
        raw          —— 原始行（"|" 分隔的字符串）
        station_map  —— 电报码 -> 中文名（接口返回的 map，仅用于展示）
        strict       —— True 时任何校验失败都抛 SchemaError；
                        False 时返回 None 交给上层统计（用于批量场景）

    注意：函数的返回值类型标注为 TicketRow，但 strict=False 时可能返回 None。
    """
    fields = raw.split("|")
    station_map = station_map or {}
    del station_map  # 目前仅保留参数位，中文名在展示层用车站表解析

    try:
        price_idx, blob = locate_price_blob(fields)
        _validate(fields, blob, price_idx)
        seats = decode_price_blob(blob)

        row = TicketRow(
            train_code=fields[C.IDX_PUBLIC_TRAIN_CODE],
            internal_train_no=fields[C.IDX_INTERNAL_TRAIN_NO],
            from_tele=fields[C.IDX_FROM_STATION_TELECODE],
            to_tele=fields[C.IDX_TO_STATION_TELECODE],
            start_tele=fields[C.IDX_START_STATION_TELECODE],
            end_tele=fields[C.IDX_END_STATION_TELECODE],
            from_station_no=int(fields[C.IDX_FROM_STATION_NO]),
            to_station_no=int(fields[C.IDX_TO_STATION_NO]),
            start_time=fields[C.IDX_START_TIME],
            arrive_time=fields[C.IDX_ARRIVE_TIME],
            lishi=fields[C.IDX_LISHI],
            can_web_buy=fields[C.IDX_CAN_WEB_BUY],
            sale_time=_tail(fields, C.TAIL_SALE_TIME),
            houbu_flag=_tail(fields, C.TAIL_HOUBU_FLAG),
            houbu_seats=_tail(fields, C.TAIL_HOUBU_SEAT_LIMIT),
            tags=parse_dw_flags(_tail(fields, C.TAIL_DW_FLAG)),
            seats=seats,
            raw=raw,
        )
        return row
    except SchemaError:
        if strict:
            raise
        return None  # type: ignore[return-value]


def _tail(fields: list[str], offset: int) -> str:
    """按尾部锚点取字段，越界返回空串（不抛异常，这些字段都是可选的）。"""
    idx = len(fields) - offset
    if 0 <= idx < len(fields):
        return fields[idx]
    return ""


def parse_rows(
    raw_rows: list[str],
    station_map: dict[str, str] | None = None,
) -> tuple[list[TicketRow], list[str]]:
    """
    批量解析。返回 (成功列表, 失败行的摘要)。

    单行失败**不中断整批**——12306 偶尔会混进个别格式异常的行，
    为一行放弃整批结果对用户没有好处。但失败数是会汇报的，不静默吞掉。
    """
    ok: list[TicketRow] = []
    failed: list[str] = []
    for raw in raw_rows:
        try:
            row = parse_row(raw, station_map, strict=True)
            ok.append(row)
        except SchemaError as exc:
            head = raw[:40].replace("\n", "")
            failed.append(f"{head}... ({exc})")
    return ok, failed
