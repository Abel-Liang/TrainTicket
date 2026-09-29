# -*- coding: utf-8 -*-
"""
日志输出 —— 统一走 stderr。

为什么要单独一个模块：`--format json` 时 stdout 必须是**干净的 JSON**，方便
用管道喂给别的脚本。所以所有提示、警告、错误一律写到 stderr，stdout 只留给
真正的结果数据。#20260928Abel
"""

import sys

# 是否输出详细日志（由 --verbose 打开）
_VERBOSE = False

# 是否只用 ASCII 输出（--ascii）。有些终端渲染不了制表符和 emoji。
_ASCII_ONLY = False


def setup_console(ascii_only: bool = False) -> None:
    """
    配置控制台输出编码。

    实测：Windows 上默认是 GBK(cp936)，直接 print("¥") 或 emoji 会抛
    UnicodeEncodeError 让程序崩掉。这里把 stdout/stderr 都切到 UTF-8，
    并对不支持的字符做替换而不是崩溃。

    没有这个函数的话，程序在绝大多数中文 Windows 上跑不起来。
    """
    global _ASCII_ONLY
    _ASCII_ONLY = bool(ascii_only)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # 流被重定向成不支持 reconfigure 的对象时忽略即可
            pass


def ascii_only() -> bool:
    return _ASCII_ONLY


def set_verbose(flag: bool) -> None:
    """设置详细模式。"""
    global _VERBOSE
    _VERBOSE = bool(flag)


def is_verbose() -> bool:
    return _VERBOSE


# 额外的输出汇聚点。GUI 里没有控制台，靠它把进度显示到界面上。
_sink = None


def set_sink(fn) -> None:
    """
    注册一个回调，每条日志都会额外喂给它一份。

    用于图形界面：Tkinter 程序没有 stderr 可看，需要把 "正在查询…"
    这类进度显示在状态栏里。传 None 取消。
    """
    global _sink
    _sink = fn


def _emit(text: str) -> None:
    """写一行到 stderr。容错处理编码，避免 GBK 控制台直接崩掉。"""
    if _sink is not None:
        try:
            _sink(text)
        except Exception:
            # 汇聚点自身出错绝不能影响主流程
            pass
    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        # 极端情况下控制台编码不支持这些字符，降级成可打印形式
        enc = getattr(sys.stderr, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"), file=sys.stderr)


def info(msg: str) -> None:
    """普通提示。"""
    _emit(f"[信息] {msg}")


def warn(msg: str) -> None:
    """警告——不影响主流程，但用户应该知道。"""
    _emit(f"[警告] {msg}")


def error(msg: str) -> None:
    """错误。"""
    _emit(f"[错误] {msg}")


def ok(msg: str) -> None:
    """成功。"""
    _emit(f"✅ {msg}")


def detail(msg: str) -> None:
    """细节信息，只在 --verbose 下输出。"""
    if _VERBOSE:
        _emit(f"    · {msg}")
