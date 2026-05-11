"""
app.routes — 按功能切分的路由模块

设计要点
--------
每个模块用 ``app.add_url_rule(rule, endpoint=, view_func=, methods=)``
直接挂到 Flask app，而不是用 ``flask.Blueprint``：

- Flask Blueprint 会强制把 endpoint 前缀化为 ``<bp_name>.<endpoint>``
- 模板里有 17 处 ``url_for("index")`` 等扁平名调用，前端 fetch 也硬编码 URL
- 用 add_url_rule 可保留扁平 endpoint 名，模板和前端零改动（A 方案）

每个模块导出一个 ``register(app)`` 函数；``web.py`` 在 app 构建完成后
依次调用所有 register。
"""
