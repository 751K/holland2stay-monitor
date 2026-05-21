# Android 客户端开发方案

## 1. 背景

FlatRadar iOS 客户端已于 v1.6.0 上架 App Store。国际学生 / young professional 群体里 Android 占比 ~40-50%，需要补齐 Android 端才能覆盖全量用户。

### 现状盘点

| 维度 | 现状 |
|---|---|
| iOS 代码量 | ~50 个 Swift 文件，~5000 行（不含测试） |
| 架构模式 | SwiftUI + @Observable + MVVM，Stores 层承载所有业务逻辑 |
| 后端 API | Flask `/api/v1/*`，统一的 `{ok, data, error}` 信封，Bearer token 鉴权 |
| 后端推送 | APNs 已跑通；FCM 通道尚未开发 |
| 设计系统 | iOS HIG 原生组件，Material 3 需要重新映射 |

完整后端接口契约见：[Backend API Reference](API.md)；Android/iOS 共用的机器可读 OpenAPI 3.1 契约见：[openapi.json](openapi.json)。Android 开工前应先按该文档确认 `/devices/register`、`/devices/test` 和推送分发的 Android/FCM 待办。

后端改动量小——FCM 推送通道 + `platform` 字段扩展，大约 300 行 Python。

---

## 2. 技术选型：Kotlin + Jetpack Compose 原生

### 2.1 决策理由

Android 开发当前有 4 条路线：

| 路线 | 与 iOS 的关系 | 结论 |
|---|---|---|
| **Kotlin + Compose** | 两套 UI 代码，但声明式范式与 SwiftUI 一致 | **选择** |
| KMP + Compose Multiplatform | 共享 Stores/Networking/Models | 暂不选——iOS SwiftUI 已稳定，为 60% 共享回去重构不划算 |
| React Native | 跨平台但非原生栈 | 不选——APNs/FCM 双端配置复杂，地图/图表性能不如原生 |
| Flutter | 自绘引擎 | 不选——SSE/APNs 集成需重头趟，Apple 生态融合度低 |

选择原生的核心论点：

1. **iOS 端已做完且稳定**。SwiftUI 代码不会回头改成 KMP shared module。两套原生代码并行维护的总成本低于一套跨平台框架的集成/调试/平台适配成本。
2. **Compose 与 SwiftUI 范式高度一致**。声明式 UI、@Observable ≈ StateFlow、NavigationStack ≈ NavHost。iOS Store 层的业务逻辑可以直接"翻译"成 Kotlin，不是重写。
3. **平台能力最完整**。FCM、Google Maps、DataStore、EncryptedSharedPreferences、Material 3 — 全是 Google 一等公民支持，没有桥接损耗。
4. **团队认知成本**。大部分 Android 开发者熟悉 Compose + Hilt + OkHttp 这套栈，招人/交接成本远低于小众框架。

### 2.2 与 iOS 的架构对称

两端保持分层一致，降低跨端理解成本：

```
┌────────────────────────────────────────────────────┐
│  iOS (SwiftUI)          Android (Compose)           │
├────────────────────────────────────────────────────┤
│  SwiftUI Views          @Composable functions       │  ← 声明式 UI
│  @Observable Stores     ViewModel + StateFlow       │  ← 响应式状态
│  @Environment           Hilt @Singleton + compose   │  ← 依赖注入
│  URLSession + async     OkHttp / Ktor + coroutines  │  ← 网络 + SSE
│  Keychain + UserDefaults EncryptedSP + DataStore    │  ← 本地持久化
│  NavigationStack        NavHost + NavController     │  ← 导航
│  APNs (PushDelegate)    FCM (FirebaseMessagingService) │  ← 推送
│  StoreKit 2             Google Play Billing 6       │  ← 内购
└────────────────────────────────────────────────────┘
```

---

## 3. 项目结构

