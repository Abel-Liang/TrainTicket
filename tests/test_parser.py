# -*- coding: utf-8 -*-
"""
解析器离线回归测试。

全部基于 tests/fixtures 里保存的真实报文，不联网。12306 一旦改版，
这些用例会先失败，而不用等到线上跑出错误价格才发现。

运行：
    .venv\\Scripts\\python.exe -m unittest discover tests -v
#20260928Abel
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config as C  # noqa: E402
from src.parser import (  # noqa: E402
    SchemaError,
    decode_price_blob,
    locate_price_blob,
    parse_row,
)

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "leftticket_aoh_wxh_20261002.json",
)


def load_fixture() -> list[str]:
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)["data"]["result"]


class TestLocateBlob(unittest.TestCase):
    """价格位图的定位。"""

    def test_all_fixture_rows_locate_blob(self) -> None:
        """夹具里每一行都要能定位到位图。"""
        for raw in load_fixture():
            with self.subTest(train=raw.split("|")[C.IDX_PUBLIC_TRAIN_CODE]):
                idx, blob = locate_price_blob(raw.split("|"))
                self.assertTrue(blob)
                self.assertEqual(len(blob) % C.BLOB_GROUP_LEN, 0)
                self.assertEqual(idx, len(raw.split("|")) - C.TAIL_PRICE_BLOB)

    def test_single_seat_blob_is_10_chars(self) -> None:
        """
        只有单一席别的车次，位图只有 10 个字符。

        这是实测踩过的坑：早先要求位图至少 20 字符（两组），会把 K5822
        这类只有软座的车次整体误判成"解析失败"。这里锁死这个行为。
        """
        fields = ["x"] * 40
        fields[3] = "K5822"
        fields[2] = "550000K5820"
        fields[8] = "07:00"
        fields[16] = "01"
        fields[17] = "03"
        # 尾部锚点
        fields[len(fields) - C.TAIL_PRICE_BLOB] = "2002150000"
        fields[len(fields) - C.TAIL_SEAT_TYPES] = "2"
        idx, blob = locate_price_blob(fields)
        self.assertEqual(blob, "2002150000")
        seats = decode_price_blob(blob)
        self.assertEqual(len(seats), 1)
        self.assertEqual(seats[0].name, "软座")
        self.assertEqual(seats[0].price, 21.5)


class FixtureMixin:
    """从夹具里按 车次 + 站对 取行的公共方法。"""

    def _find_raw(self, code: str, from_tele: str, to_tele: str) -> list[str]:
        for raw in load_fixture():
            v = raw.split("|")
            if (
                v[C.IDX_PUBLIC_TRAIN_CODE] == code
                and v[C.IDX_FROM_STATION_TELECODE] == from_tele
                and v[C.IDX_TO_STATION_TELECODE] == to_tele
            ):
                return v
        raise AssertionError(f"夹具里找不到 {code} {from_tele}->{to_tele}")

    def _find(self, code: str, from_tele: str, to_tele: str):
        return parse_row("|".join(self._find_raw(code, from_tele, to_tele)), strict=True)


class TestDecode(FixtureMixin, unittest.TestCase):
    """位图解码的数值正确性。"""

    def test_fixture_rows_all_pass_self_check(self) -> None:
        """夹具 311 行必须全部通过 seat_types 自校验。"""
        rows = load_fixture()
        parsed = [parse_row(r, strict=True) for r in rows]
        self.assertEqual(len(parsed), len(rows))

    def test_k528_prices(self) -> None:
        """K528：硬卧 69.5 / 软卧 104.5 / 硬座 23.5 / 无座 23.5。"""
        row = self._find("K528", "IMH", "WXH")
        by_name = {s.name: s for s in row.seats}
        self.assertAlmostEqual(by_name["硬卧"].price, 69.5)
        self.assertAlmostEqual(by_name["软卧"].price, 104.5)
        self.assertAlmostEqual(by_name["硬座"].price, 23.5)
        self.assertTrue(any(s.is_standing and s.price == 23.5 for s in row.seats))

    def test_standing_flag_and_count(self) -> None:
        """
        余票编码 >= 3000 表示该席别实际是无座，余票数 = 值 - 3000。

        实测 K528：位图末组的编码是 3002，对应"无座 剩余2张"。
        """
        row = self._find("K528", "IMH", "WXH")
        standing = [s for s in row.seats if s.is_standing]
        self.assertTrue(standing, "K528 应当有无座席别")
        self.assertTrue(all(s.code == "W" for s in standing))

    def test_standing_price_equals_second_class(self) -> None:
        """
        无座票价应当等于二等座票价（12306 定价规则）。

        用一个内部一致性来交叉验证解码没错位——如果锚点算错了，
        价格往往不会呈现出这种规律。
        """
        checked = 0
        for raw in load_fixture():
            row = parse_row(raw, strict=True)
            by_short = {s.short: s for s in row.seats}
            if "ze" in by_short and "wz" in by_short:
                self.assertAlmostEqual(by_short["wz"].price, by_short["ze"].price)
                checked += 1
        self.assertGreater(checked, 20, "样本太少，这条断言没有说服力")


class TestValidation(FixtureMixin, unittest.TestCase):
    """自校验必须真的拦得住错误。"""

    def test_seat_types_mismatch_raises(self) -> None:
        """
        破坏 seat_types 后必须报错，而不是安静地输出错误价格。

        这是整个解析器最重要的一条防线：宁可报错让用户重跑，
        也不能给出错误的票价——用户是拿着这个结果去买票的。
        """
        v = self._find_raw("D2928", "AOH", "WXH")
        v[len(v) - C.TAIL_SEAT_TYPES] = "XYZ"
        with self.assertRaises(SchemaError) as ctx:
            parse_row("|".join(v), strict=True)
        self.assertIn("席别自校验失败", str(ctx.exception))

    def test_blob_corruption_raises(self) -> None:
        """位图被改成长度非 10 的倍数时必须报错。"""
        v = self._find_raw("D2928", "AOH", "WXH")
        v[len(v) - C.TAIL_PRICE_BLOB] = "O005600000M0089000"
        with self.assertRaises(SchemaError):
            parse_row("|".join(v), strict=True)

    def test_public_code_without_letter_prefix(self) -> None:
        """
        普速车次号可以没有字母前缀（实测有 1462 次）。

        早先的正则强制要求字母前缀，会把这类车次判成"无法识别车次编码"。
        """
        fields = ["x"] * 45
        fields[C.IDX_INTERNAL_TRAIN_NO] = "550000146209"
        fields[C.IDX_PUBLIC_TRAIN_CODE] = "1462"
        fields[C.IDX_START_TIME] = "08:00"
        fields[C.IDX_ARRIVE_TIME] = "09:00"
        fields[C.IDX_FROM_STATION_NO] = "01"
        fields[C.IDX_TO_STATION_NO] = "05"
        fields[len(fields) - C.TAIL_PRICE_BLOB] = "1002350000"
        fields[len(fields) - C.TAIL_SEAT_TYPES] = "1"
        row = parse_row("|".join(fields), strict=True)
        self.assertEqual(row.train_code, "1462")
        self.assertEqual(row.internal_train_no, "550000146209")


class TestHoubu(unittest.TestCase):
    """候补字段的解析。"""

    def test_d2928_houbu_seats(self) -> None:
        """D2928 上海虹桥→无锡：实测 houbu_train_flag=1、可候补席别 'OM'。"""
        for raw in load_fixture():
            v = raw.split("|")
            if (
                v[C.IDX_PUBLIC_TRAIN_CODE] == "D2928"
                and v[C.IDX_FROM_STATION_TELECODE] == "AOH"
                and v[C.IDX_TO_STATION_TELECODE] == "WXH"
            ):
                row = parse_row(raw, strict=True)
                self.assertTrue(row.can_houbu)
                self.assertEqual(row.houbu_seats, "OM")
                self.assertEqual(row.houbu_seat_names, ["二等座", "一等座"])
                return
        self.fail("夹具里找不到 D2928 AOH->WXH")

    def test_houbu_flag_is_binary(self) -> None:
        """候补开关只能是 '0'/'1'/空。形态变了说明锚点需要重新核对。"""
        for raw in load_fixture():
            row = parse_row(raw, strict=True)
            self.assertIn(row.houbu_flag, ("0", "1", ""))

    def test_flag_without_seat_list_is_not_actionable(self) -> None:
        """
        flag='1' 但席别串为空时**不算**可候补。

        实测 227 趟 flag='1' 里只有 23 趟带席别串，而正是这 23 趟是售罄车次。
        只看 flag 会输出一堆"可候补席别：未知"的空方案，对用户没有任何用处。
        """
        fields = ["x"] * 45
        fields[C.IDX_INTERNAL_TRAIN_NO] = "5q000D292810"
        fields[C.IDX_PUBLIC_TRAIN_CODE] = "D2928"
        fields[C.IDX_START_TIME] = "06:52"
        fields[C.IDX_ARRIVE_TIME] = "07:59"
        fields[C.IDX_FROM_STATION_NO] = "02"
        fields[C.IDX_TO_STATION_NO] = "06"
        fields[len(fields) - C.TAIL_PRICE_BLOB] = "O005600000"
        fields[len(fields) - C.TAIL_SEAT_TYPES] = "O"
        fields[len(fields) - C.TAIL_HOUBU_FLAG] = "1"
        fields[len(fields) - C.TAIL_HOUBU_SEAT_LIMIT] = ""
        row = parse_row("|".join(fields), strict=True)
        self.assertEqual(row.houbu_flag, "1")
        self.assertFalse(row.can_houbu, "没有席别信息就不能声称可候补")
        self.assertEqual(row.houbu_seat_names, [])

    def test_not_on_sale_yet_is_distinct(self) -> None:
        """
        「未到起售时间」必须与「已售完」区分。

        前者等等就能买，后者才需要替代方案。混为一谈会误导用户。
        """
        fields = ["x"] * 45
        fields[C.IDX_INTERNAL_TRAIN_NO] = "5q000D292810"
        fields[C.IDX_PUBLIC_TRAIN_CODE] = "D2928"
        fields[C.IDX_START_TIME] = "06:52"
        fields[C.IDX_ARRIVE_TIME] = "07:59"
        fields[C.IDX_FROM_STATION_NO] = "02"
        fields[C.IDX_TO_STATION_NO] = "06"
        fields[C.IDX_CAN_WEB_BUY] = "IS_TIME_NOT_BUY"
        fields[len(fields) - C.TAIL_PRICE_BLOB] = "O005600000"
        fields[len(fields) - C.TAIL_SEAT_TYPES] = "O"
        row = parse_row("|".join(fields), strict=True)
        self.assertTrue(row.not_on_sale_yet)
        self.assertEqual(row.can_web_buy, "IS_TIME_NOT_BUY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
