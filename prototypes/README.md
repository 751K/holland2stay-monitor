# Web 设计原型

本目录保存 Web 改版过程中使用的独立设计原型，供比较布局与交互方案。
这些原型不属于正式 Flask 页面，也不执行真实登录、通知、监控或预订操作。

当前正式页面使用 `templates/inbox.html`、`templates/calendar.html` 与
`templates/map.html`。本地运行：`python tools/dev_web.py --seed-prototype`。

| 目录 | 探索内容 |
|---|---|
| `web-v21` | 第一版工具界面与信息层级 |
| `web-v21-refined` | 保留品牌元素的材质方案 |
| `web-v21-solid` | 不透明表面的视觉方案 |
| `web-v21-type` | 以文字为主的信息排版 |
| `web-list-desktop` | 桌面表格与详情布局 |
| `web-list-inbox` | 收件箱式房源列表 |
| `web-platform-inbox` | 平台分组、个人条件与角色导航 |

`web-platform-inbox/data.js` 包含 21 条公开房源的历史快照，供隔离的本地实例
使用。快照保留各批次的采样时间，不代表实时房源或真实用户配置。
