# -*- coding: utf-8 -*-
"""
TrainTicket 图形界面（Tkinter，零新增依赖）。

用标准库自带的 Tkinter 写，不需要装任何东西。界面分三块：
  · 顶部：查询条件（出发站 / 到达站 / 日期 / 发车时段 / 车型 / 分析开关）
  · 中部：车次表格，**点列头即可排序**（发车时间、最低价等）
  · 底部：替代方案分析结果 + 请求统计

车站输入框是"边打边检索"的：输入「上海」会列出上海、上海虹桥、上海南…，
可以直接敲站名也可以敲城市名（城市会自动展开为全市车站）。

【关键实现点】
  1. 所有网络请求都在后台线程跑。直接在按钮回调里发请求会让窗口"假死"，
     用户会以为程序挂了。
  2. 后台线程**不直接碰界面**。Tkinter 只允许创建控件的那个线程操作控件，
     所以后台线程把"要做的事"塞进队列，主线程每 80ms 取出来执行。
     见 _dispatch / _pump_queue。
  3. GUI 没有控制台，所以给 logs 注册了一个汇聚点，把进度显示到状态栏。
  4. 表格排序：Treeview 本身不带排序，这里在点列头时按存的排序键重排。

启动：
    .venv\\Scripts\\python.exe ui.py
    或双击 run-ui.bat
#20260928Abel
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import config as C  # noqa: E402
from src import logs  # noqa: E402
from src import output as out  # noqa: E402
from src.analysis import Analyzer, Options  # noqa: E402
from src.cli import (  # noqa: E402
    PresaleError,
    in_window,
    matches_type,
    parse_train_types,
    parse_window,
    resolve_date,
)
from src.client import Budget, HttpSession, TrainTicketError  # noqa: E402
from src.parser import TicketRow  # noqa: E402
from src.route import TrainRouteService  # noqa: E402
from src.stations import StationTable  # noqa: E402
from src.ticket import LeftTicketService  # noqa: E402

# 界面字体：中文 Windows 上用雅黑最好看，退回系统默认也能跑
FONT_UI = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_MONO = ("Consolas", 9)

# 预设时段按钮：(标签, 起始, 结束)
TIME_PRESETS = [
    ("全天", "00:00", "24:00"),
    ("早上 06-12", "06:00", "12:00"),
    ("下午 12-18", "12:00", "18:00"),
    ("晚上 18-24", "18:00", "24:00"),
]

# 下拉框最多列出多少个候选，避免几千个站名拖慢界面
MAX_SUGGESTIONS = 60


def enable_dpi_awareness() -> None:
    """
    告诉 Windows 本程序自己处理高 DPI。

    不做这一步的话，在缩放 125%/150% 的屏幕上字会发虚。失败就算了，
    不影响功能。
    """
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class StationCombo(ttk.Combobox):
    """
    车站输入框：边打字边过滤候选。

    Tkinter 的 Combobox 本身不会随输入过滤，这里在每次按键后重设 values
    来实现。站名是精确匹配的（不做拼音），因为中文输入法切来切去更麻烦。
    """

    def __init__(self, master: tk.Misc, **kw) -> None:
        super().__init__(master, **kw)
        self._all_names: list[str] = []
        self.bind("<KeyRelease>", self._on_key)
        self.bind("<<ComboboxSelected>>", lambda _e: self._close_dropdown())

    def set_source(self, names: list[str]) -> None:
        """灌入全部站名。由后台线程加载完成后调用。"""
        self._all_names = sorted(set(names))
        self["values"] = self._all_names[:MAX_SUGGESTIONS]

    def _on_key(self, event: tk.Event) -> None:
        # 方向键 / 回车是在候选里导航，不该触发重新过滤
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        text = self.get().strip()
        if not text:
            self["values"] = self._all_names[:MAX_SUGGESTIONS]
            return
        # 先列"以此开头"的，再列"包含"的——前者几乎总是用户想要的
        starts = [n for n in self._all_names if n.startswith(text)]
        contains = [n for n in self._all_names if text in n and not n.startswith(text)]
        self["values"] = (starts + contains)[:MAX_SUGGESTIONS]
        self._open_dropdown()

    def _open_dropdown(self) -> None:
        """把候选列表展开出来。个别 Tk 版本没有 Post，失败就静默跳过。"""
        try:
            self.tk.call("ttk::combobox::Post", self)
        except tk.TclError:
            pass

    def _close_dropdown(self) -> None:
        try:
            self.tk.call("ttk::combobox::Unpost", self)
        except tk.TclError:
            pass


class CheckToggle(ttk.Button):
    """
    复选按钮：选中时方框里显示 √。

    不用自带的 ttk.Checkbutton，是因为它的指示器画出来是个叉或实心块，
    想换成 √ 得重画主题，很折腾。这里直接用按钮文字来表现：
        选中 -> [√] 标签
        未选 -> [　] 标签     （用全角空格占位，保证两种状态宽度一致不跳动）
    """

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        command: Callable[[], None] | None = None,
        **kw: Any,
    ) -> None:
        self._var = variable
        self._label = text
        self._extra = command
        super().__init__(master, command=self._toggle, **kw)
        # 变量被别处改动时（比如程序里重置）也要跟着刷新
        self._var.trace_add("write", lambda *_: self._refresh())
        self._refresh()

    def _toggle(self) -> None:
        self._var.set(not self._var.get())
        if self._extra is not None:
            self._extra()

    def _refresh(self) -> None:
        self.configure(text=("[√] " if self._var.get() else "[　] ") + self._label)


class TrainTicketUI:
    """主窗口。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("12306 车票查询")
        root.geometry("1180x760")
        root.minsize(940, 600)

        # 车站表由后台线程加载，加载完成前保持 None（此时禁止查询）
        self.stations: StationTable | None = None

        # 表格行的排序键：iid -> {...}
        self._row_keys: dict[str, dict[str, Any]] = {}
        self._sort_column = "发车"
        self._sort_reverse = False
        self._busy = False

        # 修改变量
        self.var_from = tk.StringVar(value="上海虹桥")
        self.var_to = tk.StringVar(value="无锡")
        self.var_date = tk.StringVar(value="tomorrow")
        self.var_time_start = tk.StringVar(value="00:00")
        self.var_time_end = tk.StringVar(value="24:00")
        self.var_types = tk.StringVar(value="全部")
        self.var_analyze = tk.BooleanVar(value=True)
        self.var_exact = tk.BooleanVar(value=False)
        # 风险提示默认收起：⚠ 那类行（票面发站、不退票款…）信息量大但很占地方。
        # 做成开关而不是直接删掉——需要的时候一键就能看回来。
        self.var_show_risks = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="正在加载车站表…")
        # 查询结果条数，显示在条件区下方（主行 + 副行）
        self.var_result = tk.StringVar(value="尚未查询")
        self.var_result_sub = tk.StringVar(value="")

        # 上一次的查询结果，供「显示风险提示」开关切换时原地重绘，不必重新查
        self._last_result: dict[str, Any] | None = None

        # 后台线程 -> 主线程的任务队列。
        # 【为什么要有这个】Tkinter 只允许"创建控件的那个线程"操作控件。
        # 在后台线程里直接调 root.after() 会抛
        # "RuntimeError: main thread is not in main loop"，或者更糟——偶发崩溃。
        # 所以后台线程一律把"要做的事"塞进队列，主线程定期取出来执行。
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self._build_widgets()

        # 「显示风险提示」用变量监听驱动重绘，而不是挂在按钮的 command 上。
        # 这样无论谁改了它（点按钮、程序里 set）都会立刻刷新，不会出现
        # "开关变了但内容没变"的不一致。
        self.var_show_risks.trace_add("write", lambda *_: self._rerender_analysis())

        # 把所有日志汇聚到状态栏。底层服务（余票查询、经停站、分析）本来就会
        # 打日志，这样它们跑到哪一步用户直接能看到，不用我在每处手工插桩。
        logs.set_sink(self._on_log)

        self._pump_queue()
        self._start_station_load()

    # ------------------------------------------------------------------
    def _dispatch(self, fn: Callable[[], None]) -> None:
        """把一个要在主线程执行的函数排进队列。任何线程都可以安全调用。"""
        self._queue.put(fn)

    def _pump_queue(self) -> None:
        """
        主线程定时把队列里攒下的任务执行掉。

        80ms 一次：快到人感觉不出延迟，又不会白耗 CPU。
        """
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except tk.TclError:
                # 窗口已经关掉了，剩下的任务没有意义
                return
            except Exception:  # noqa: BLE001  单个任务出错不该拖垮整个界面
                pass
        self.root.after(80, self._pump_queue)

    def _on_log(self, text: str) -> None:
        """
        接收一条日志并显示到状态栏。可能来自后台线程，所以走队列。

        日志里带 [信息]/[警告]/[错误] 前缀，状态栏上没必要，去掉更清爽。
        """
        cleaned = text.strip()
        for prefix in ("[信息] ", "[警告] ", "[错误] ", "✅ "):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        if cleaned:
            self._dispatch(lambda: self.var_status.set(cleaned))

    # ==================================================================
    # 界面搭建
    # ==================================================================
    def _build_widgets(self) -> None:
        style = ttk.Style()
        # 部分主题下 Treeview 的表头不显示，clam 最稳
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", font=FONT_UI, rowheight=24)
        style.configure("Treeview.Heading", font=FONT_TITLE)

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        self._build_condition_panel(outer)
        self._build_result_bar(outer)

        # 表格与分析上下可拖动，方便用户按需分配空间
        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True, pady=(8, 6))

        self._build_table(paned)
        self._build_analysis_panel(paned)

        self._build_status_bar()

    # ------------------------------------------------------------------
    def _build_condition_panel(self, parent: tk.Misc) -> None:
        box = ttk.LabelFrame(parent, text=" 查询条件 ", padding=10)
        box.pack(fill="x")

        # ---- 第一行：出发站 / 到达站 ----
        row1 = ttk.Frame(box)
        row1.pack(fill="x", pady=(0, 8))

        ttk.Label(row1, text="出发站", font=FONT_UI).pack(side="left")
        self.cmb_from = StationCombo(row1, textvariable=self.var_from, width=18, font=FONT_UI)
        self.cmb_from.pack(side="left", padx=(6, 4))

        ttk.Button(row1, text="⇄", width=3, command=self._swap_stations).pack(side="left")

        ttk.Label(row1, text="到达站", font=FONT_UI).pack(side="left", padx=(10, 0))
        self.cmb_to = StationCombo(row1, textvariable=self.var_to, width=18, font=FONT_UI)
        self.cmb_to.pack(side="left", padx=(6, 16))

        CheckToggle(
            row1, "只按具体车站（不展开城市）", self.var_exact
        ).pack(side="left")

        # ---- 第二行：日期 / 发车时段 ----
        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(0, 8))

        ttk.Label(row2, text="乘车日期", font=FONT_UI).pack(side="left")
        ttk.Entry(row2, textvariable=self.var_date, width=12, font=FONT_UI).pack(
            side="left", padx=(6, 2)
        )
        ttk.Label(row2, text="(可填 2026-10-02 / today / tomorrow / +3)", font=FONT_UI).pack(
            side="left", padx=(0, 18)
        )

        ttk.Label(row2, text="发车时段", font=FONT_UI).pack(side="left")
        ttk.Entry(row2, textvariable=self.var_time_start, width=7, font=FONT_UI).pack(
            side="left", padx=(6, 2)
        )
        ttk.Label(row2, text="至", font=FONT_UI).pack(side="left")
        ttk.Entry(row2, textvariable=self.var_time_end, width=7, font=FONT_UI).pack(
            side="left", padx=(2, 6)
        )
        for label, start, end in TIME_PRESETS:
            ttk.Button(
                row2,
                text=label,
                width=10,
                command=lambda s=start, e=end: self._apply_preset(s, e),
            ).pack(side="left", padx=2)

        # ---- 第三行：车型 / 分析开关 / 查询 ----
        row3 = ttk.Frame(box)
        row3.pack(fill="x")

        ttk.Label(row3, text="车型", font=FONT_UI).pack(side="left")
        ttk.Combobox(
            row3,
            textvariable=self.var_types,
            values=["全部", "只看高铁动车(G/D)", "排除普速(K/T/Z)", "只看高铁(G)"],
            state="readonly",
            width=17,
            font=FONT_UI,
        ).pack(side="left", padx=(6, 16))

        CheckToggle(row3, "无票时分析替代方案", self.var_analyze).pack(
            side="left", padx=(0, 10)
        )
        CheckToggle(row3, "显示风险提示", self.var_show_risks).pack(
            side="left", padx=(0, 16)
        )

        self.btn_query = ttk.Button(row3, text="查询", width=12, command=self.on_query)
        self.btn_query.pack(side="left")

        ttk.Button(row3, text="清空结果", width=10, command=self._clear_results).pack(
            side="left", padx=6
        )

    # ------------------------------------------------------------------
    def _build_result_bar(self, parent: tk.Misc) -> None:
        """
        条件区下方：本次查到多少条数据。

        分两行是为了不被窗口宽度截断——挤成一行时右边的信息会看不见。
        """
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, textvariable=self.var_result, font=FONT_TITLE).pack(anchor="w")
        ttk.Label(bar, textvariable=self.var_result_sub, font=FONT_UI).pack(anchor="w")

    # ------------------------------------------------------------------
    def _build_table(self, parent: ttk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text=" 车次（点击列头排序） ", padding=4)
        parent.add(frame, weight=3)

        columns = ("车次", "上车站", "发车", "下车站", "到达", "历时", "最低价", "状态", "席别明细")
        widths = (72, 96, 62, 96, 62, 62, 78, 88, 460)
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        for col, width in zip(columns, widths):
            # 列头显示排序箭头，让用户知道当前按什么排
            self.tree.heading(col, text=col, command=lambda c=col: self._sort_by(c))
            anchor = "w" if col in ("席别明细", "上车站", "下车站") else "center"
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "席别明细"))

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # 无票的行标红，一眼能看出哪些需要看替代方案
        self.tree.tag_configure("sold_out", foreground="#b00020")
        self.tree.tag_configure("available", foreground="#1a7f37")

    # ------------------------------------------------------------------
    def _build_analysis_panel(self, parent: ttk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text=" 替代方案分析 ", padding=4)
        parent.add(frame, weight=2)

        self.txt_analysis = tk.Text(
            frame, wrap="none", font=FONT_MONO, height=14, background="#fbfbfb"
        )
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.txt_analysis.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.txt_analysis.xview)
        self.txt_analysis.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.txt_analysis.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    # ------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.var_status, font=FONT_UI).pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.progress.pack(side="right")

    # ==================================================================
    # 小交互
    # ==================================================================
    def _apply_preset(self, start: str, end: str) -> None:
        self.var_time_start.set(start)
        self.var_time_end.set(end)

    def _swap_stations(self) -> None:
        a, b = self.var_from.get(), self.var_to.get()
        self.var_from.set(b)
        self.var_to.set(a)

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._row_keys.clear()
        self.txt_analysis.delete("1.0", "end")
        self._last_result = None
        self.var_result.set("尚未查询")
        self.var_result_sub.set("")
        self.var_status.set("已清空")

    def _set_status(self, text: str) -> None:
        self.var_status.set(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_query.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    # ==================================================================
    # 车站表加载（后台）
    # ==================================================================
    def _start_station_load(self) -> None:
        """
        后台加载车站表。

        首次运行要联网下载 500KB 的车站数据，放在主线程会把窗口卡住，
        所以丢到线程里，加载完再回主线程灌进下拉框。
        """
        def worker() -> None:
            try:
                session = HttpSession(budget=Budget(3))
                table = StationTable(session)
                table.load()
                names = list(table.by_name.keys())
                self._dispatch(lambda: self._on_stations_loaded(table, names))
            except Exception as exc:  # noqa: BLE001  （要把任何失败都告诉用户）
                self._dispatch(lambda: self._on_stations_failed(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_stations_loaded(self, table: StationTable, names: list[str]) -> None:
        self.stations = table
        self.cmb_from.set_source(names)
        self.cmb_to.set_source(names)
        self._set_status(f"车站表就绪（{len(names)} 站）。填好条件后点「查询」。")

    def _on_stations_failed(self, exc: Exception) -> None:
        self._set_status("车站表加载失败")
        messagebox.showerror(
            "车站表加载失败",
            f"无法获取车站数据：\n{exc}\n\n"
            "请检查网络连接后重启程序。\n"
            "（如果之前成功运行过，本地缓存仍可用）",
        )

    # ==================================================================
    # 查询
    # ==================================================================
    def on_query(self) -> None:
        if self._busy:
            return
        if self.stations is None:
            messagebox.showwarning("请稍候", "车站表还在加载中，请稍等几秒再试。")
            return

        try:
            params = self._collect_params()
        except PresaleError as exc:
            messagebox.showerror("参数有误", str(exc))
            return

        self._clear_results()
        self._set_busy(True)
        self._set_status("查询中…")
        threading.Thread(target=self._worker, args=(params,), daemon=True).start()

    def _collect_params(self) -> dict[str, Any]:
        """把界面上的输入收集并校验成一个参数字典。"""
        assert self.stations is not None

        date = resolve_date(self.var_date.get())
        start, end, label = parse_window(
            None, self.var_time_start.get(), self.var_time_end.get()
        )

        from_text = self.var_from.get().strip()
        to_text = self.var_to.get().strip()
        if not from_text or not to_text:
            raise PresaleError("出发站和到达站都要填。")

        res_from = self.stations.resolve(from_text, exact_station=self.var_exact.get())
        res_to = self.stations.resolve(to_text, exact_station=self.var_exact.get())
        for res in (res_from, res_to):
            if res.kind == "not_found":
                raise PresaleError(res.message)
            if res.kind == "ambiguous":
                names = "、".join(s.name for s in res.stations[:12])
                raise PresaleError(f"{res.message}：\n{names}\n请写得更精确一些。")

        types_text = self.var_types.get()
        if types_text == "只看高铁动车(G/D)":
            keep, drop = parse_train_types("GD")
        elif types_text == "排除普速(K/T/Z)":
            keep, drop = parse_train_types("-KTZ")
        elif types_text == "只看高铁(G)":
            keep, drop = parse_train_types("G")
        else:
            keep, drop = set(), set()

        return {
            "date": date,
            "window_label": label,
            "start": start,
            "end": end,
            "from_res": res_from,
            "to_res": res_to,
            "from_allowed": [s.telecode for s in res_from.stations],
            "to_allowed": [s.telecode for s in res_to.stations],
            "keep_types": keep,
            "drop_types": drop,
            "analyze": self.var_analyze.get(),
        }

    # ------------------------------------------------------------------
    def _worker(self, params: dict[str, Any]) -> None:
        """后台线程：跑完整的查询 + 分析流程。结果统一走队列回主线程。"""
        try:
            result = self._run_query(params)
            self._dispatch(lambda: self._on_done(result))
        except TrainTicketError as exc:
            self._dispatch(lambda: self._on_error(str(exc)))
        except Exception as exc:  # noqa: BLE001
            self._dispatch(lambda: self._on_error(f"未预期的错误：{exc}"))

    def _run_query(self, params: dict[str, Any]) -> dict[str, Any]:
        """真正的查询流程。注意：这个方法运行在后台线程里，不要碰界面。"""
        assert self.stations is not None

        budget = Budget(C.DEFAULT_MAX_REQUESTS)
        session = HttpSession(budget=budget)
        tickets = LeftTicketService(session, self.stations)
        routes = TrainRouteService(session, self.stations)

        date = params["date"]
        from_allowed = params["from_allowed"]
        to_allowed = params["to_allowed"]

        tickets.query_raw(date, from_allowed[0], to_allowed[0])

        rows = tickets.rows_for(date, set(from_allowed), set(to_allowed))
        rows = [
            r
            for r in rows
            if in_window(r, params["start"], params["end"])
            and matches_type(r.train_code, params["keep_types"], params["drop_types"])
        ]

        # 同一车次可能因同城多站出现多行，按 (车次, 上车站) 去重
        raw_count = len(rows)
        dedup: dict[tuple[str, str], TicketRow] = {}
        for row in rows:
            dedup.setdefault((row.train_code, row.from_tele), row)
        rows = sorted(dedup.values(), key=lambda r: (r.start_time, r.train_code))
        deduped_count = raw_count - len(rows)

        station_names = dict(tickets.station_map)
        for code in set(from_allowed) | set(to_allowed):
            station_names[code] = self.stations.name_of(code, station_names)

        analysis = None
        if params["analyze"] and any(not r.has_ticket for r in rows):
            analyzer = Analyzer(
                session, self.stations, tickets, routes, Options(max_analyze_trains=8)
            )
            analysis = analyzer.analyze(date, from_allowed[0], to_allowed[0], rows)

        return {
            "rows": rows,
            "station_names": station_names,
            "analysis": analysis,
            "budget": budget.snapshot(),
            "tickets_snapshot": tickets.snapshot(),
            "params": params,
            "deduped": deduped_count,
        }

    # ------------------------------------------------------------------
    def _on_done(self, result: dict[str, Any]) -> None:
        self._set_busy(False)
        self._last_result = result
        self._populate_table(result["rows"], result["station_names"])
        self._update_result_bar(result)
        self._render_analysis_pane(result)

        total = len(result["rows"])
        sold = sum(1 for r in result["rows"] if not r.has_ticket)
        self._set_status(f"完成：共 {total} 趟车次，其中无票 {sold} 趟。点列头可排序。")

    # ------------------------------------------------------------------
    def _update_result_bar(self, result: dict[str, Any]) -> None:
        """更新"本次查到多少条数据"那两行。"""
        rows = result["rows"]
        total = len(rows)
        sold = sum(1 for r in rows if not r.has_ticket)
        pairs = result["tickets_snapshot"].get("cached_station_pairs", 0)
        params = result["params"]
        window = params.get("window_label") or "全天"

        self.var_result.set(
            f"查询结果：{total} 条车次数据　｜　有票 {total - sold} 条　｜　无票 {sold} 条"
        )
        sub = [
            f"{params['date']}  {window}",
            f"{params['from_res'].source_text} → {params['to_res'].source_text}",
            f"接口返回 {pairs} 个车站组合",
        ]
        if result.get("deduped"):
            # 同城多站会让同一车次出现多行，合并了多少要说清楚，否则数字对不上
            sub.append(f"已合并 {result['deduped']} 条同车次重复行")
        self.var_result_sub.set("　｜　".join(sub))

    def _render_analysis_pane(self, result: dict[str, Any]) -> None:
        """渲染分析区。show_risks 由界面开关决定。"""
        analysis = result["analysis"]
        if analysis is not None:
            text = out.render_analysis(
                analysis,
                result["station_names"],
                show_risks=self.var_show_risks.get(),
            )
        else:
            text = "（没有需要分析的无票车次，或已关闭分析）"
        stats = out.render_stats(
            result["budget"],
            result["tickets_snapshot"],
            analysis.probes_run if analysis else [],
            analysis.probes_dropped if analysis else [],
        )
        self.txt_analysis.delete("1.0", "end")
        self.txt_analysis.insert("1.0", text + "\n" + stats)

    def _rerender_analysis(self) -> None:
        """
        开关「显示风险提示」时原地重绘。

        用上次存下的结果重绘，不重新查询——重查一次要好几秒且白白消耗请求额度。
        """
        if self._last_result is not None:
            self._render_analysis_pane(self._last_result)

    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        self._set_status("查询失败")
        messagebox.showerror("查询失败", message)

    # ==================================================================
    # 表格填充与排序
    # ==================================================================
    def _populate_table(
        self, rows: list[TicketRow], station_names: dict[str, str]
    ) -> None:
        self.tree.delete(*self.tree.get_children())
        self._row_keys.clear()

        for row in rows:
            avail = row.cheapest_available()
            all_seats = sorted(row.seats, key=lambda s: s.price)
            # 排序用的价格：有票的取最便宜的；全无票就取该车次最低票价（标出来）
            price_key = avail.price if avail else (
                all_seats[0].price if all_seats else float("inf")
            )
            if avail:
                price_text = f"¥{avail.price:g}"
                status = avail.count_text
            elif row.not_on_sale_yet:
                price_text = f"¥{all_seats[0].price:g}" if all_seats else "—"
                status = "未到起售时间"
            else:
                price_text = f"¥{all_seats[0].price:g}" if all_seats else "—"
                status = "无票"

            detail = " | ".join(
                f"{s.name} ¥{s.price:g} {s.count_text}" for s in row.seats
            ) or "（无席别数据）"

            iid = self.tree.insert(
                "",
                "end",
                values=(
                    row.train_code,
                    station_names.get(row.from_tele, row.from_tele),
                    row.start_time,
                    station_names.get(row.to_tele, row.to_tele),
                    row.arrive_time,
                    row.lishi,
                    price_text,
                    status,
                    detail,
                ),
                tags=("available" if row.has_ticket else "sold_out",),
            )
            self._row_keys[iid] = {
                "车次": row.train_code,
                "上车站": station_names.get(row.from_tele, row.from_tele),
                "发车": row.start_time,
                "下车站": station_names.get(row.to_tele, row.to_tele),
                "到达": row.arrive_time,
                "历时": row.lishi,
                "最低价": price_key,
                "状态": status,
                "席别明细": detail,
                # 有票的排在无票前面，这样"按发车时间"排序时最有用
                "_has_ticket": 1 if row.has_ticket else 0,
            }

        self._apply_sort()

    def _sort_by(self, column: str) -> None:
        """点列头排序。再点一次切换升降序。"""
        if column == self._sort_column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._apply_sort()

    def _apply_sort(self) -> None:
        items = list(self.tree.get_children())
        if not items:
            return
        column = self._sort_column

        def key(iid: str) -> tuple:
            value = self._row_keys[iid][column]
            # 价格排序：无票的按无穷大处理，升序时沉到最后
            if column == "最低价":
                return (0, value if isinstance(value, (int, float)) else float("inf"))
            return (0, value)

        try:
            items.sort(key=key, reverse=self._sort_reverse)
        except TypeError:
            # 同一列里混了不同类型时退化成按字符串排，别让界面崩
            items.sort(key=lambda i: str(self._row_keys[i][column]),
                       reverse=self._sort_reverse)

        for index, iid in enumerate(items):
            self.tree.move(iid, "", index)

        # 列头加箭头，指明当前排序方式
        arrow = " ▼" if self._sort_reverse else " ▲"
        for col in self.tree["columns"]:
            self.tree.heading(col, text=col + (arrow if col == column else ""))

        # 重排后把视图拉回顶部。
        # 不这么做的话，插入/移动几十行之后 Treeview 会停在末尾，
        # 用户看到的是最晚的车次，会以为没查到早班车。
        self.tree.yview_moveto(0.0)


def main() -> int:
    logs.setup_console()
    enable_dpi_awareness()
    root = tk.Tk()
    TrainTicketUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
