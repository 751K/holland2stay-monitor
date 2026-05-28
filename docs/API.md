# FlatRadar Backend API Reference

本文档整理 FlatRadar 移动端和第三方客户端使用的后端 API。当前稳定接口集中在 `/api/v1/*`，Web 后台的 HTML 页面和 `/api/*` 旧接口不作为移动端契约。

最后更新：2026-05-21

机器可读契约：

- OpenAPI 3.1 JSON：[openapi.json](openapi.json)

## 基础约定

### Base URL

生产环境：

```text
https://flatradar.app/api/v1
```

本地开发：

```text
http://127.0.0.1:8088/api/v1
```

### 响应格式

所有 `/api/v1/*` JSON 接口使用统一响应壳。

成功：

```json
{
  "ok": true,
  "data": {}
}
```

失败：

```json
{
  "ok": false,
  "error": {
    "code": "validation",
    "message": "参数无效"
  }
}
```

稳定错误码：

| HTTP | code | 含义 |
|---:|---|---|
| 400 | `validation` | 请求参数或 JSON body 无效 |
| 401 | `unauthorized` | 未登录、token 无效、token 过期、用户已停用 |
| 403 | `forbidden` | 当前角色无权限 |
| 404 | `not_found` | 资源不存在，或 user 视角不可见 |
| 409 | `conflict` | 资源冲突，例如用户名已存在 |
| 429 | `rate_limited` | 请求过于频繁 |
| 500 | `server_error` | 服务端错误，客户端不应依赖 message 做逻辑 |

### 鉴权

移动端使用 Bearer token：

```http
Authorization: Bearer <token>
```

角色：

| role | 说明 |
|---|---|
| `admin` | 管理员，可查看全量数据和执行管理操作 |
| `user` | 普通用户，房源、通知等接口会按该用户的 `listing_filter` 隔离 |
| guest | 不登录，只能访问公开统计、公开房源、地图、日历、过滤选项等 optional bearer 接口 |

SSE 特例：`GET /notifications/stream` 支持 `Authorization` header，也支持 `?token=<token>`，因为部分 EventSource 客户端不能设置自定义 header。

### 时间与时区

接口中的时间字符串主要来自服务端 SQLite，通常是 ISO-like 字符串。移动端展示时应按以下规则：

- 服务端业务时间默认按 Amsterdam 时间理解。
- 如果设备时区不是 Amsterdam，展示时应补充时区标识。
- 客户端不应把空字符串时间解析为当前时间。

## 端点总览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/auth/login` | 无 | 登录并签发 Bearer token |
| POST | `/auth/register` | 无 | 自助注册，注册即登录 |
| POST | `/auth/logout` | admin/user | 撤销当前 token |
| GET | `/auth/me` | admin/user | 当前身份摘要 |
| POST | `/auth/password` | user | 修改当前用户密码 |
| GET | `/stats/public/summary` | 可选 | 公开统计概览 |
| GET | `/stats/public/charts` | 可选 | 图表 key 列表 |
| GET | `/stats/public/charts/<key>` | 可选 | 图表数据 |
| GET | `/listings` | 可选 | 房源列表 |
| GET | `/listings/<id>` | 可选 | 房源详情 |
| GET | `/map` | 可选 | 已缓存坐标的地图房源 |
| GET | `/calendar` | 可选 | 有入住日期的日历房源 |
| GET | `/notifications` | admin/user | 通知列表 |
| POST | `/notifications/read` | admin/user | 标记通知已读 |
| GET | `/notifications/stream` | admin/user | SSE 通知流 |
| GET | `/me/summary` | admin/user | 当前用户统计 |
| GET | `/me/filter` | admin/user | 当前用户过滤条件 |
| PUT | `/me/filter` | user | 更新当前用户过滤条件 |
| DELETE | `/me` | user | 注销当前账号 |
| GET | `/me/export` | user | 导出当前用户数据 |
| GET | `/filter/options` | 可选 | 过滤器候选值 |
| POST | `/devices/register` | admin/user | 注册/刷新设备推送 token |
| GET | `/devices` | admin/user | 当前 session 下设备列表 |
| DELETE | `/devices/<id>` | admin/user | 删除当前 session 下设备 |
| POST | `/devices/test` | admin/user | 测试推送 |
| POST | `/feedback` | admin/user | 提交反馈 |
| POST | `/diagnostics/crash` | 可选 | 上传崩溃/性能诊断 |
| GET | `/admin/users` | admin | 用户列表 |
| POST | `/admin/users/<id>/toggle` | admin | 启用/停用用户 |
| DELETE | `/admin/users/<id>` | admin | 删除用户 |
| GET | `/admin/monitor/status` | admin | 监控进程状态 |
| POST | `/admin/monitor/start` | admin | 启动监控 |
| POST | `/admin/monitor/stop` | admin | 停止监控 |
| POST | `/admin/monitor/reload` | admin | 热重载监控配置 |

