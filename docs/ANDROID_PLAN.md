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
│   │   │   │   ├── FcmService.kt              # FirebaseMessagingService
│   │   │   │   ├── FcmTokenManager.kt         # Token 存储 + 设备注册/注销 API
│   │   │   │   └── NotificationChannels.kt    # 通知渠道常量
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
PushDelegate (UIApplicationDelegate)  FcmService (FirebaseMessagingService)
APNs hex token → POST /devices        FCM token → POST /devices (platform="android")
Notification tap → deep link          PendingIntent → NavDeepLink
```

**FcmService**（`@AndroidEntryPoint`）：
```kotlin
@AndroidEntryPoint
class FcmService : FirebaseMessagingService() {
    @Inject lateinit var fcmTokenManager: FcmTokenManager

    override fun onNewToken(token: String) {
        super.onNewToken(token)
        fcmTokenManager.onTokenRefreshed(token)  // 存储 token，已登录则立即注册
    }

    override fun onMessageReceived(message: RemoteMessage) {
        // 前台/后台统一手动展示通知
        // 解析 data payload → title / body / listing_id
        // 构建 PendingIntent（h2smonitor://listing/<id> deep link）
        // NotificationCompat.Builder → notify()
    }
}
```

**FcmTokenManager**（`@Singleton`）：
- `onTokenRefreshed(token)`: 存储 FCM token 到 SharedPreferences；若 auth token 存在则自动注册
- `registerCurrentDevice()`: 获取当前 FCM token → `POST /api/v1/devices/register` (env=sandbox/production, platform=android)
- `unregisterCurrentDevice()`: `DELETE /api/v1/devices/{deviceId}`，登出时调用
- 设备注册在 `AuthViewModel.applyMe()`（登录成功后）和 `clearAuth()`（登出时）自动触发

**通知渠道**：`FlatRadarApplication.onCreate()` 创建 `listings`（HIGH importance）和 `general`（DEFAULT）渠道。

**POST_NOTIFICATIONS 权限**：Android 13+ 用户在登录后通过 `rememberLauncherForActivityResult` 请求运行时权限。

**深度链接**：`MainActivity.onNewIntent()` → `handleDeepLink()` → `NavigationCoordinator.openListing(id)`。

**AndroidManifest.xml**：
```xml
<service android:name=".push.FcmService" android:exported="false">
    <intent-filter>
        <action android:name="com.google.firebase.MESSAGING_EVENT" />
    </intent-filter>
</service>
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

## 6. 当前实现进度与复盘

截至 2026-05-25，Android 客户端已完成 FCM 推送闭环（客户端 + 后端 sender），并完成架构审查与测试补齐：

| 模块 | 状态 | 说明 |
|---|---|---|
| A0 项目骨架 | 已完成 | Gradle Kotlin DSL、Compose Material 3、Hilt、NavigationBar/NavigationRail、自适应导航均已落地。 |
| A1 鉴权 + Dashboard + Listings | 部分完成 | 三档登录、注册、token 持久化、BiometricPrompt user 登录解锁、Dashboard、Listings、详情页、全局 snackbar 错误通道已接入；Dashboard Explore 统计卡片已按后端动态 chart key 解码并恢复平台/状态/价格/类型/能源/租客分布展示，且可点击展开完整统计明细；AuthViewModel（12 测试）、DashboardViewModel（6 测试）、ListingsViewModel（11 测试）单元测试已完成。BiometricPrompt 真机指纹/面部解锁已验证通过。 |
| A2 Map + Calendar | 部分完成 | Map 已接 Google Maps Compose，支持聚类、状态色 marker、边界初始视角、选中卡片详情、无 key 列表降级与重试；Calendar 已升级为月格、每日数量、选中日房源列表与重试，CalendarViewModel（9 测试）单元测试已完成。真机地图手动验证仍待补。 |
| A3 Notifications + SSE | 部分完成 | 通知列表已重构为扁平化 flat list 布局，包括顶部动态 stats 胶囊 pill、实时滚动筛选 chips、覆盖型未读提示蓝点 badge、箭头符号标准化与 Extended FAB；NotificationsViewModel（10 测试）覆盖 load、markRead/markAllRead、SSE Data 新增/去重/已读更新、Keepalive、error 重连。 |
| A4 FCM 推送 | 已完成 | **客户端**：Firebase 项目 `flatradar-66342`，`google-services.json` 已配置，`FcmService` + `FcmTokenManager` + 通知渠道 + POST_NOTIFICATIONS 运行时权限 + deep link 全部接入。**后端**：`notifier_channels/fcm.py`（~260 行，OAuth2 服务账号认证 + FCM HTTP v1 API），`mcore/push.py` 按 `platform` 字段分流 iOS（APNs）/ Android（FCM）双发，`FcmTokenManager` 异常已加日志不再静默吞错。服务端已部署 `FCM_ENABLED=true` + service account JSON 密钥（`/secrets/`）。端到端推送通道已拉通。 |
| A5 Settings + 多语言 + 深色模式 | 部分完成 | Settings 已支持 DataStore 持久化、System/Light/Dark 主题、反馈、法律文档、改密码、GDPR 数据导出、删号、Admin Users/Monitor；法律文案已统一为 `app/legal/*.txt` 单一数据源，三端（web/iOS/Android）均通过 API 获取 + 本地缓存 fallback，修改一处即时生效；中文字符串 `values-zh/strings.xml` 已覆盖 ~170 条目，与英文版本完全对称；Crash 诊断仍待补。 |
| A6 打磨 + 内购 + 上架 | 未开始 | Google Play Billing、截图、Data Safety、封闭测试和上架流程尚未启动。 |