```
android/
├── app/
│   ├── src/main/
│   │   ├── java/com/flatradar/app/
│   │   │   ├── FlatRadarApplication.kt        # Application + Hilt
│   │   │   ├── MainActivity.kt                # Single Activity host
│   │   │   │
│   │   │   ├── navigation/
│   │   │   │   ├── AppNavigation.kt            # NavHost 定义
│   │   │   │   ├── TopLevelDestination.kt      # Tab 枚举
│   │   │   │   └── NavigationCoordinator.kt    # Deep link 处理
│   │   │   │
│   │   │   ├── ui/
│   │   │   │   ├── theme/
│   │   │   │   │   ├── Theme.kt               # Material 3 主题
│   │   │   │   │   ├── Color.kt               # 色彩 token
│   │   │   │   │   ├── Type.kt                # 字体 token
│   │   │   │   │   └── Shape.kt               # 圆角/shape token
│   │   │   │   ├── dashboard/
│   │   │   │   │   ├── DashboardScreen.kt
│   │   │   │   │   └── DashboardViewModel.kt
│   │   │   │   ├── listings/
│   │   │   │   │   ├── ListingsScreen.kt
│   │   │   │   │   ├── ListingDetailScreen.kt
│   │   │   │   │   ├── ListingRow.kt
│   │   │   │   │   └── ListingsViewModel.kt
│   │   │   │   ├── map/
│   │   │   │   │   ├── MapScreen.kt
│   │   │   │   │   └── MapViewModel.kt
│   │   │   │   ├── calendar/
│   │   │   │   │   ├── CalendarScreen.kt
│   │   │   │   │   └── CalendarViewModel.kt
│   │   │   │   ├── notifications/
│   │   │   │   │   ├── NotificationsScreen.kt
│   │   │   │   │   ├── NotificationRow.kt
│   │   │   │   │   └── NotificationsViewModel.kt
│   │   │   │   ├── settings/
│   │   │   │   │   ├── SettingsScreen.kt
│   │   │   │   │   ├── FilterEditScreen.kt
│   │   │   │   │   ├── FeedbackScreen.kt
│   │   │   │   │   └── SettingsViewModel.kt
│   │   │   │   ├── auth/
│   │   │   │   │   ├── LoginScreen.kt
│   │   │   │   │   └── AuthViewModel.kt
│   │   │   │   ├── admin/
│   │   │   │   │   ├── AdminUsersScreen.kt
│   │   │   │   │   ├── AdminMonitorScreen.kt
│   │   │   │   │   └── AdminViewModel.kt
│   │   │   │   └── components/
│   │   │   │       ├── StatusBadge.kt
│   │   │   │       ├── PriceText.kt
│   │   │   │       ├── FeatureList.kt
│   │   │   │       ├── FilterChips.kt
│   │   │   │       └── EmptyState.kt
│   │   │   │
│   │   │   ├── data/
│   │   │   │   ├── remote/
│   │   │   │   │   ├── ApiClient.kt           # OkHttp + Retrofit / Ktor
│   │   │   │   │   ├── ApiModels.kt           # API 请求/响应 DTO
│   │   │   │   │   ├── SseClient.kt           # SSE 解析器
│   │   │   │   │   └── AuthInterceptor.kt     # Bearer token 注入
│   │   │   │   ├── local/
│   │   │   │   │   ├── TokenManager.kt        # EncryptedSharedPreferences
│   │   │   │   │   ├── PreferencesManager.kt  # DataStore
│   │   │   │   │   └── BiometricAuth.kt       # BiometricPrompt
│   │   │   │   └── repository/
│   │   │   │       ├── AuthRepository.kt
│   │   │   │       ├── ListingsRepository.kt
│   │   │   │       ├── NotificationsRepository.kt
│   │   │   │       └── ...
│   │   │   │
│   │   │   ├── domain/
│   │   │   │   ├── model/
│   │   │   │   │   ├── Listing.kt
│   │   │   │   │   ├── NotificationItem.kt
│   │   │   │   │   ├── UserInfo.kt
│   │   │   │   │   ├── MonitorStatus.kt
│   │   │   │   │   └── ...
│   │   │   │   └── usecase/                   # 可选，ViewModel 简单时不加
│   │   │   │
│   │   │   ├── di/
│   │   │   │   └── AppModule.kt               # Hilt @Module
│   │   │   │
│   │   │   ├── push/
│   │   │   │   ├── FlatRadarMessagingService.kt  # FirebaseMessagingService
│   │   │   │   └── PushTokenManager.kt
│   │   │   │
│   │   │   └── util/
│   │   │       ├── DateFormatter.kt
│   │   │       ├── CurrencyFormatter.kt
│   │   │       ├── NetworkMonitor.kt
│   │   │       └── Constants.kt
│   │   │
│   │   └── res/
│   │       ├── values/
│   │       │   ├── strings.xml                # 英文（默认）
│   │       │   └── themes.xml
│   │       ├── values-zh/
│   │       │   └── strings.xml                # 中文
│   │       └── drawable/                      # 图标/插图
│   │
│   ├── build.gradle.kts
│   └── proguard-rules.pro
│
├── build.gradle.kts                           # 根 build
├── settings.gradle.kts
└── gradle/
    └── libs.versions.toml                     # Version catalog
```

