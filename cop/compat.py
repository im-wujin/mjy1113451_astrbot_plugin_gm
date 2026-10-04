"""兼容层：把散落在 main.py 顶部的惰性 / 多级兜底导入集中到一处。

逻辑与 main.py 原第 8-17 行、第 31-34 行逐字等价，仅做搬运，不改变任何行为。
必须放在模块最外层执行（而非函数内），以保证与重构前完全相同的导入时机。
"""

import threading

# #246：event.send 只接受 MessageChain（AstrBot 4.28 起会直接访问 .chain），
# 若 MessageChain 取不到就退化为传 list，会抛 `'list' object has no attribute 'chain'`。
# 官方导出路径是 astrbot.api.event，需放在最前；后两个仅为老版本兼容兜底。
try:
    from astrbot.api.event import MessageChain
except ImportError:
    try:
        from astrbot.api.message import MessageChain
    except ImportError:
        try:
            from astrbot.api.message_components import MessageChain
        except ImportError:
            MessageChain = None

try:
    import aiohttp
except ImportError:
    aiohttp = None

# 配置写入串行化锁（review#192 race-condition）：多条同步写路径共用，防止交错写坏文件。
# 放在 compat 中作为模块级单例，所有分层模块共享同一把可重入锁（RLock 支持重入）。
_CFG_LOCK = threading.RLock()

__all__ = ["MessageChain", "aiohttp", "_CFG_LOCK"]