## Auth

### POST `/auth/login`

登录并签发 Bearer token。

Body：

```json
{
  "username": "Alice",
  "password": "secret",
  "device_name": "Pixel 9",
  "ttl_days": 90
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `username` | string | 是 | 普通用户名；管理员固定为 `__admin__` |
| `password` | string | 是 | 账号密码 |
| `device_name` | string | 否 | 登录设备名，最长 64 字符 |
| `ttl_days` | int | 否 | token 有效期，1-90 天；默认 90 |

成功：

```json
{
  "ok": true,
  "data": {
    "token": "plain-token",
    "token_id": 123,
    "role": "user",
    "user_id": "usr_xxx",
    "device_name": "Pixel 9",
    "ttl_days": 90
  }
}
```

注意：

- `__admin__` 使用 Web 后台密码。
- 普通 user 默认使用本地 app password。
- H2S 凭据 fallback 默认关闭，只有用户显式允许时才尝试。
- 登录失败计入 IP 限流。

### POST `/auth/register`

自助注册普通用户，并立即签发 token。

Body：

```json
{
  "username": "Alice",
  "password": "secret",
  "device_name": "Pixel 9",
  "ttl_days": 90
}
```

校验：

- `username` 至少 2 字符，最长 64 字符。
- `password` 至少 4 字符。
- `username` 不能是 `__admin__`。
- 同 IP 注册限流：每小时最多 3 次。

成功状态码：`201`

返回：

```json
{
  "ok": true,
  "data": {
    "token": "plain-token",
    "token_id": 123,
    "role": "user",
    "user_id": "usr_xxx",
    "device_name": "Pixel 9",
    "ttl_days": 90,
    "user": {
      "id": "usr_xxx",
      "name": "Alice",
      "enabled": true,
      "notifications_enabled": false,
      "listing_filter": {}
    }
  }
}
```

### POST `/auth/logout`

撤销当前 Bearer token。

鉴权：`admin` / `user`

返回：

```json
{
  "ok": true,
  "data": {
    "revoked": true
  }
}
```

### GET `/auth/me`

获取当前身份。

鉴权：`admin` / `user`

返回：

```json
{
  "ok": true,
  "data": {
    "role": "user",
    "user_id": "usr_xxx",
    "user": {
      "id": "usr_xxx",
      "name": "Alice",
      "enabled": true,
      "notifications_enabled": true,
      "listing_filter": {}
    }
  }
}
```

admin 的 `user` 为 `null`。

### POST `/auth/password`

修改当前 user 密码。

鉴权：`user`

Body：

```json
{
  "current_password": "old",
  "new_password": "new-secret"
}
```

返回：

```json
{
  "ok": true,
  "data": {
    "revoked_other_sessions": 2
  }
}
```

说明：

- admin 密码不通过此接口修改。
- 修改成功后保留当前 token，撤销同一 user 的其他 token。

## Public Stats

### GET `/stats/public/summary`

公开统计概览。guest 可访问。

返回：

```json
{
  "ok": true,
  "data": {
    "total": 199,
    "new_24h": 0,
    "new_7d": 24,
    "changes_24h": 1,
    "last_scrape": "2026-05-20T11:09:58"
  }
}
```

### GET `/stats/public/charts`

返回可用图表 key。

```json
{
  "ok": true,
  "data": {
    "charts": [
      "area_dist",
      "city_dist",
      "contract_dist",
      "daily_changes",
      "daily_new",
      "energy_dist",
      "floor_dist",
      "hourly_dist",
      "price_dist",
      "status_dist",
      "tenant_dist",
      "type_dist"
    ]
  }
}
```

### GET `/stats/public/charts/<key>`

查询图表数据。

Query：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---:|---|
| `days` | int | 30 | 1-365 |

返回：

```json
{
  "ok": true,
  "data": {
    "key": "type_dist",
    "days": 30,
    "data": []
  }
}
```

客户端应把 `data` 当作图表专用结构，不同 key 的 shape 可能不同。

## Listings

### GET `/listings`

分页房源列表。admin 返回全量；user 自动应用自己的 `listing_filter`；guest 返回公开全量。

Query：

| 参数 | 类型 | 说明 |
|---|---|---|
| `status` | string | 精确匹配状态，例如 `Available to book` |
| `city` | string | 单城市过滤，旧兼容参数 |
| `cities` | string | 多城市逗号分隔，例如 `Eindhoven,Amsterdam` |
| `q` | string | 名称搜索 |
| `types` | string | 房型逗号分隔，例如 `Studio,Apartment` |
| `contract` | string | 合同类型子串匹配 |
| `energy` | string | 最低能耗等级，例如 `B` |
| `limit` | int | 1-500，默认 100 |
| `offset` | int | 默认 0 |

返回：

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "id": "listing-id",
        "name": "Victoriapark 875",
        "status": "Reserved",
        "price_raw": "€2,088",
        "price_value": 2088.0,
        "available_from": "2026-06-22",
        "city": "Eindhoven",
        "url": "https://...",
        "features": ["Type: Apartment"],
        "feature_map": {"Type": "Apartment"},
        "first_seen": "2026-05-14T10:00:00",
        "last_seen": "2026-05-20T11:09:58"
      }
    ],
    "total": 1,
    "limit": 100,
    "offset": 0,
    "filtered": true
  }
}
```