### 3.1 关键依赖

```toml
[versions]
kotlin = "2.1.0"
compose-bom = "2025.05.00"
hilt = "2.52"
okhttp = "4.12.0"
retrofit = "2.11.0"
kotlinx-coroutines = "1.9.0"
datastore = "1.1.2"
firebase-bom = "34.0.0"
maps-compose = "5.1.0"
coil = "2.7.0"                  # 图片加载（房源照片等）

[libraries]
compose-bom = { group = "androidx.compose", name = "compose-bom", version.ref = "compose-bom" }
compose-material3 = { group = "androidx.compose.material3", name = "material3" }
compose-navigation = { group = "androidx.navigation", name = "navigation-compose" }
hilt-android = { group = "com.google.dagger", name = "hilt-android", version.ref = "hilt" }
hilt-navigation-compose = { group = "androidx.hilt", name = "hilt-navigation-compose" }
retrofit = { group = "com.squareup.retrofit2", name = "retrofit", version.ref = "retrofit" }
retrofit-moshi = { group = "com.squareup.retrofit2", name = "converter-moshi" }
okhttp = { group = "com.squareup.okhttp3", name = "okhttp", version.ref = "okhttp" }
okhttp-logging = { group = "com.squareup.okhttp3", name = "logging-interceptor" }
datastore = { group = "androidx.datastore", name = "datastore-preferences" }
security-crypto = { group = "androidx.security", name = "security-crypto" }
firebase-messaging = { group = "com.google.firebase", name = "firebase-messaging" }
maps-compose = { group = "com.google.maps.android", name = "maps-compose" }
coil = { group = "io.coil-kt", name = "coil-compose" }
```

---

## 4. 架构设计：iOS → Android 逐层映射

### 4.1 状态管理

iOS 的 `@Observable` Stores 直接映射为 Android ViewModel + StateFlow：

```kotlin
// iOS: @Observable final class DashboardStore
// Android:

@HiltViewModel
class DashboardViewModel @Inject constructor(
    private val statsRepo: StatsRepository,
    private val listingsRepo: ListingsRepository
) : ViewModel() {
    var uiState by mutableStateOf(DashboardUiState())
        private set

    fun fetchSummary() {
        viewModelScope.launch {
            uiState = uiState.copy(isLoading = true)
            statsRepo.getPublicSummary()
                .onSuccess { uiState = uiState.copy(summary = it, isLoading = false) }
                .onFailure { uiState = uiState.copy(error = it.message, isLoading = false) }
        }
    }
}

data class DashboardUiState(
    val summary: MonitorStatus? = null,
    val chartKeys: List<String> = emptyList(),
    val isLoading: Boolean = false,
    val error: String? = null
)
```

关键差异：iOS 的 `@Observable` 是细粒度追踪（改单个字段只重绘相关 View），Compose 的 `mutableStateOf` 在 data class 上改一个字段会触发整个 `UiState` 重组。对于 Dashboard/Listings 这种重页面，建议要么拆多个 `StateFlow`，要么用 `SnapshotMutationPolicy` 或 `derivedStateOf` 优化。

### 4.2 导航

