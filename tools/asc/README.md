# ASC Metadata CLI

App Store Connect 文字元数据自动化工具：用 REST API 读 / 改 / 提交 description、
keywords、what's new、subtitle、name 等字段，避免 ASC 网页里逐字段点击。

## 一次性配置（10 分钟）

### 1. 申请 API Key

App Store Connect → **Users and Access → Integrations → App Store Connect API**
→ **Generate API Key**

- **Name**：`flatradar-metadata`（自己看的）
- **Access**：选 **App Manager**（够用且最小特权）

点 **Generate** → 立即下载 `.p8` 文件（**只有这一次下载机会**）。

记下两个值：

- **Key ID**：列表里那行的 10 字符
- **Issuer ID**：页面顶部的 UUID

### 2. 放好私钥 + 写 config.json

```bash
mkdir -p ~/.config/asc
mv ~/Downloads/AuthKey_*.p8 ~/.config/asc/
chmod 600 ~/.config/asc/AuthKey_*.p8

# 拿到下载文件名后填进去
KEY_FILE=$(ls ~/.config/asc/AuthKey_*.p8)
KEY_ID=$(basename "$KEY_FILE" | sed 's/AuthKey_\(.*\)\.p8/\1/')

cat > ~/.config/asc/config.json <<EOF
{
  "key_id":    "$KEY_ID",
  "issuer_id": "粘贴你的 Issuer ID（UUID 格式）",
  "p8_path":   "$KEY_FILE",
  "bundle_id": "你的 bundle id，如 app.flatradar.ios"
}
EOF

# 验证
chmod 600 ~/.config/asc/config.json
python tools/asc/asc_api.py status
```

跑通 `status` 看到你的 app 信息即说明配置正确。

### 3. .gitignore 排除（默认就在用户目录外，但加一层保险）

```gitignore
# 永远不提交
**/AuthKey_*.p8
~/.config/asc/
```

---

## 日常使用（命令行）

### 看现状

```bash
python tools/asc/asc_api.py status                  # app + 可编辑版本 + 所有 localization 总览
python tools/asc/asc_api.py show                    # 所有 localization 的全部文本
python tools/asc/asc_api.py show --lang zh-Hans     # 只看中文
```

### 新增 localization

```bash
python tools/asc/asc_api.py add-locale zh-Hans
python tools/asc/asc_api.py add-locale zh-Hant
python tools/asc/asc_api.py add-locale nl
```

### 改字段

短文案直接命令行：

```bash
python tools/asc/asc_api.py set --lang zh-Hans --field subtitle \
  --value "荷兰 H2S 房源实时监控"
```

长文案从文件读（避免 shell 转义）：

```bash
python tools/asc/asc_api.py set --lang zh-Hans --field description \
  --file metadata/zh-Hans-description.md

python tools/asc/asc_api.py set --lang en-US --field whatsNew \
  --file metadata/release_notes/1.5.1/en-US.txt
```

会展示 before/after diff，**输入 `y` 才真发**。要批处理（自己跑过的脚本）加
`--yes` 跳过确认。

### 提交"仅元数据"审核

```bash
python tools/asc/asc_api.py submit-metadata-only --yes
```

不上传新 binary，只把当前可编辑版本的所有 metadata 改动送审。通常 < 24h 通过。

### Dry-run（看请求但不发）

```bash
python tools/asc/asc_api.py --dry-run set --lang zh-Hans --field description --file x.md
```

---

## 与 Claude skill 配合

`~/.claude/skills/asc-metadata/SKILL.md` 让你直接对 Claude 说人话：

> "改一下 zh-Hans 描述，强调速度 + 隐私 + 自动通知，加上免费打赏说明"
> "看一下当前中文 keywords，按 H2S 搜索量重新排一下"
> "提交本次中英文元数据修改给苹果审核"

Claude 会调用 `asc_api.py`，**永远先 show 再 diff 再 confirm**，安全边界由 skill
instructions 自身保证。

---

## 字段速查

| Field             | 上限 | Layer    | 说明 |
|-------------------|------|----------|------|
| name              | 30   | appInfo  | App Store 上显示的名字 |
| subtitle          | 30   | appInfo  | 副标题，**搜索权重高** |
| privacyPolicyUrl  | 255  | appInfo  | 隐私政策链接 |
| description       | 4000 | version  | 主描述 |
| keywords          | 100  | version  | 逗号分隔，**总长 100 字符** |
| whatsNew          | 4000 | version  | 本次更新（每发版改）|
| promotionalText   | 170  | version  | 顶部短宣，**不需审核可改** |
| marketingUrl      | 255  | version  | 营销页 URL |
| supportUrl        | 255  | version  | 客服 URL |

**appInfo** = app 级别（跨版本，改了立即生效需提交一次 appInfo 审核）
**version** = 版本级别（跟当前可编辑版本绑定）

中文字符按 1 char 算。`keywords` 100 char 用中文能塞 30+ 词。

---

## 字段编辑前提

| 字段层 | 何时可改 |
|---|---|
| version-level | 必须当前有"可编辑版本"（PREPARE_FOR_SUBMISSION / REJECTED / METADATA_REJECTED 等）。如果 app 已 READY_FOR_SALE 且无新版本，需先在 ASC 网页创建新版本 |
| appInfo-level | 几乎随时可改，但需要"提交 appInfo 审核"才生效 |

---

## 轮换 API Key

如果怀疑私钥泄漏：

1. ASC → API 列表 → 旧 key 旁 **Revoke**
2. 生成新 key（步骤同上）
3. 把新 `.p8` 放进 `~/.config/asc/`，删旧 `.p8`
4. 改 `config.json` 的 `key_id` 和 `p8_path`
5. `python tools/asc/asc_api.py status` 验证

---

## 不支持的事（明确边界）

- **上传 binary** → 用 Xcode Archive 或 `xcrun altool`
- **截图** → ASC API 支持但矩阵复杂（device class × locale），fastlane 更合适
- **IAP / 订阅** → ASC 网页或 fastlane
- **Privacy Nutrition Label** → ASC API 支持但 schema 庞大，目前未实现
- **价格 / 上下架** → ASC 网页

---

## Troubleshooting

| 现象 | 排查 |
|---|---|
| `配置文件不存在` | `~/.config/asc/config.json` 没建，按步骤 2 来 |
| `未找到 app` | `config.json` 里 `bundle_id` 跟 ASC 不一致 |
| `没有可编辑版本` | 当前没有 in-progress 版本，去 ASC 网页 New Version |
| HTTP 401 / JWT 过期 | 私钥被 revoke 了，或 key_id / issuer_id 写错 |
| HTTP 403 forbidden | API Key 权限不够，需要 App Manager 或 Admin |
| HTTP 409 conflict | 版本当前在 WAITING_FOR_REVIEW，需要从审核队列撤下来再改 |
| HTTP 422 / 字段超长 | 看错误 detail，本地 client 已有上限预检但偶尔有遗漏 |