本轮关键复盘：

- 地图不再使用城市分组列表作为唯一体验；`MAPS_API_KEY` 存在时走 GoogleMap，缺失时保留可用 fallback，marker 已按房源状态着色并接入官方 clustering，初始视角按房源 bounds 适配，选中卡片展示状态、价格、面积、入住日期和来源信息。
- Map/Calendar 曾按通用分页模型读取 `data.items`，但后端契约实际为 `data.listings`，地图还使用 `lat/lng` 和 `building` 这类轻量字段；Android DTO 已拆出 `MapCalendarListingDto` 并转换为 UI 需要的 `Listing`，避免 `Required value 'items' missing at $.data` 解析错误。开发阶段无旧 Android 客户端兼容压力，DTO 直接按 `data.listings` 收敛，不保留 `items` fallback。
- Settings 界面优化：移除了 `server_url` 服务器地址配置以防用户误改，并新增在 “Push Notification Filter” 菜单项下方动态显示活跃过滤器摘要；同时为 “Log Out” 按钮增加了二级确认弹窗防误触；`color_scheme` 继续驱动全局主题。
- 账号合规继续补齐：user 角色可在 Settings 修改 app password，并通过 Android 系统分享面板导出 `/me/export` 返回的个人数据 JSON。
- A1 鉴权继续补齐：user 可选择在本机保存生物识别登录，登录页可用 BiometricPrompt 解锁后复用正常登录 API，Settings 可移除本机保存的生物识别登录。
- A1 错误展示从各页面局部文字扩展为 root `AppErrorBus` + snackbar；登录、注册、Dashboard、Listings、Listing Detail 的后端错误会同步进入全局 snackbar。
- Dashboard Explore 统计卡片的问题根因是 Android `ChartEntry` 只按 `label/count/date` 解码，但后端不同图表返回 `source/status/range/hour/city/label` 等动态字段；Android 已对齐 iOS 的动态 label 归一化思路，并为平台、状态、价格、类型、能源、租客恢复 mini distribution 展示。卡片点击后通过 bottom sheet 展开完整分布明细，Dashboard 根内容补 `statusBarsPadding()` 以适配 edge-to-edge 状态栏。
- Listing Detail 对齐 iOS 关键结构：顶部 source/status/city，价格、入住日期、面积、建筑 metric cards，Key Details、All Details、Monitoring、官方平台链接均稳定展示。当前 API/model 未提供 listing 图片 URL，因此 Android/iOS 原生详情页均无房源图片展示。
- Calendar 从月份列表升级为月格视图，并在选中日下方展示可入住房源；Android Calendar 日期分组已改为与 iOS 一致的 `available_from` 前 10 位 day key，不再复用会过滤 `2049/2050` 的通用时间 helper，避免后端已有房源但当天列表为空；空态和错误态均提供 retry。
- Notifications 从平铺列表升级为 TODAY/YESTERDAY/EARLIER inbox，支持导航 unread 角标、单条已读、滑动已读、更多菜单、类型色点和相对时间；导航和 Alerts 页面共用同一个 ViewModel，避免重复 SSE 连接。
- **FCM 后端 sender**：`notifier_channels/fcm.py` 对标 `apns.py`，OAuth2 服务账号认证（RS256 JWT → access token 缓存 55min），`FcmClient.send_one/send_many`，data-only payload 由 Android `FcmService` 统一展示通知 + deep link。`mcore/push.py` 按 `device_tokens.platform` 字段分流，所有 dispatch 函数双发 APNs + FCM，平台隔离故障互不阻塞。FCM 测试 35 个（client 18 + dispatcher 17），Python 全量 1033 passed。
- **架构审查与修复**：C1（SQLite 多线程）确认无风险——monitor 单进程主线程写，web Gunicorn 多进程独立连接，WAL 模式保证并发安全。C2（users.json 双存储）确认不存在——旧 JSON 仅一次性迁移，运行期只读 SQLite `user_configs`。C3（FCM 私钥日志泄漏）已修复——文件加载失败不再 dump traceback，构造失败只打 `project_id` 不碰 `private_key`。
- **Android Listing 模型对齐 iOS**：删除 `areaText`/`energyLabel`/`buildingText`/`finishing`/`floor`/`rooms`/`occupancy`/`contractType`/`tenantRequirement` 共 9 个冗余字段，`display*` 计算属性统一从 `featureMap` 派生（与 iOS 一致）。`MapCalendarListingDto.toListing()` 将 DTO flat 字段 `putIfAbsent` 合并进 `featureMap`，后端改 key 名时两端同步自适应。`ListingDetailScreen` 改用 `display*` 属性。全量 47 Android 测试通过。
- **测试总览**：Python 1033 tests（含 FCM 35），Android 47 tests（Auth 12 / Dashboard 6 / Listings 11 / Calendar 9 / Notifications 10）。ViewModel 层无 Repository 重构，MockK + kotlinx-coroutines-test 直接 mock `ApiClient`。
- **法律文案统一**：之前四份独立副本（`legal_text.py` web / `LegalText.kt` Android / `LegalText.swift` iOS / API 无端点），修改需同步四处。现在 `app/legal/*.txt` 为 canonical source of truth，`GET /api/v1/legal` 公开 API 端点，三端均 API 优先 + 本地缓存 fallback。免责条款已从"仅 Holland2Stay"更新为多平台中立声明（"not affiliated with any of the housing platforms it monitors"）。
- **中文字符串完整覆盖**：`values/strings.xml` + `values-zh/strings.xml` 各 ~170 条目完全对称，覆盖 Tab、仪表盘、登录注册、房源列表/详情、地图、日历、通知、设置、管理面板、使用条款、通用文案。Kotlin 代码中硬编码英文暂不动，后续可逐步迁移到 `R.string.*`。
- **Android 代码审查与修复**：全量代码审查发现 6 个问题（2 Critical + 3 High + 1 Medium），全部修复——SSE 阻塞 Main 线程（`SseClient` → `withContext(Dispatchers.IO)`）、深链接断开（`AppNavigation` 加 `consumePendingListingId` 消费）、弱网误删 token（`restoreSession` 只对 401/403 清）、Settings 状态擦除（`.copy()` 替代新建）、LocationListener 内存泄漏（15s timeout）、filter 不生效（monitor 侧 `write_reload_request()`）。
- **iOS 单元测试补齐**：新增 `FlatRadarTests` target（31 个测试），覆盖 Listing 模型解码/computed properties/statusKind、APIResponse 信封、AuthModels 编解码、NotificationItem 解码/listingTitleHint/NotificationKind 分类。此前 iOS 端 13K 行代码零单元测试，现在核心模型层有覆盖。
- 设计系统开始从 iOS 风格 token 收敛到 Android Material 3：固定 seed `#0057CC`、light/dark role mapping、M3 type scale、shape scale、FlatRadar `book/lottery/reserved` semantic status token、80dp bottom navigation 已落地；Listings、Detail、Settings、Map、Calendar 已完成第一轮 surface/shape/status/token 收口，后续重点转为真机视觉 QA、文案本地化和截图回归。
- FCM 不在本轮启用。`docs/API.md` 仍标注 Android FCM sender 未完成，客户端不能依赖 `/devices/test` 验证 FCM。