```
iOS:                                    Android:
TabView(selection: $coord.selectedTab)  Scaffold + NavigationBar
NavigationStack(path: $coord.path)      NavHost(navController, startDestination)
navigationDestination(for:)             composable(route) { ... }
NavigationCoordinator (deep link)       SavedStateHandle + NavController.navigate()
.onOpenURL { handleURL }                intent-filter + NavDeepLink
```

iPhone 4-tab / iPad 6-tab 的自适应策略在 Android 上可以进一步简化：Android 平板一般是 600dp+ 宽度时用 NavigationRail 替换底部 NavigationBar。用 `WindowSizeClass` 判断：

```kotlin
@Composable
fun AppNavigation(windowSize: WindowWidthSizeClass) {
    val useWideNav = windowSize == WindowWidthSizeClass.Expanded
    // iPhone-like: Bottom NavBar (4 items, Browse has segmented toggle)
    // iPad-like: NavigationRail (6 items directly)
}
```

### 4.3 网络层

**选择 OkHttp + Retrofit + Moshi**（而非 Ktor）。理由：
- Retrofit + Moshi 是 Android 社区事实标准，文档/社区支持最丰富
- OkHttp 的 Interceptor 机制天然适合 Bearer token 注入 + 401 全局监听
- OkHttp 内置 HTTP/2 支持（SSE 长连接复用连接池）

```kotlin
// 与 iOS APIClient.shared 对应
@Singleton
class ApiClient @Inject constructor(
    private val tokenManager: TokenManager,
    okHttpClient: OkHttpClient
) {
    // Retrofit service interfaces
    val authService: AuthService
    val listingsService: ListingsService
    val notificationsService: NotificationsService
    // ...
}
```

**API 信封**：后端统一返回 `{ok, data, error}`。Retrofit 侧用 `CallAdapter` 或手动解包：

```kotlin
// 通用响应壳
data class ApiResponse<T>(
    val ok: Boolean,
    val data: T?,
    val error: ApiError?
)

data class ApiError(
    val code: String,
    val message: String
)
```

自定义 `CallAdapter.Factory` 让 Retrofit 直接返回 `T`（非 200/401/403 时抛异常），对标 iOS `APIClient.request<T>()`。

### 4.4 SSE 客户端

iOS 用 `URLSession.AsyncBytes` + 自定义 SSE 解析器（~140 行 Swift）。Android 用 OkHttp 等价实现：

```kotlin
class SseClient(
    private val okHttpClient: OkHttpClient,
    private val token: String?
) {
    fun connect(lastId: Int): Flow<SseEvent> = callbackFlow {
        val request = Request.Builder()
            .url("$baseUrl/api/v1/notifications/stream?last_id=$lastId")
            .header("Authorization", "Bearer $token")
            .header("Accept", "text/event-stream")
            .header("Cache-Control", "no-cache")
            .build()

        val call = okHttpClient.newCall(request)
        val response = call.execute()
        val source = response.body?.source() ?: throw IOException("empty body")

        // 逐行解析 SSE（与 iOS 逻辑一致）
        // 直接 data: 行 emit，不依赖空行
        while (!source.exhausted()) {
            val line = source.readUtf8Line() ?: break
            if (line.startsWith("data:")) {
                val payload = line.removePrefix("data:").trimStart()
                trySend(SseEvent.Data(payload))
            } else if (line.startsWith(":")) {
                trySend(SseEvent.Keepalive)
            }
        }
        channel.close()
    }

    sealed class SseEvent {
        data class Data(val payload: String) : SseEvent()
        data class Retry(val ms: Int) : SseEvent()
        data object Keepalive : SseEvent()
    }
}
```

调用方 `NotificationsViewModel` 负责指数退避重连，跟 iOS `NotificationsStore.streamLoop()` 完全对称。

### 4.5 鉴权与 Token 管理

```
iOS:                                  Android:
KeychainManager.save(token)           EncryptedSharedPreferences
UserDefaults (fallback)               DataStore (非敏感偏好)
BiometricAuthService                  BiometricPrompt (fingerprint/face)
```

