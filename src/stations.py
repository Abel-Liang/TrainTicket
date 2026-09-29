# -*- coding: utf-8 -*-
"""
车站表 —— 车站名/城市名 -> 电报码。

12306 的接口只认三个字母的电报码（北京=VAP、上海虹桥=AOH），所以我们得先把
用户输入的中文名翻译过去。

数据来源是 station_name.js，格式是一长串：
    var station_names ='@bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||@...'
按 "@" 拆记录、再按 "|" 拆字段。实测字段位置：
    [0] 站ID   [1] 中文名   [2] 电报码   [3] 拼音   [4] 首字母
    [5] 序   [6] 城市码   [7] **城市名**   [8..] 海外字段

【关于"城市优先"这条规则】
「上海」既是城市名也是一个车站名。如果按车站解析，`--from 上海` 只会查上海站，
会漏掉上海虹桥、上海南——那正是用户场景里需要的站。城市展开是车站的严格超集，
所以这里**城市优先**，并把解析结果明确打印出来让用户看得见。

文件会缓存到 data/stations.json（默认 7 天），避免每次运行都拉 500KB 的 JS。
#20260928Abel
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C
from . import logs
from .client import HttpSession, TrainTicketError

# 数据文件里字段之间用这个
_RECORD_SEP = "@"
_FIELD_SEP = "|"


@dataclass(frozen=True)
class Station:
    """一个车站。"""

    telecode: str      # 电报码，如 AOH
    name: str          # 中文名，如 上海虹桥
    pinyin: str = ""
    short: str = ""
    city_code: str = ""
    city: str = ""     # 所属城市名，如 上海

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "telecode": self.telecode, "city": self.city}


@dataclass
class Resolution:
    """
    一次用户输入的解析结果。

    kind 取值：
        station   —— 明确的一个车站
        city      —— 一个城市，展开成多个车站
        ambiguous —— 有歧义，需要用户挑一个
        not_found —— 没找到
    """

    kind: str
    stations: list[Station] = field(default_factory=list)
    message: str = ""
    source_text: str = ""

    @property
    def ok(self) -> bool:
        return self.kind in ("station", "city") and bool(self.stations)

    @property
    def telecodes(self) -> list[str]:
        return [s.telecode for s in self.stations]

    def describe(self) -> str:
        """给用户看的一句话说明。"""
        names = "、".join(s.name for s in self.stations)
        if self.kind == "city":
            return f"「{self.source_text}」按城市解析，包含 {len(self.stations)} 个车站：{names}"
        if self.kind == "station":
            return f"「{self.source_text}」按车站解析：{names}"
        return self.message


class StationTable:
    """车站表：加载、缓存、查询。"""

    def __init__(self, session: HttpSession, cache_dir: Path | None = None) -> None:
        self.session = session
        self.cache_dir = cache_dir or (Path(__file__).resolve().parent.parent / C.DATA_DIR_NAME)
        self._cache_file = self.cache_dir / C.STATION_CACHE_FILE

        self.by_telecode: dict[str, Station] = {}
        self.by_name: dict[str, Station] = {}
        # 城市 -> 该市所有车站（按电报码去重后的顺序）
        self.by_city: dict[str, list[Station]] = {}

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def _load_cache(self) -> bool:
        """从磁盘缓存读取。过期或损坏都返回 False。"""
        if not self._cache_file.exists():
            return False
        age_days = (time.time() - self._cache_file.stat().st_mtime) / 86400
        if age_days > C.STATION_CACHE_TTL_DAYS:
            logs.detail(f"车站表缓存已过期（{age_days:.1f} 天）")
            return False
        try:
            with open(self._cache_file, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            stations = [Station(**item) for item in payload["stations"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logs.warn(f"车站表缓存损坏，将重新下载：{exc}")
            return False
        if not stations:
            return False
        self._index(stations)
        logs.detail(f"从缓存载入车站表：{len(stations)} 站")
        return True

    def _save_cache(self, stations: list[Station]) -> None:
        """写缓存。先写临时文件再原子替换——Windows 上直接覆写容易踩到占用。"""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"saved_at": int(time.time()),
                     "stations": [s.__dict__ for s in stations]},
                    fh, ensure_ascii=False,
                )
            os.replace(tmp, self._cache_file)
        except OSError as exc:
            logs.warn(f"车站表缓存写入失败（不影响本次使用）：{exc}")

    def load(self, refresh: bool = False) -> None:
        """加载车站表：优先缓存，必要时联网下载。"""
        if not refresh and self._load_cache():
            return

        logs.detail("正在下载车站表…")
        try:
            text = self.session.get_text(C.STATION_JS_URL, kind="stations")
        except TrainTicketError as exc:
            # 联网失败但本地有旧缓存时，退而用旧的，别让用户什么都查不了
            if self._cache_file.exists():
                logs.warn(f"车站表下载失败（{exc}），改用本地旧缓存")
                try:
                    with open(self._cache_file, "r", encoding="utf-8") as fh:
                        payload = json.load(fh)
                    self._index([Station(**i) for i in payload["stations"]])
                    return
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            raise TrainTicketError(f"无法获取车站数据，且无本地缓存：{exc}") from exc

        stations = self._parse_js(text)
        if not stations:
            raise TrainTicketError(
                "车站数据解析结果为空，12306 可能改版了。"
                "请检查 src/stations.py 里的字段位置。"
            )
        self._index(stations)
        self._save_cache(stations)
        logs.detail(f"车站表已更新：{len(stations)} 站")

    @staticmethod
    def _parse_js(text: str) -> list[Station]:
        """解析 station_name.js。"""
        match = re.search(r"station_names\s*=\s*'(.*?)'\s*;?", text, re.S)
        body = match.group(1) if match else text

        stations: list[Station] = []
        for record in body.split(_RECORD_SEP):
            fields = record.split(_FIELD_SEP)
            if len(fields) < 8:
                continue
            telecode = fields[2].strip()
            name = fields[1].strip()
            if not telecode or not name:
                continue
            stations.append(
                Station(
                    telecode=telecode,
                    name=name,
                    pinyin=fields[3].strip(),
                    short=fields[4].strip(),
                    city_code=fields[6].strip(),
                    city=fields[7].strip(),
                )
            )
        return stations

    def _index(self, stations: list[Station]) -> None:
        """建索引。同名车站取第一个（12306 数据里同名情况极少）。"""
        self.by_telecode.clear()
        self.by_name.clear()
        self.by_city.clear()

        for st in stations:
            self.by_telecode.setdefault(st.telecode, st)
            self.by_name.setdefault(st.name, st)
            if st.city:
                self.by_city.setdefault(st.city, []).append(st)

    # ------------------------------------------------------------------
    # 解析用户输入
    # ------------------------------------------------------------------
    def resolve(self, text: str, exact_station: bool = False) -> Resolution:
        """
        把用户输入的「上海」/「上海虹桥」/「AOH」翻译成车站列表。

        顺序：城市 -> 车站 -> 去掉"站"字重试 -> 电报码 -> 拼音/首字母 -> 模糊包含。
        城市优先的理由见模块顶部说明。
        """
        raw = (text or "").strip().replace("　", "").replace(" ", "")
        if not raw:
            return Resolution("not_found", message="车站名为空", source_text=text)

        # 1) 电报码直查
        upper = raw.upper()
        if upper in self.by_telecode:
            return Resolution("station", [self.by_telecode[upper]], source_text=text)

        # 2) 城市名（--exact-station 时跳过，强制按车站理解）
        if not exact_station:
            hit = self._match_city(raw)
            if hit:
                return Resolution("city", hit, source_text=text)

        # 3) 车站全名
        if raw in self.by_name:
            return Resolution("station", [self.by_name[raw]], source_text=text)

        # 4) 去掉末尾的"站"再试（用户常写「上海站」）
        trimmed = raw[:-1] if raw.endswith("站") and len(raw) > 1 else None
        if trimmed:
            if not exact_station:
                hit = self._match_city(trimmed)
                if hit:
                    return Resolution("city", hit, source_text=text)
            if trimmed in self.by_name:
                return Resolution("station", [self.by_name[trimmed]], source_text=text)

        # 5) 拼音 / 首字母（唯一时才采纳）
        fuzzy = [s for s in self.by_telecode.values()
                 if s.pinyin == raw.lower() or s.short == raw.lower()]
        if len(fuzzy) == 1:
            return Resolution("station", fuzzy, source_text=text)
        if len(fuzzy) > 1:
            return Resolution("ambiguous", fuzzy, source_text=text,
                              message=f"「{raw}」匹配到多个车站，请从候选中选择")

        # 6) 包含匹配：唯一车站就采纳；同城则当城市（会扩大查询范围，所以给出提示）
        partial = [s for s in self.by_telecode.values() if raw in s.name]
        if len(partial) == 1:
            return Resolution("station", partial, source_text=text)
        if len(partial) > 1:
            cities = {s.city for s in partial}
            if len(cities) == 1 and not exact_station:
                return Resolution("city", partial, source_text=text,
                                  message=f"「{raw}」未精确匹配，按同城车站展开")
            return Resolution(
                "ambiguous", partial, source_text=text,
                message=f"「{raw}」匹配到多个车站，请写得更精确或加 --exact-station",
            )

        return Resolution(
            "not_found", source_text=text,
            message=f"找不到车站「{raw}」。可用 --refresh-stations 刷新车站表后重试。",
        )

    def _match_city(self, text: str) -> list[Station] | None:
        """按城市名匹配（含"市"字后缀的写法）。"""
        candidates = [text, text[:-1] if text.endswith("市") else text + "市"]
        for key in candidates:
            if key in self.by_city and self.by_city[key]:
                return list(self.by_city[key])
        return None

    # ------------------------------------------------------------------
    def name_of(self, telecode: str, fallback_map: dict[str, str] | None = None) -> str:
        """
        电报码 -> 中文名。

        优先用接口返回的 map（它一定包含本次查询涉及的车站），
        再用本地车站表兜底。
        """
        if fallback_map and telecode in fallback_map:
            return fallback_map[telecode]
        st = self.by_telecode.get(telecode)
        return st.name if st else telecode

    def city_of(self, telecode: str) -> str:
        """电报码 -> 城市名。找不到就返回空串。"""
        st = self.by_telecode.get(telecode)
        return st.city if st else ""


def find_station_by_name(table: StationTable, name: str) -> Station | None:
    """按中文名精确查一个车站（经停站数据里给的是站名，需要翻成电报码）。"""
    return table.by_name.get(name)