### GET `/listings/<id>`

单条房源详情。

权限行为：

- admin / guest：房源存在则返回。
- user：如果该房源不通过当前用户的 `listing_filter`，返回 404。

返回字段同 `/listings` 的 item。

## Map

### GET `/map`

返回已缓存坐标的地图房源。不会触发外部 geocode 请求。

鉴权：可选

返回：

```json
{
  "ok": true,
  "data": {
    "listings": [
      {
        "id": "listing-id",
        "name": "Victoriapark 875",
        "status": "Reserved",
        "price_raw": "€2,088",
        "available_from": "2026-06-22",
        "city": "Eindhoven",
        "address": "Victoriapark 875, Eindhoven",
        "lat": 51.4416,
        "lng": 5.4697
      }
    ],
    "uncached": 3
  }
}
```

说明：

- `uncached` 表示有多少房源没有坐标缓存。
- user 会按 `listing_filter` 过滤。
- guest 可访问，但只读取缓存，不产生外部请求或写入。

## Calendar

### GET `/calendar`

返回有 `available_from` 的房源，用于日历视图。

鉴权：可选

返回：

```json
{
  "ok": true,
  "data": {
    "listings": [
      {
        "id": "listing-id",
        "name": "Victoriapark 875",
        "status": "Reserved",
        "price_raw": "€2,088",
        "available_from": "2026-06-22",
        "url": "https://...",
        "city": "Eindhoven",
        "building": "Onyx"
      }
    ]
  }
}
```

## Notifications

### GET `/notifications`

分页通知列表。

鉴权：`admin` / `user`

Query：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---:|---|
| `limit` | int | 50 | 1-200 |
| `offset` | int | 0 | 分页偏移 |

