# -*- coding: utf-8 -*-
"""
配置区 —— 全项目的常量集中在这里。

【重要提醒｜务必先读】
12306 的余票接口返回的每一行是一个用 "|" 分隔的长字符串。网上（以及参考项目
Joooook/12306-mcp）流传的那张"固定下标表"**已经失效**：因为可选字段为空时会被
整个省略，同一批响应里各行的长度并不一致（实测见过 53~58 共 6 种长度）。
照抄固定下标不会报错，而是会**静默算出错误的价格**——这是最危险的失败模式。

本项目的应对办法叫「头尾双锚定」：
  · 头部字段（下标 0~19）：位置稳定，直接按下标取。
  · 尾部字段：一律写作 len(行) - N。删掉中间某个字段时，行尾字段的绝对下标和
    行长度会同时减 1，所以"倒数第 N 个"永远不变。
  · 中间那段字段下标会漂移，**不可依赖**，本项目不使用它们。

锚点数值由 tools/schema_probe.py 对线上真实数据实测校验后冻结于此。
#20260928Abel
"""

# ==================== 配置区 ====================

VERSION = "1.0.0"
PROJECT_NAME = "TrainTicket"

# -------------------- 接口地址 --------------------
API_BASE = "https://kyfw.12306.cn"
SEARCH_API_BASE = "https://search.12306.cn"
WEB_URL = "https://www.12306.cn/index/"

# 余票接口的"初始化"页。它有两个作用：
#   1) 让服务端下发 cookie（JSESSIONID / SF_cookie_2 / BIGipServerotn）
#   2) 页面 JS 里带着当前有效的余票查询路径
LEFT_TICKET_INIT_URL = f"{API_BASE}/otn/leftTicket/init"

# 车站名数据（直连 kyfw 最完整，实测 3396 站）
STATION_JS_URL = f"{API_BASE}/otn/resources/js/framework/station_name.js"

# 经停站查询（需要内部的 train_no，不是车次号）
TRAIN_ROUTE_URL = f"{API_BASE}/otn/queryTrainInfo/query"
# 备用经停站接口（部分场景可用，主接口失败时兜底）
TRAIN_ROUTE_FALLBACK_URL = f"{API_BASE}/otn/czxx/queryByTrainNo"
# 车次号 -> 内部 train_no（正常情况下不需要，余票行里已带）
TRAIN_SEARCH_URL = f"{SEARCH_API_BASE}/search/v1/train/search"

# 余票路径发现失败的兜底（正常会从 init 页动态取到）
DEFAULT_LEFT_TICKET_PATH = "leftTicket/queryG"

# -------------------- 请求头 / 限速 --------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFERER = f"{API_BASE}/otn/leftTicket/init"
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9"

# 12306 大约 3~5 请求/秒/IP 就会触发风控，这里保守取 1.2 秒
MIN_REQUEST_INTERVAL = 1.2
REQUEST_JITTER = 0.4          # 抖动，避免打点过于规律
MIN_INTERVAL_HARD_FLOOR = 0.5  # 硬下限，命令行也不允许调到比这更快
MAX_RETRY = 3
RETRY_BACKOFF_BASE = 2.0
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 20

# -------------------- 请求预算 --------------------
DEFAULT_MAX_REQUESTS = 60
DEFAULT_MAX_ANALYZE_TRAINS = 8    # 最多深挖几趟无票车次
DEFAULT_MAX_CANDIDATE_STOPS = 6   # 每趟车最多探测几个中途落客站
DEFAULT_MAX_BACK_STOPS = 2        # 买长乘短最多向上游看几站

# -------------------- 缓存 --------------------
DATA_DIR_NAME = "data"
STATION_CACHE_FILE = "stations.json"
STATION_CACHE_TTL_DAYS = 7
ROUTE_CACHE_DIR = "route_cache"
ROUTE_CACHE_TTL_HOURS = 6

# -------------------- 业务规则 --------------------
PRESALE_DAYS = 15                 # 12306 预售期（天）

