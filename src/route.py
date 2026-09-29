# -*- coding: utf-8 -*-
"""
经停站（列车路线图）查询 —— 「买短乘长 / 买长乘短」分析的地基。

要分析一台车能不能分段买，先得知道它到底停哪些站、站序如何。

关键点：这个接口要的是**内部车次号**（train_no，形如 5q000D292810），
不是车次号（D2928）。好在余票接口返回的每一行里就带着内部号，直接从那里取即可，
不需要额外去 search 接口查一次。

接口：
    GET /otn/queryTrainInfo/query?leftTicketDTO.train_no=<内部号>
        &leftTicketDTO.train_date=YYYY-MM-DD&rand_code=
返回 data.data[] 是站点数组，每项含 station_name / arrive_time / start_time /
station_no / running_time / arrive_day_str 等。

实测 D2928 返回 19 站：
    上海松江01(06:23) → 上海虹桥02(06:40/06:52) → 昆山南03 → 苏州园区04
    → 苏州05 → 无锡06(07:59) → … → 烟台19

结果会缓存到 data/route_cache/（默认 6 小时），因为经停站一天内基本不变。
#20260928Abel
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as C
from . import logs
from .client import ApiFormatError, HttpSession, TrainTicketError
from .stations import StationTable

_TIME_RE = re.compile(C.TIME_RE)


@dataclass
class RouteStop:
    """经停站里的一站。"""

    station_no: int
    name: str
    telecode: str        # 由车站表反查得到，查不到则为空
    arrive_time: str
    start_time: str
    running_time: str = ""
    is_first: bool = False   # 始发站
    is_last: bool = False    # 终到站

    @property
    def can_board(self) -> bool:
        """
        能否在此站上车。

        用"不是终点站 + 有发车时刻"判断。不能只看 start_time 是否有效——
        实测终点站（烟台）的 start_time 会被填成和到达时间一样，那样会误判成可上车。
        """
        return (not self.is_last) and bool(_TIME_RE.match(self.start_time))

    @property
    def can_alight(self) -> bool:
        """
        能否在此站下车。

        用"不是始发站 + 有到达时刻"判断。始发站的 arrive_time 是 '----'。
        """
        return (not self.is_first) and bool(_TIME_RE.match(self.arrive_time))

    @property
    def bookable(self) -> bool:
        """是否办理客运（能上或能下）。"""
        return self.can_board or self.can_alight

    @property
    def change_minutes(self) -> int:
        """本站的停靠时长（分钟）。同车分段换座时用来提示是否来得及。"""
        if not (_TIME_RE.match(self.arrive_time) and _TIME_RE.match(self.start_time)):
            return 0
        ah, am = (int(x) for x in self.arrive_time.split(":"))
        sh, sm = (int(x) for x in self.start_time.split(":"))
        delta = (sh * 60 + sm) - (ah * 60 + am)
        return delta if delta >= 0 else delta + 24 * 60

    def to_dict(self) -> dict[str, object]:
        return {
            "station_no": self.station_no,
            "name": self.name,
            "telecode": self.telecode,
            "arrive_time": self.arrive_time,
            "start_time": self.start_time,
            "running_time": self.running_time,
            "is_first": self.is_first,
            "is_last": self.is_last,
            "can_board": self.can_board,
            "can_alight": self.can_alight,
            "change_minutes": self.change_minutes,
        }


class TrainRouteService:
    """经停站查询 + 缓存。"""

    def __init__(self, session: HttpSession, stations: StationTable) -> None:
        self.session = session
        self.stations = stations
        self._memory: dict[tuple[str, str], list[RouteStop]] = {}
        self._cache_dir = (
            Path(__file__).resolve().parent.parent / C.DATA_DIR_NAME / C.ROUTE_CACHE_DIR
        )

    # ------------------------------------------------------------------
    def get_route(self, date: str, internal_train_no: str) -> list[RouteStop]:
        """
        取一趟车的经停站列表。

        失败会抛 TrainTicketError，由调用方决定是跳过这趟车还是整体中止。
        """
        key = (internal_train_no, date)
        if key in self._memory:
            self.session.budget.hit_cache()
            return self._memory[key]

        cached = self._load_disk(key)
        if cached is not None:
            self.session.budget.hit_cache()
            self._memory[key] = cached
            return cached

        stops = self._fetch(internal_train_no, date)
        self._memory[key] = stops
        self._save_disk(key, stops)
        return stops

    # ------------------------------------------------------------------
    def _fetch(self, internal_train_no: str, date: str) -> list[RouteStop]:
        """请求经停站。主接口失败时退回 czxx 接口。"""
        errors: list[str] = []

        try:
            return self._fetch_primary(internal_train_no, date)
        except (TrainTicketError, ApiFormatError) as exc:
            errors.append(f"主接口失败：{exc}")

        try:
            return self._fetch_fallback(internal_train_no, date)
        except (TrainTicketError, ApiFormatError) as exc:
            errors.append(f"备用接口失败：{exc}")

        raise TrainTicketError(
            f"无法获取内部车次号 {internal_train_no} 的经停站（{'；'.join(errors)}）"
        )

    def _fetch_primary(self, internal_train_no: str, date: str) -> list[RouteStop]:
        url = (
            f"{C.TRAIN_ROUTE_URL}?leftTicketDTO.train_no={internal_train_no}"
            f"&leftTicketDTO.train_date={date}&rand_code="
        )
        payload = self.session.get_json(url, kind="route")
        return self._parse(payload)

    def _fetch_fallback(self, internal_train_no: str, date: str) -> list[RouteStop]:
        """
        备用接口 czxx/queryByTrainNo。

        实测主接口一直可用，这里是防御性的。两个电报码参数可以用 BBB 作通配。
        """
        url = (
            f"{C.TRAIN_ROUTE_FALLBACK_URL}?train_no={internal_train_no}"
            f"&from_station_telecode=BBB&to_station_telecode=BBB&depart_date={date}"
        )
        payload = self.session.get_json(url, kind="route")
        return self._parse(payload)

    # ------------------------------------------------------------------
    def _parse(self, payload: dict) -> list[RouteStop]:
        """把接口返回解析成站点列表（按站序升序）。"""
        data = payload.get("data") or {}
        raw_stops = data.get("data") if isinstance(data, dict) else None
        if raw_stops is None:
            raw_stops = data.get("list") or []
        if not isinstance(raw_stops, list) or not raw_stops:
            raise ApiFormatError("经停站接口没有返回站点数据")

        stops: list[RouteStop] = []
        for item in raw_stops:
            if not isinstance(item, dict):
                continue
            name = (item.get("station_name") or "").strip()
            if not name:
                continue
            try:
                no = int(item.get("station_no") or 0)
            except (TypeError, ValueError):
                continue
            station = self.stations.by_name.get(name)
            stops.append(
                RouteStop(
                    station_no=no,
                    name=name,
                    telecode=station.telecode if station else "",
                    arrive_time=(item.get("arrive_time") or C.NO_TIME).strip(),
                    start_time=(item.get("start_time") or C.NO_TIME).strip(),
                    running_time=(item.get("running_time") or "").strip(),
                )
            )

        if not stops:
            raise ApiFormatError("经停站数据解析后为空")

        stops.sort(key=lambda s: s.station_no)
        # 标记首末站，供 can_board / can_alight 判断
        for i, stop in enumerate(stops):
            stop.is_first = i == 0
            stop.is_last = i == len(stops) - 1

        # 站名里有查不到电报码的，说明本地车站表可能过期，提示一下
        missing = [s.name for s in stops if not s.telecode]
        if missing:
            logs.detail(f"以下经停站不在本地车站表中，将无法作为分段方案候选：{'、'.join(missing)}")

        return stops

    # ------------------------------------------------------------------
    def _load_disk(self, key: tuple[str, str]) -> list[RouteStop] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > C.ROUTE_CACHE_TTL_HOURS:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                items = json.load(fh)["stops"]
            count = len(items)
            stops = [
                RouteStop(
                    station_no=int(i["station_no"]),
                    name=i["name"],
                    telecode=i.get("telecode", ""),
                    arrive_time=i.get("arrive_time", C.NO_TIME),
                    start_time=i.get("start_time", C.NO_TIME),
                    running_time=i.get("running_time", ""),
                    is_first=(idx == 0),
                    is_last=(idx == count - 1),
                )
                for idx, i in enumerate(items)
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return stops

    def _save_disk(self, key: tuple[str, str], stops: list[RouteStop]) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._cache_path(key)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"saved_at": int(time.time()),
                     "stops": [s.to_dict() for s in stops]},
                    fh, ensure_ascii=False,
                )
            os.replace(tmp, path)
        except OSError as exc:
            logs.detail(f"经停站缓存写入失败（不影响本次使用）：{exc}")

    def _cache_path(self, key: tuple[str, str]) -> Path:
        train_no, date = key
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", train_no)
        return self._cache_dir / f"{safe}_{date}.json"


# ----------------------------------------------------------------------
def index_of_station(stops: list[RouteStop], telecode: str, name: str) -> int | None:
    """
    在某趟车的经停站里找一站的位置。

    先按电报码找（准确），找不到再按中文名找——因为经停站数据给的是站名，
    而我们可能拿不到它的电报码（本地车站表没收录），这时只能退回名字比对。
    """
    for i, stop in enumerate(stops):
        if stop.telecode and stop.telecode == telecode:
            return i
    for i, stop in enumerate(stops):
        if stop.name == name:
            return i
    return None