返回：

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "id": 1001,
        "created_at": "2026-05-20T11:09:58Z",
        "type": "new_listing",
        "title": "New listing",
        "body": "Victoriapark 875",
        "url": "",
        "listing_id": "listing-id",
        "read": 0,
        "user_id": "usr_xxx"
      }
    ],
    "total": 1,
    "unread": 1,
    "limit": 50,
    "offset": 0
  }
}
```

user 视角：

- 只返回自己可见的通知。
- 只保留 `new_listing`、`status_change`、`booking` 类型。
- 带 `listing_id` 的通知会再次按 `listing_filter` 过滤。

### POST `/notifications/read`

标记通知已读。

鉴权：`admin` / `user`

Body：

```json
{
  "ids": [1001, 1002]
}
```

或传 `{}` / 不传 `ids` 表示全部已读。

返回：

```json
{
  "ok": true,
  "data": {
    "marked": true
  }
}
```

### GET `/notifications/stream`

SSE 增量通知流。

鉴权：Bearer header 或 `?token=...`

Query：

| 参数 | 类型 | 说明 |
|---|---|---|
| `token` | string | 可选；无法设置 Authorization header 时使用 |
| `last_id` | int | 从该通知 id 之后开始增量推送 |

事件格式：

```text
retry: 2000

data: [{"id":1002,"type":"new_listing"}]

