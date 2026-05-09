"""
app/ — Web 面板的内聚子模块包

本阶段（Stage 1）只把 web.py 中无 Flask 耦合或弱耦合的纯函数抽出，
不改变路由、endpoint、URL，也不引入 Blueprint。create_app() 工厂留待
Stage 5 再统一收口。
"""
