"""平台相关的启动设置。

langchain-postgres 会在后台线程里新建事件循环来调用 psycopg 的异步接口，
而 Windows 默认的 Proactor 循环不被 psycopg 支持，必须在事件循环创建之前切换成 Selector。
Linux 上这段是空操作，生产部署不受影响。
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