: keepalive
```

客户端建议：

- 连接最长约 5 分钟，服务端会断开；客户端必须自动重连。
- 收到 `data` 后用最后一条通知的 `id` 更新 `last_id`。
- 断线重连建议指数退避，最大 30 秒。

## Current User

### GET `/me/summary`

当前用户统计。admin 返回全库口径；user 返回匹配当前 `listing_filter` 的口径。

鉴权：`admin` / `user`

返回：

```json
{
  "ok": true,
  "data": {
    "role": "user",
    "total_in_db": 199,
    "new_24h_total": 0,
    "matched_total": 54,
    "matched_available": 12,
    "last_scrape": "2026-05-20T11:09:58",
    "filter_active": true
  }
}
```

### GET `/me/filter`

读取当前过滤条件。

鉴权：`admin` / `user`

返回：

```json
{
  "ok": true,
  "data": {
    "role": "user",
    "filter": {
      "max_rent": 1200,
      "min_area": 25,
      "min_floor": 0,
      "allowed_cities": ["Eindhoven"],
      "allowed_types": ["Studio"],
      "allowed_energy": "B"
    },
    "is_empty": false
  }
}
```

### PUT `/me/filter`

覆盖当前 user 的过滤条件。

鉴权：`user`

Body 支持字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `max_rent` | number | 最大租金，正数 |
| `min_area` | number | 最小面积，正数 |
| `min_floor` | int | 最低楼层，0-200 |
| `allowed_cities` | string[] | 城市 |
| `allowed_types` | string[] | 房型 |
| `allowed_neighborhoods` | string[] | 区域 |
| `allowed_contract` | string[] | 合同类型 |
| `allowed_tenant` | string[] | 租客类型 |
| `allowed_offer` | string[] | offer 类型 |
| `allowed_finishing` | string[] | 装修类型 |
| `allowed_occupancy` | string[] | 入住人数 |
| `allowed_energy` | string | 能耗等级，必须在服务端白名单内 |

示例：

```json
{
  "max_rent": 1200,
  "min_area": 25,
  "allowed_cities": ["Eindhoven"],
  "allowed_energy": "B"
}
```

返回同 `GET /me/filter`。

注意：这是完整覆盖式更新，缺省字段会回到默认值，不会保留旧值。

### DELETE `/me`

注销当前用户账号。

鉴权：`user`

效果：

- 删除 SQLite 用户配置。
- 撤销该用户所有 App token。

返回：

```json
{
  "ok": true,
  "data": {
    "deleted": true,
    "user_id": "usr_xxx"
  }
}
```

### GET `/me/export`

导出当前用户数据。

鉴权：`user`

返回包括：

- account
- filter
- notification_history，最多 500 条
- active_devices
- active_tokens
- exported_at

## Filter Options

### GET `/filter/options`

返回过滤器候选值。guest 可访问。

```json
{
  "ok": true,
  "data": {
    "cities": ["Eindhoven"],
    "occupancy": ["One", "Two (only couples)"],
    "types": ["Studio"],
    "neighborhoods": ["Vlootkwartier"],
    "contract": ["Regular"],
    "tenant": ["Student"],
    "offer": ["Lottery"],
    "finishing": ["Furnished"],
    "energy": ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F"]
  }
}
```

## Devices and Push

### 当前状态

后端数据库已经有 `platform` 字段，但当前生产推送链路仍以 APNs 为主：

- iOS：可用。
- Android：注册字段已预留，但 FCM sender 尚未完成前，不应把 Android token 接入正式推送链路。

Android 开发前应先完成：

- `/devices/register` 对 `platform` 做白名单：`ios` / `android`。
- APNs 和 FCM 按 platform 分流。
- `/devices/test` 支持 Android FCM 测试。

### POST `/devices/register`

注册或刷新当前 session 的设备 token。

鉴权：`admin` / `user`

Body：

```json
{
  "device_token": "push-token",
  "env": "production",
  "platform": "ios",
  "model": "iPhone 17 Pro",
  "bundle_id": "com.j.kong.FlatRadar"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `device_token` | string | 是 | APNs token；未来 Android 为 FCM token |
| `env` | string | 否 | `production` / `sandbox`，默认 `production` |
| `platform` | string | 否 | 当前默认 `ios`；Android 应传 `android` |
| `model` | string | 否 | 设备型号，最长 64 |
| `bundle_id` | string | 否 | App bundle/package id，最长 128 |

返回：

```json
{
  "ok": true,
  "data": {
    "device_id": 1,
    "env": "production",
    "platform": "ios"
  }
}
```

### GET `/devices`

列出当前 token/session 下的设备。

鉴权：`admin` / `user`

返回：

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "id": 1,
        "device_token_hint": "abcdef123456…7890",
        "env": "production",
        "platform": "ios",
        "model": "iPhone 17 Pro",
        "created_at": "2026-05-17T10:00:00Z",
        "last_seen": "2026-05-21T10:00:00Z",
        "disabled": false,
        "disabled_reason": ""
      }
    ]
  }
}
```

### DELETE `/devices/<id>`

删除当前 session 下的设备。只能删除当前 Bearer token 绑定的设备；越权返回 404。

返回：

```json
{
  "ok": true,
  "data": {
    "deleted": true
  }
}
```

### POST `/devices/test`

测试通知链路。

鉴权：`admin` / `user`

Body：

```json
{
  "title": "测试推送",
  "body": "如果你看到这条，推送链路工作正常",
  "apns_only": false,
  "notification_only": false
}
```

返回：

```json
{
  "ok": true,
  "data": {
    "sent": 1,
    "total": 1,
    "results": [
      {
        "device_token_hint": "abcdef123456…7890",
        "env": "production",
        "status": 200,
        "reason": "",
        "ok": true
      }
    ],
    "notification_id": 1001
  }
}
```

当前实现说明：

- 写入 `web_notifications` 可验证 SSE / App alerts。
- APNs 测试仅适用于 iOS。
- Android FCM 支持完成前，Android 客户端不应依赖该接口验证 FCM。

## Feedback

### POST `/feedback`

提交用户反馈。

鉴权：`admin` / `user`

Body：

```json
{
  "kind": "bug",
  "message": "Map page freezes on first open.",
  "user_name": "Alice",
  "app_version": "1.6.0"
}
```

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | string | `bug` / `suggestion` / `other` |
| `message` | string | 5-2000 字符 |
| `user_name` | string | 可选 |
| `app_version` | string | 可选 |

返回：

```json
{
  "ok": true,
  "data": {
    "submitted": true
  }
}
```

## Diagnostics

### POST `/diagnostics/crash`

上传崩溃或性能诊断。guest 可上传。

鉴权：可选

限制：

- 单 IP 每小时最多 20 条。
- body 最大 256 KB。

Body：

```json
{
  "kind": "crash",
  "app_version": "1.6.0",
  "ios_version": "26.0",
  "device_model": "iPhone 17 Pro",
  "payload": {}
}
```

`kind` 可选：

- `crash`
- `hang`
- `diskwrite`
- `cpuexception`

返回状态码：`202`

```json
{
  "ok": true,
  "data": {
    "received": true,
    "id": "20260521T100000Z-crash-abcd1234.json"
  }
}
```

Android 客户端可复用此端点，但建议字段改为：

```json
{
  "kind": "crash",
  "app_version": "1.0.0",
  "device_model": "Pixel 9",
  "payload": {}
}
```

当前后端字段名仍有 `ios_version` 历史命名，后续可扩展 `os_version`。

## Admin

### GET `/admin/users`

列出用户摘要。

鉴权：`admin`

返回：

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "id": "usr_xxx",
        "name": "Alice",
        "enabled": true,
        "notifications_enabled": true,
        "channel_count": 1,
        "channels": ["email"],
        "app_login_enabled": true,
        "has_app_password": true,
        "allow_h2s_login": false,
        "active_devices": 2,
        "auto_book_enabled": false,
        "filter_summary": {
          "max_rent": 1200,
          "min_area": 25,
          "min_floor": 0,
          "cities": ["Eindhoven"],
          "energy": "B",
          "filter_active": true
        }
      }
    ],
    "total": 1
  }
}
```