### 架构师视角：下一阶段策略

当前 Android 端已经从“功能空壳”进入“可编译、可体验、主要路径已接通”的阶段。下一阶段不应继续横向堆功能，而应进入 **Release Candidate 收敛模式**：冻结 A1/A2/A3/A5 的核心交互面，补验证、测试、配置边界和上架前风险项。只有验证闭环稳定后，再切到 A4 FCM 和 A6 Play Store。

架构原则：

1. **先稳定核心路径，再扩功能**
   - 核心路径定义为：登录/guest → Dashboard → Listings → Detail → Map/Calendar → Notifications → Settings。
   - 除 FCM、Billing、Crash 诊断外，不再新增大功能；只做错误处理、状态一致性、测试和真机验证。

2. **不做大重构，只补必要接缝**
   - 现阶段 ViewModel 直接依赖 `ApiClient` 可以接受；为测试而新增小型 fake/service seam 可以做，但暂不引入完整 Repository 层重构。
   - 避免为了“架构纯度”拆散已经可工作的 Compose + Hilt + Retrofit 结构。

3. **所有剩余开发都必须带验收口径**
   - 每个任务完成后至少跑 `./gradlew :app:assembleDebug`。
   - 对 Map、Biometric、通知角标、Settings server URL 这类依赖系统能力的功能，必须补真机/模拟器手动验证记录。
   - A3 SSE、Listings 分页、Calendar 日期过滤这种纯客户端逻辑应补 ViewModel 单元测试。

