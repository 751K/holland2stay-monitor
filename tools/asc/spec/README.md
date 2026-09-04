# App Store Connect API 的官方 OpenAPI 规格

`app-store-connect-openapi.json` —— Apple 自己发布的完整规格。
OpenAPI 3.0.1，966 个端点、1393 个 schema。

## 为什么存一份在仓库里

网页版文档**不写枚举取值**。`CiArtifact.fileType` 在文档页上只有一句
"A string that describes the type of the artifact"，而规格里是明确列出来的：

    ARCHIVE, ARCHIVE_EXPORT, LOG_BUNDLE, RESULT_BUNDLE,
    TEST_PRODUCTS, XCODEBUILD_PRODUCTS, STAPLED_NOTARIZED_ARCHIVE

2026-09-04 配 Xcode Cloud 的截图 workflow 时，因为没查这份规格，好几个字段是拿
API 的错误信息一次次试出来的：

| 字段 | 试错次数 | 正确值 |
|---|---|---|
| `runtimeIdentifier` | 3 | `"default"` |
| 手动触发 | 2 | `manualBranchStartCondition`（不是 `isManualStart`） |
| `testDestinations` | 4 | 需要 deviceTypeIdentifier + deviceTypeName + runtime* + kind |

每一次试错都是在**改生产 CI 配置**。规格里全都写着。

## 怎么用

```bash
# 某个 schema 的全部字段与枚举
python3 -c "
import json; d=json.load(open('tools/asc/spec/app-store-connect-openapi.json'))
s=d['components']['schemas']['CiArtifact']
print(json.dumps(s, ensure_ascii=False, indent=2))"

# 找端点
python3 -c "
import json; d=json.load(open('tools/asc/spec/app-store-connect-openapi.json'))
for p in d['paths']:
    if 'ciBuildRuns' in p: print(p)"
```

## 更新

    curl -sL -o spec.zip \
      https://developer.apple.com/sample-code/app-store-connect/app-store-connect-openapi-specification.zip
    unzip -o spec.zip

Apple 不给它单独的版本页，`info.version` 是唯一的版本标识（当前 4.4.1）。
社区有个仓库在自动追踪每次变更，需要 diff 历史时可以看：
<https://github.com/EvanBacon/App-Store-Connect-OpenAPI-Spec>