### POST `/admin/users/<id>/toggle`

启用或停用用户。

鉴权：`admin`

返回：

```json
{
  "ok": true,
  "data": {
    "id": "usr_xxx",
    "enabled": false
  }
}
```

### DELETE `/admin/users/<id>`

删除用户并撤销其 App token。

鉴权：`admin`

返回：

```json
{
  "ok": true,
  "data": {
    "deleted": true,
    "name": "Alice",
    "revoked_sessions": 2
  }
}
```

### GET `/admin/monitor/status`

返回监控进程状态。

鉴权：`admin`

```json
{
  "ok": true,
  "data": {
    "running": true,
    "pid": 12345,
    "last_scrape": "2026-05-20T11:09:58",
    "last_count": "11"
  }
}
```

### POST `/admin/monitor/start`

启动 monitor。

鉴权：`admin`

返回：

```json
{
  "ok": true,
  "data": {
    "started": true,
    "method": "supervisor"
  }
}
```

如果已经运行，返回 `400 validation`。

### POST `/admin/monitor/stop`

停止 monitor。

鉴权：`admin`

返回：

```json
{
  "ok": true,
  "data": {
    "stopped": true,
    "pid": 12345,
    "method": "supervisor"
  }
}
```

如果未运行，返回 `400 validation`。

### POST `/admin/monitor/reload`

热重载 monitor 配置。

鉴权：`admin`

返回：

```json
{
  "ok": true,
  "data": {
    "reload": true,
    "method": "signal"
  }
}
```

## Webhooks

FlatRadar 通过 webhook 接收外部服务回调。Webhook 端点不走 Bearer token 鉴权，而是通过 Svix HMAC-SHA256 签名验证请求来源。

### POST `/api/inbound/email`

接收 Resend `email.received` webhook 回调。当 `notify@flatradar.app` 收到入站邮件时，Resend 向该端点推送事件元数据。

鉴权：Svix HMAC-SHA256 签名（无 session）

**请求头：**

| 头 | 说明 |
|---|---|
| `svix-id` | 事件唯一 ID |
| `svix-timestamp` | Unix 秒时间戳 |
| `svix-signature` | `v1,<base64>` 形式，可空格分隔多个轮换签名 |

**签名验证：**

1. 用 `RESEND_WEBHOOK_SECRET`（`whsec_` 前缀的 base64 串）解码 HMAC 密钥。
2. 验证时间戳在 ±5 分钟容忍窗口内（防重放攻击）。
3. 计算 `HMAC-SHA256(secret, "svix-id.svix-timestamp.raw_body")`，与 `svix-signature` 中的任一 `v1,xxx` 比对。
4. 所有比较使用 `hmac.compare_digest` 防时序攻击。

**Body：**

```json
{
  "type": "email.received",
  "created_at": "2026-02-22T23:41:12.126Z",
  "data": {
    "email_id": "56761188-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "from": "user@example.com",
    "to": ["notify@flatradar.app"],
    "subject": "关于某房源的咨询",
    "attachments": [
      {
        "id": "att_xxx",
        "filename": "photo.png",
        "content_type": "image/png"
      }
    ]
  }
}
```