4. **后端阻塞项和客户端项分离**
   - A4 FCM 在后端 sender、Firebase 项目、`google-services.json` 未齐前不进入主线。
   - 客户端继续保留 devices DTO/接口，但不把 `/devices/test` 当作 Android FCM 验收。

### 下一步待完成

下一轮按“验证 → 测试 → 合规 → 后端集成 → 上架”的顺序推进：

1. **R1 手动验证矩阵收口（最高优先级）**
   - [x] A1：验证 Biometric 保存、登出后解锁、Settings 移除、无生物识别设备 fallback（已通过模拟器验证正常）。
   - [x] A1：验证 401、登录失败、Dashboard 网络失败、Listings/Detail 后端错误都进入全局 snackbar，且页面状态不丢（已验证正常）。
   - [/] A2：验证有/无 `MAPS_API_KEY`、cluster 点击、单点 marker 点击、bounds 初始视角、`/map` 与 `/calendar` 真实响应解析、Calendar 跨月、无房源日期和占位日期过滤（Calendar 已验证正常，Map API Key 已于 local.properties 完成配置，待在真机上最终确认效果）。
   - [ ] A3：验证新通知到达后角标增量、单条 mark read 后角标递减、mark all read 后角标清零。

2. **R2 客户端测试补齐**（已完成）
   - [x] `AuthViewModel` — 12 测试 (login/logout/register/deleteAccount/restoreSession/guest)
   - [x] `DashboardViewModel` — 6 测试 (fetchAll guest/user/error/meSummary failure/clear/initial)
   - [x] `NotificationsViewModel` — 10 测试 (load/markRead/markAllRead/SSE Data merge/update/keepalive/error)
   - [x] `CalendarViewModel` — 9 测试 (load 成功/失败/分组/过滤/月份导航/日期选择)
   - [x] `ListingsViewModel` — 11 测试 (load/loadMore/updateSearch/updateFilters/clearFilters/filterOptions/isActive)
   - 总计 47 个 ViewModel 单元测试，使用 MockK + kotlinx-coroutines-test，无 Repository 重构

3. **R3 A5 本地化与合规收口**
   - [x] 补齐完整 `values-zh`（~170 条目），覆盖全页面。
   - [x] Terms/Privacy 文案已统一为多平台中立声明（"与任何房源平台无关"），法律文本已合并为 `app/legal/*.txt` 单一数据源，三端 API 获取 + 本地缓存。
   - Crash 诊断暂不实现，除非后端明确诊断接收协议；文档继续列为 A5 待办。