```kotlin
@Singleton
class TokenManager @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val prefs = EncryptedSharedPreferences.create(
        "flatradar_secure_prefs",
        MasterKey.DEFAULT_MASTER_KEY_ALIAS,
        context,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
    )

    fun saveToken(token: String) { prefs.edit().putString("auth_token", token).apply() }
    fun getToken(): String? = prefs.getString("auth_token", null)
    fun clearToken() { prefs.edit().remove("auth_token").apply() }
}
```

**AuthInterceptor**：OkHttp Interceptor 自动为每个请求注入 `Authorization: Bearer <token>`。收到 401/403 时 post 全局事件，`AuthViewModel` 监听并触发登出——与 iOS `APIClient.authFailedNotification` 设计对称。

### 4.6 推送：FCM

```
iOS:                                  Android:
PushDelegate (UIApplicationDelegate)  FirebaseMessagingService
APNs hex token → POST /devices        FCM token → POST /devices (platform="android")
Notification tap → deep link          PendingIntent → NavDeepLink
```

```kotlin
@AndroidEntryPoint
class FlatRadarMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        // POST /api/v1/devices/register { platform: "android", device_token: token, ... }
        pushTokenManager.registerToken(token)
    }

    override fun onMessageReceived(remoteMessage: RemoteMessage) {
        // 处理 data payload → 显示通知 + deep link
        val listingId = remoteMessage.data["listing_id"]
        // ...
    }
}
```

**后端改动**（~300 行 Python）：
- `notifier_channels/fcm.py` — 与 `apns.py` 对称，HTTP v1 API + OAuth2
- `mstorage/_devices.py` — `register_device` 的 `platform` 字段已支持，只需确认
- `/api/v1/devices/register` — `platform` 字段已在 body schema 中

### 4.7 地图

**Google Maps Compose**，无 osmdroid 备选。决策依据：

- FlatRadar 目标用户均为荷兰国际学生/young professional，设备几乎 100% 含 GMS，Play Services 依赖在实践中不是问题
- Compose 原生 maps 组件聚类 API 成熟，3 行代码开聚类拖拽，对标 iOS MapKit 使用体验
- 与 osmdroid 相比省掉 AndroidView 封装 + 手写聚类逻辑，至少省 3-5 天工时
- Google Cloud 每月 $200 免费额度（~28,000 次 Dynamic Maps 加载），远远覆盖地图页非高频访问

```kotlin
// Google Maps Compose 使用示例
@Composable
fun MapScreen(viewModel: MapViewModel) {
    val listings by viewModel.listings.collectAsStateWithLifecycle()
    val cameraPositionState = rememberCameraPositionState { ... }

    GoogleMap(
        cameraPositionState = cameraPositionState,
        uiSettings = MapUiSettings(zoomControlsEnabled = false)
    ) {
        // 聚类
        Clustering(items = listings.map { it.toClusterItem() })
    }
}
```

### 4.8 图表

iOS 用 Swift Charts（iOS 16+ 系统库）。Android 选择：

- **Vico**（Compose-native，Material 3 风格，GitHub 6k+ star）— 最推荐的轻量方案
- **MPAndroidChart** — 老牌但 View-based，需 `AndroidView` 桥接
- **YCharts** — Compose-native 但功能较新

Dashboard 的 sparkline + KPI charts 用 Vico 足够覆盖。

---

## 5. Material 3 设计系统映射

iOS HIG 与 Material 3 的数值差异不能直接照搬。以下是关键 token 映射：

| iOS (HIG) | Android (Material 3) | 映射 |
|---|---|---|
| `Color.secondarySystemBackground` | `MaterialTheme.colorScheme.surfaceVariant` | 列表行底色 |
| `.padding(.horizontal, 16)` | `HorizontalArrangement.spacedBy(16.dp)` | 卡片内边距 |
| `.cornerRadius(12)` | `RoundedCornerShape(12.dp)` | 卡片圆角 |
| `.font(.system(.caption))` | `MaterialTheme.typography.labelSmall` | 辅助文字 |
| `Label` (icon + text) | `NavigationBarItem` / `Icon + Text` | Tab 项 |
| `.tint(Color.accentColor)` | `MaterialTheme.colorScheme.primary` | 主色调 |
| `ProgressView()` | `CircularProgressIndicator()` | 加载态 |
| `.refreshable` | `PullToRefreshBox` (Material 3 1.3+) | 下拉刷新 |

