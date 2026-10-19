"""mcore — 从 monitor.py 抽出的核心逻辑与小服务。

包本身不做任何 eager import：``config`` 会在模块初始化期间导入
``mcore.proxy``，如果这里先导入 booking / scrapers，会形成循环。
需要子模块时直接 ``from mcore.xxx import ...``。

模块
----
interval / pacing / health / circuit / backoff / watchdog / prewarm /
booking / push / proxy / sources / state / telemetry / stale / dispatch /
scrape_round / prewarm_round / post_round / round_failures / startup /
outage / throttles / h2s_guard / h2s_rest / geocode / warm_browser
"""