4. **R4 A4 FCM 推送（已完成）**
   - [x] Firebase 项目 + `google-services.json` 配置完成
   - [x] `FcmService`（onNewToken / onMessageReceived）、`FcmTokenManager`（设备注册/注销）、通知渠道、运行时权限、deep link 全部接入
   - [x] 后端 `notifier_channels/fcm.py`（OAuth2 + FCM HTTP v1 API）+ `mcore/push.py` 平台分流双发
   - [x] 服务端 `FCM_ENABLED=true` + service account JSON 已部署
   - [ ] 真机验收：Android 设备收到 FCM 推送，点击通知进入对应 listing

5. **R5 A6 上架准备**
   - Google Play Billing coffee、Data Safety、截图、多设备/多语言测试、封闭测试计划进入 A6。
   - 开始招募封闭测试用户应提前于最终功能完成，避免 Google Play 14 天测试周期卡发布时间。

### Android 本地配置

`android/local.properties` 不应提交。除 Android SDK 路径外，本地可追加：

```properties
MAPS_API_KEY=your_google_maps_android_key
```

Gradle 会把该值注入 `AndroidManifest.xml` 的 `com.google.android.geo.API_KEY`，并生成 `BuildConfig.MAPS_API_KEY` 供运行时判断。未配置时，Map 页自动展示列表 fallback 和配置提示。

---

## 7. 阶段拆分与工时估算

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
| **A1** | 鉴权 + Dashboard + Listings（部分完成：Biometric、全局 snackbar、Detail parity 已接入） | 20 | 2 周 |
| | `AuthViewModel` — 三档登录（admin/user/guest）+ register | | |
| | `TokenManager` — EncryptedSharedPreferences | | |
| | `BiometricAuth` — BiometricPrompt（对标 iOS Face ID） | | |
| | `ApiClient` — OkHttp + Retrofit + Moshi + AuthInterceptor | | |
| | `DashboardScreen` + `DashboardViewModel` — Summary + Charts | | |
| | `ListingsScreen` + `ListingDetailScreen` — 分页 + 多条件筛选 | | |
| | `ListingRow` — 状态徽章、价格、面积、年龄文本 | | |
| **A2** | Map + Calendar（部分完成：GoogleMap + clustering + fallback + Calendar 月格已落地） | 10 | 1.5 周 |
| | Google Maps Compose 集成 + 聚类 + 状态色 pin | | |
| | `MapScreen` + `MapViewModel` — 与 iOS MapView 功能对齐 | | |
| | `CalendarScreen` + `CalendarViewModel` — 月历 + 按日期展开 | | |
| **A3** | Notifications + SSE（部分完成：分组 inbox + 单条/滑动已读 + unread 角标已落地） | 8 | 1 周 |
| | `SseClient` — OkHttp streaming + SSE 解析 | | |
| | `NotificationsViewModel` — 分页 + SSE 实时增量 + 未读角标 | | |
| | `NotificationsScreen` — 分组列表 + 滑动/长按标记已读 | | |
| | `NotificationRow` — 类型图标 + 时间相对文本 | | |
| **A4** | FCM 推送（端到端已拉通） | 4 | 已完成 |
| | Firebase 项目 + `google-services.json` + Gradle 插件 | | |
| | `FcmService` — onNewToken 注册 + onMessageReceived 展示通知 | | |
| | `FcmTokenManager` — Token 存储 + 设备注册/注销（含异常日志） | | |
| | `FlatRadarApplication` 通知渠道（listings/general）+ Android 13+ 运行时权限 | | |
| | 通知点击 deep link → `MainActivity.handleDeepLink()` → `NavigationCoordinator.openListing()` | | |
| | 后端 `notifier_channels/fcm.py`（OAuth2 + FCM HTTP v1）+ `mcore/push.py` 平台分流双发 | | |
| | 服务端 `FCM_ENABLED=true` + service account JSON 已部署 | | |
| **A5** | Settings + 多语言 + 深色模式 + 错误处理（部分完成：server URL/主题/法律/反馈/改密码/导出/admin） | 10 | 1.5 周 |
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

