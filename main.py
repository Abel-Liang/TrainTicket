# -*- coding: utf-8 -*-
"""
TrainTicket —— 12306 车票查询 + 无票替代方案分析

入口。真正的逻辑在 src/cli.py，这里只负责把 src 目录挂进 import 路径
（沿用本机其它项目的做法：不把项目装成包，直接 sys.path.insert）。

用法示例：
    .venv\\Scripts\\python.exe main.py -d 2026-10-02 -f 上海虹桥 -t 无锡 --time 06:00-12:00 --analyze

注意：main.py 里**不能**省略对控制台编码的设置。实测在中文 Windows 上默认是
GBK，直接打印 "¥" 或表格制表符会抛 UnicodeEncodeError 让程序崩掉。
#20260928Abel
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.cli import run  # noqa: E402


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