**处理流程：**

1. 验证 Svix 签名，失败返回 `401`。
2. 忽略非 `email.received` 事件类型。
3. 用 `email_id` 反查 Resend API 拉取完整邮件正文（text/html）。
4. DMARC 报告自动识别为 `inbound_dmarc` 类型，其余为 `inbound_email`。
5. 写入 admin 通知面板（`web_notifications` 表）。
6. 非 DMARC 邮件转发到 `INBOUND_FORWARD_TO` 配置的管理员邮箱。

**成功响应：**

```json
{
  "ok": true,
  "email_id": "56761188-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

**错误响应：**

| HTTP | 说明 |
|---:|---|
| 401 | 签名验证失败（secret 未配置 / 时间戳过期 / HMAC 不匹配） |
| 503 | 服务端未配置 `RESEND_WEBHOOK_SECRET` |

**环境变量：**

| 变量 | 必填 | 说明 |
|---|---|---|
| `RESEND_WEBHOOK_SECRET` | 是 | Svix webhook 签名密钥，形如 `whsec_<base64>` |
| `RESEND_API_KEY` | 是 | 反查完整邮件和转发邮件使用 |
| `RESEND_FROM` | 否 | 转发邮件时的发件地址 |
| `INBOUND_FORWARD_TO` | 否 | 管理员个人邮箱，用于转发入站邮件 |

**Resend 配置建议：**

在 Resend Dashboard → Webhooks 中添加 endpoint `https://<domain>/api/inbound/email`，订阅 `email.received` 事件。Resend 会自动重试失败的投递。

## 移动端实现建议

### Token 存储

- iOS：Keychain。
- Android：EncryptedSharedPreferences 或 Jetpack Security。
- 不要把 Bearer token 写入普通日志、crash payload 或 analytics。

### Guest 模式

guest 不调用 `/auth/login`。客户端本地标记 guest，然后访问 optional bearer 接口：

- `/stats/public/summary`
- `/stats/public/charts`
- `/stats/public/charts/<key>`
- `/listings`
- `/listings/<id>`
- `/map`
- `/calendar`
- `/filter/options`

guest 不能访问：

- `/notifications`
- `/devices/*`
- `/me/*`
- `/admin/*`
- `/feedback`

### Android 首版接口优先级

建议 Android MVP 先接入：

1. `/auth/login`
2. `/auth/register`
3. `/auth/me`
4. `/stats/public/summary`
5. `/stats/public/charts/<key>`
6. `/listings`
7. `/listings/<id>`
8. `/me/summary`
9. `/me/filter`
10. `/filter/options`
11. `/notifications`
12. `/notifications/read`

推送相关在后端 FCM 完成后再接：

1. `/devices/register`
2. `/devices`
3. `/devices/test`

### Android 后端待办

在 Android 客户端正式启用 FCM 前，需要完成以下后端改动：

- 新增 `notifier_channels/fcm.py`。
- 修改 push dispatcher，按 `device_tokens.platform` 分发：
  - `ios` → APNs
  - `android` → FCM
- `/devices/register` 增加 platform 白名单。
- `/devices/test` 根据 platform 调用 APNs 或 FCM。
- tests：
  - device register 支持 `platform=android`
  - APNs sender 不处理 Android token
  - FCM sender 不处理 iOS token
  - test push 返回 platform-specific results

## 兼容性说明

当前 API 的人读说明以本文档为准；Android/iOS 共用的机器可读契约以 [openapi.json](openapi.json) 为准。修改规则：

- 新字段只增不删。
- 删除字段前至少保留一个 minor 版本。
- 错误码 `code` 保持稳定。
- 客户端不要依赖 `message` 做控制流。
- Android/iOS 客户端应忽略未知字段。
- 新增、删除、重命名 `/api/v1/*` 端点时，必须同步更新 `docs/openapi.json` 和 `tests/test_openapi_contract.py`。