### RC 收敛里程碑

| 里程碑 | 目标 | 进入条件 | 退出条件 |
|---|---|---|---|
| **RC1 验证收口** | 证明当前 Android 核心功能在设备上可用 | `assembleDebug` 通过，A1/A2/A3/A5 主要功能已接入 | 手动验证矩阵完成，记录所有阻塞 bug |
| **RC2 测试补齐** | 锁住最容易回归的状态逻辑 | RC1 阻塞 bug 修完 | 47 个 ViewModel 单测覆盖 Auth/Dashboard/Notifications/Calendar/Listings 关键状态 ✅ |
| **RC3 本地化与合规** | 达到可给测试用户使用的文本和合规水位 | RC2 通过 | `values-zh` 170 条目完整覆盖，法律文本三端统一 ✅ |
| **RC4 FCM 集成** | 端到端推送通道已拉通 | 后端 FCM sender + 客户端均已就绪 | 真机验收 FCM 收发、点击 deep link（待真机测试） |
| **RC5 上架准备** | 进入 Google Play 发布流程 | RC4 通过 | Billing/Data Safety/截图/封闭测试计划完成 |

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

## 8. 测试策略

| 层级 | 技术 | 覆盖 |
|---|---|---|
| 单元测试 | JUnit 5 + MockK | ViewModel 业务逻辑、Repository 数据组装 |
| UI 组件测试 | Compose Testing | Screen snapshot、交互逻辑、dark mode 切换 |
| 端到端测试 | UI Automator + Firebase Test Lab | 登录 → Dashboard → Listings 筛选 → 详情 关键路径 |
| 截图测试 | Roborazzi / Paparazzi | 多语言 + 多屏幕尺寸的截图回归（对标 iOS ScreenshotTests） |

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| **Google Play 20 人封闭测试要求** | 个人开发者账号 2024 年起强制要求 12+ 人连续 14 天测试 | 提前招募测试用户（可从现有 iOS 用户群征集），A5 阶段就开始攒 |
| **Material 3 vs HIG 视觉一致性** | 两端 "同一款 App" 但视觉风格不同 | 接受合理差异，不对齐像素级；用户对平台原生风格有预期 |
| **FCM 服务端 token 失效** | 用户收不到推送 | 同 APNs `Unregistered` 处理路径，`FlatRadarMessagingService.onNewToken` 自动刷新 |
| **Google Maps API 计费** | 单月 $200 credit 用完后计费 | 地图页非高频（用户每天开几次），远低于免费额度，不影响实际使用 |
| **无 GMS 设备**（华为等） | FCM + Google Maps 不可用 | Android 12+ 设备 GMS 覆盖率极高，不作备选处理 |
| **Play Store Data Safety 审核** | 被拒上架 | 与 iOS 隐私标签内容一致复用；非官方关系声明与 App Store 版本对齐 |

---

## 10. 后端待办

Android 客户端开发依赖以下后端改动（~300 行 Python）：

1. **`notifier_channels/fcm.py`** — FCM HTTP v1 API 推送通道（对标 `apns.py`）
2. **`mstorage/_devices.py`** — 确认 `platform` 字段支持 `"android"`
3. **Devices 端点** — 注册/注销/列表/测试推送均已在 `platform` body 字段支持 android
4. **SSE 流** — 无需改动，Android 端用 OkHttp 连接与 iOS `SSEClient` 协议一致

这些后端改动不受 iOS 上架节奏影响，但 Android 正式启用 FCM 前必须完成。当前客户端只保留 devices API 模型和服务接口，不展示 FCM test push 作为验收依据。

---

## 11. 决定记录

以下问题已确定：

| 问题 | 决定 |
|---|---|
| **minSdk** | **31 (Android 12)**。Material You 动态取色原生支持，省一套主题切换逻辑。荷兰/欧洲学生用户设备普遍 2021 年后，覆盖率 > 80% |
| **地图引擎** | **Google Maps Compose**。目标用户 100% 有 GMS，无 osmdroid 备选 |
| **内购时机** | **A6 阶段一次性集成**。前期不分散精力 |
| **App 签名** | **Google Play App Signing**。本地 upload key + Google 托管 signing key，对标 iOS Xcode 自动签名 |