# 车次公开编码，如 D2928 / G1234 / K528。
# 注意前缀是**可选**的：普速车次号可以纯数字（实测见过 1462 次），
# 所以不能强制要求字母前缀，否则会漏掉整类车次。
PUBLIC_CODE_RE = r"^[GDCZTKYLS]?\d{1,4}[A-Z]?$"

# 内部车次号（train_no）固定 12 位，如 5q000D292810 / 550000146209。
# 光靠正则区分不开它和纯数字车次号，必须靠长度。
INTERNAL_NO_LEN = 12
TIME_RE = r"^\d{2}:\d{2}$"
NO_TIME = "----"                  # 始发站没有到达时间，用这个占位

# -------------------- 余票行字段锚点 --------------------
# 头部：位置稳定，直接按绝对下标取（实测 0~19 全区段一致）
ROW_MIN_LEN = 20
IDX_INTERNAL_TRAIN_NO = 2      # 内部车次号，如 5q000D292810（经停站接口要用它）
IDX_PUBLIC_TRAIN_CODE = 3      # 车次号，如 D2928
IDX_START_STATION_TELECODE = 4
IDX_END_STATION_TELECODE = 5
IDX_FROM_STATION_TELECODE = 6  # 该行对应的上车站（城市展开后会逐站成行）
IDX_TO_STATION_TELECODE = 7
IDX_START_TIME = 8
IDX_ARRIVE_TIME = 9
IDX_LISHI = 10                 # 历时 hh:mm
IDX_CAN_WEB_BUY = 11           # Y/N
IDX_START_TRAIN_DATE = 13
IDX_FROM_STATION_NO = 16       # 该车自己的站序（01 起）
IDX_TO_STATION_NO = 17

# 尾部：一律用 len(行) - N。删字段时头尾同减，倒数是稳定的。
TAIL_PRICE_BLOB = 19           # 价格+余票位图（本项目的主数据源）
TAIL_SEAT_TYPES = 23           # 席别串，与位图互为校验
TAIL_HOUBU_FLAG = 21           # 候补开关：'1' 支持 / '0' 不支持
TAIL_HOUBU_SEAT_LIMIT = 20     # 可候补的席别码串，如 'OM' / '9MO'，空串=无
TAIL_DW_FLAG = 12              # 车次标签串，如 0#1#Q03#0#z#0#z#z
TAIL_COUNTRY_FLAG = 9          # 'CHN,CHN'
TAIL_BED_LEVEL = 6             # 'N#N#'
TAIL_SALE_TIME = 3             # 起售时间，12 位数字
TAIL_LOCAL_FLAG = 2            # 'Y'

# -------------------- 价格位图解码 --------------------
BLOB_GROUP_LEN = 10            # 每 10 个字符描述一个席别
BLOB_PRICE_START = 1           # g[1:6] 是票价 ×10
BLOB_PRICE_END = 6
BLOB_COUNT_START = 6           # g[6:10] 是余票编码
BLOB_COUNT_END = 10
STANDING_OFFSET = 3000         # 余票编码 >= 3000 表示该席别实际是「无座」
COUNT_HAVE_CAP = 21            # 余票 >= 21 时 12306 页面显示「有」，这里照做

# 座别码 -> (中文名, 短名)。短名用于去重和 JSON 键。
SEAT_CODE_MAP = {
    "9": ("商务座", "swz"),
    "P": ("特等座", "tz"),
    "M": ("一等座", "zy"),
    "D": ("优选一等座", "zy"),
    "O": ("二等座", "ze"),
    "S": ("二等包座", "ze"),
    "6": ("高级软卧", "gr"),
    "A": ("高级动卧", "gr"),
    "4": ("软卧", "rw"),
    "I": ("一等卧", "rw"),
    "F": ("动卧", "rw"),
    "3": ("硬卧", "yw"),
    "J": ("二等卧", "yw"),
    "2": ("软座", "rz"),
    "1": ("硬座", "yz"),
    "W": ("无座", "wz"),
    "H": ("其他", "qt"),
}

# dw_flag 里各位置代表的车型标签（判断失败不影响主流程，纯展示）
DW_FLAG_TAGS = {
    0: ("5", "智能动车组"),
    1: ("1", "复兴号"),
    5: ("D", "动感号"),
}