色彩系统：iOS App 用 `Color+Tokens.swift` 定义语义色（`.accent`、`.success`、`.warning`、`.danger`）。Android 端用 `Color.kt` 做同样的事，挂到 `MaterialTheme.colorScheme`：

```kotlin
// 与 iOS Color+Tokens.swift 对齐
private val LightColors = lightColorScheme(
    primary = Color(0xFF5E6AD2),        // iOS accent
    onPrimaryContainer = Color(0xFF1A1A2E),  // iOS text primary
    secondaryContainer = Color(0xFFE5E7EB),  // iOS border
    error = Color(0xFFEF4444),           // iOS danger
    // ...
)
```

---

## 6. 阶段拆分与工时估算

| 阶段 | 内容 | 文件数（估） | 工时 |
|---|---|---|---|
| **A0** | 项目骨架 | 15 | 1 周 |
| | Android Studio 项目初始化 + Gradle Kotlin DSL | | |
| | Compose + Material 3 主题（Color/Type/Shape tokens） | | |
| | Hilt DI 框架集成 + `AppModule` | | |
| | Navigation scaffold（NavHost + NavigationBar + NavigationRail） | | |
| | `WindowSizeClass` 自适应（phone 4-tab / tablet 6-tab） | | |
| | 中英文 `strings.xml` 骨架 | | |
| | CI: GitHub Actions `build.yml` 增加 Android assemble | | |
| **A1** | 鉴权 + Dashboard + Listings | 20 | 2 周 |
| | `AuthViewModel` — 三档登录（admin/user/guest）+ register | | |
| | `TokenManager` — EncryptedSharedPreferences | | |
| | `BiometricAuth` — BiometricPrompt（对标 iOS Face ID） | | |
| | `ApiClient` — OkHttp + Retrofit + Moshi + AuthInterceptor | | |
| | `DashboardScreen` + `DashboardViewModel` — Summary + Charts | | |
| | `ListingsScreen` + `ListingDetailScreen` — 分页 + 多条件筛选 | | |
| | `ListingRow` — 状态徽章、价格、面积、年龄文本 | | |
| **A2** | Map + Calendar | 10 | 1.5 周 |
| | Google Maps Compose 集成 + 聚类 + 状态色 pin | | |
| | `MapScreen` + `MapViewModel` — 与 iOS MapView 功能对齐 | | |
| | `CalendarScreen` + `CalendarViewModel` — 月历 + 按日期展开 | | |
| **A3** | Notifications + SSE | 8 | 1 周 |
| | `SseClient` — OkHttp streaming + SSE 解析 | | |
| | `NotificationsViewModel` — 分页 + SSE 实时增量 + 未读角标 | | |
| | `NotificationsScreen` — 分组列表 + 滑动/长按标记已读 | | |
| | `NotificationRow` — 类型图标 + 时间相对文本 | | |
| **A4** | FCM 推送 | 8 | 1.5 周 |
| | Firebase 项目创建 + `google-services.json` | | |
| | `FlatRadarMessagingService` — token 注册 + 刷新 | | |
| | 通知点击 deep link → NavigationCoordinator | | |
| | 后端 `fcm.py` 开发 + 测试推送验证 | | |
| | 后端 `/api/v1/devices/register` 确认 `platform="android"` 通道 | | |
| **A5** | Settings + 多语言 + 深色模式 + 错误处理 | 10 | 1.5 周 |
| | `SettingsScreen` — 服务器地址、颜色方案、关于 | | |
| | `FilterEditScreen` — 多维度 listing filter 编辑（对标 iOS FilterEditView） | | |
| | Feedback 提交、GDPR 数据导出、删除账户 | | |
| | 中英文完整翻译 + locale 切换 | | |
| | 全局错误 snackbar + 401 自动登出 | | |
| | Crash 诊断上报（对标 iOS CrashDiagnosticsCollector） | | |
| **A6** | 打磨 + 内购 + Play Store 上架 | 6 | 2 周 |
| | Material 3 视觉对齐（spacing/elevation/shape token 逐页校对） | | |
| | Google Play Billing 6 — "Buy me a coffee" 内购（一次性集成） | | |
| | Data Safety 表格 + 隐私政策链接 + 非官方关系声明 | | |
| | 内部测试（Closed testing — Google 要求 12 人 14 天） | | |
| | Play Store 截图（多语言 + 多设备） | | |
| | 提交审核 → 正式发布 | | |

**总工时：约 10 周**（一个全职 Android 开发）。

### 依赖关系

```
A0 (骨架)
 └─► A1 (鉴权+Dashboard+Listings)
      ├─► A2 (Map+Calendar) ── 可与 A1 并行的部分：Map/Calendar ViewModel + Screen
      ├─► A3 (Notifications+SSE) —— 可与 A2 并行
      │    └─► A4 (FCM) —— 依赖 A3 的 SSE 跑通 + 后端 FCM 通道就绪
      └─► A5 (Settings+多语言+深色模式) —— 可与 A2/A3 并行
           └─► A6 (打磨+上架) —— 依赖全部完成
```

关键路径：A0 → A1 → A3 → A4 → A6。最长路径约 8 周。

---

## 7. 测试策略

| 层级 | 技术 | 覆盖 |
|---|---|---|
| 单元测试 | JUnit 5 + MockK | ViewModel 业务逻辑、Repository 数据组装 |
| UI 组件测试 | Compose Testing | Screen snapshot、交互逻辑、dark mode 切换 |
| 端到端测试 | UI Automator + Firebase Test Lab | 登录 → Dashboard → Listings 筛选 → 详情 关键路径 |
| 截图测试 | Roborazzi / Paparazzi | 多语言 + 多屏幕尺寸的截图回归（对标 iOS ScreenshotTests） |

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| **Google Play 20 人封闭测试要求** | 个人开发者账号 2024 年起强制要求 12+ 人连续 14 天测试 | 提前招募测试用户（可从现有 iOS 用户群征集），A5 阶段就开始攒 |
| **Material 3 vs HIG 视觉一致性** | 两端 "同一款 App" 但视觉风格不同 | 接受合理差异，不对齐像素级；用户对平台原生风格有预期 |
| **FCM 服务端 token 失效** | 用户收不到推送 | 同 APNs `Unregistered` 处理路径，`FlatRadarMessagingService.onNewToken` 自动刷新 |
| **Google Maps API 计费** | 单月 $200 credit 用完后计费 | 地图页非高频（用户每天开几次），远低于免费额度，不影响实际使用 |
| **无 GMS 设备**（华为等） | FCM + Google Maps 不可用 | Android 12+ 设备 GMS 覆盖率极高，不作备选处理 |
| **Play Store Data Safety 审核** | 被拒上架 | 与 iOS 隐私标签内容一致复用；非官方关系声明与 App Store 版本对齐 |

---

## 9. 后端待办

Android 客户端开发依赖以下后端改动（~300 行 Python）：

1. **`notifier_channels/fcm.py`** — FCM HTTP v1 API 推送通道（对标 `apns.py`）
2. **`mstorage/_devices.py`** — 确认 `platform` 字段支持 `"android"`
3. **Devices 端点** — 注册/注销/列表/测试推送均已在 `platform` body 字段支持 android
4. **SSE 流** — 无需改动，Android 端用 OkHttp 连接与 iOS `SSEClient` 协议一致

这些后端改动不受 iOS 上架节奏影响，A3 阶段前完成即可。

---

## 10. 决定记录

以下问题已确定：

| 问题 | 决定 |
|---|---|
| **minSdk** | **31 (Android 12)**。Material You 动态取色原生支持，省一套主题切换逻辑。荷兰/欧洲学生用户设备普遍 2021 年后，覆盖率 > 80% |
| **地图引擎** | **Google Maps Compose**。目标用户 100% 有 GMS，无 osmdroid 备选 |
| **内购时机** | **A6 阶段一次性集成**。前期不分散精力 |
| **App 签名** | **Google Play App Signing**。本地 upload key + Google 托管 signing key，对标 iOS Xcode 自动签名 |
