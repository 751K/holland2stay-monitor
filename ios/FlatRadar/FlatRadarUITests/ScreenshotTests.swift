//
//  ScreenshotTests.swift
//  FlatRadarUITests
//
//  App Store 截图自动化。每个 testCapture* 走一个关键页面，存为 XCTAttachment。
//  跑完后 .xcresult 包里能用 xcparse 提取 PNG。
//
//  设计原则
//  --------
//  - 每个 test 独立 launch app 一次，通过 launch args 直接定位到目标页面/
//    模式，避免靠 UI menu 切换（iPhone 26 的 Menu picker 在 UI Test 下
//    切换不可靠）
//  - 仅 guest 模式：避免依赖真实账号 + 真实数据；条款/biometric prompt 全跳过
//  - 关动画：FlatRadarApp.init 检测 UI_TEST_SCREENSHOT_MODE 后
//    UIView.setAnimationsEnabled(false)
//

import UIKit
import XCTest

final class ScreenshotTests: XCTestCase {

    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
    }

    override func tearDownWithError() throws {
        app = nil
    }

    // MARK: - Captures

    @MainActor
    func testCapture00_Login() throws {
        // UI_TEST_SHOW_LOGIN 阻止自动 guest，让 LoginView 留下
        launch(extra: ["UI_TEST_SHOW_LOGIN"])
        // LoginView 有 hero 动画 + 实时统计加载，留 2.5s 渲染
        sleep(3)
        // 七条用例里这条原本是唯一没有断言的，而它偏偏是唯一会被「加了凭据登录」
        // 打破的：拍出来是 Dashboard，商店里于是两张 Dashboard、没有登录页。
        //
        // 用「主界面不该存在」来判定，而不是找登录页上的某个控件：后者要么是
        // 翻译过的文案，要么依赖具体布局；而 tab bar 在登录页上一定不存在。
        XCTAssertFalse(app.buttons[Tab.dashboard].exists,
                       "登录页上不该有主界面的 tab——说明它自动登录了。"
                       + "当前界面层级：\n" + dumpHierarchy())
        snap(named: "00-Login")
    }

    @MainActor
    func testCapture01_Dashboard() throws {
        launch(extra: ["UI_TEST_TAB=dashboard"])
        waitForMainUI()
        assertTabSelected(Tab.dashboard, "Dashboard")
        // 给 Dashboard chart 渲染
        sleep(2)
        snap(named: "01-Dashboard")
    }

    @MainActor
    func testCapture02_Listings() throws {
        let (args, tab) = browse("list", padTab: Tab.listings)
        launch(extra: args)
        waitForMainUI()
        assertTabSelected(tab, "Listings")
        sleep(2)
        snap(named: "02-Listings")
    }

    @MainActor
    func testCapture03_Map() throws {
        let (args, tab) = browse("map", padTab: Tab.map)
        launch(extra: args)
        waitForMainUI()
        assertTabSelected(tab, "Map")
        // Leaflet 渲染稍慢
        sleep(3)
        snap(named: "03-Map")
    }

    @MainActor
    func testCapture04_Calendar() throws {
        let (args, tab) = browse("calendar", padTab: Tab.calendar)
        launch(extra: args)
        waitForMainUI()
        assertTabSelected(tab, "Calendar")
        sleep(2)
        snap(named: "04-Calendar")
    }

    @MainActor
    func testCapture05_Notifications() throws {
        // 这一屏访客看不到——tab bar 里根本没有 Notifications。必须带凭据跑
        // （UI_TEST_USERNAME / UI_TEST_PASSWORD），否则下面的断言会直接失败，
        // 而不是悄悄拍下 Dashboard。
        launch(extra: ["UI_TEST_TAB=notifications"])
        waitForMainUI()
        // launch arg 落位在这一屏上不可靠：登录是异步的，而 tab 是同步设的，
        // 两轮云端实测都停在了 Dashboard。改顺序（先 await 再设 tab）也没解决，
        // 大概率是 MainTabView 挂载时又把 selectedTab 读回了自己的默认值。
        //
        // 不再猜——直接点那个 tab。UI 点击对单个 tab 是可靠的（原设计避开
        // menu 是因为 Browse 的三个子模式藏在 Menu 里，那才不稳）。
        // 按序号点，不按标题——标题在非英文语言下是翻译过的。
        // 按 symbol 点。两种设备共用 bell.fill，且与语言无关。
        let alerts = app.buttons[Tab.alerts].firstMatch
        if alerts.waitForExistence(timeout: 60) { alerts.tap() }
        assertTabSelected(Tab.alerts, "Alerts")
        sleep(2)
        snap(named: "05-Notifications")
    }

    @MainActor
    func testCapture06_Settings() throws {
        launch(extra: ["UI_TEST_TAB=settings"])
        waitForMainUI()
        assertTabSelected(Tab.settings, "Settings")
        sleep(1)
        snap(named: "06-Settings")
    }

    // MARK: - 设备差异

    /// tab 用 **SF Symbol 名**定位，不用序号、不用标题、也不经过 `tabBars`。
    ///
    /// 三个依赖是逐个踩掉的：
    ///
    /// 1. **不用标题**（`app.staticTexts["Alerts"]`）——标题是翻译过的，跑非
    ///    英文语言时会全部失败。
    /// 2. **不用序号**——iPhone 四个 tab、iPad 六个，Alerts 在两边分别是 2 和 4。
    /// 3. **不经过 `tabBars`**——iPad 上**根本没有**这个元素。2026-09-04 让测试
    ///    把界面层级打回来才看清：iPadOS 26 的 TabView 渲染成一个普通 `Other`
    ///    容器装着六个 `Button`，每个 Button 的 identifier 就是 SF Symbol 名：
    ///
    ///        Button, identifier: 'chart.bar.fill', label: 'Dashboard', Selected
    ///        Button, identifier: 'bell.fill',      label: 'Alerts'
    ///
    /// identifier 来自 `MainTabView` 的 `Label(_, systemImage:)`，两种设备共用
    /// 同一批符号，且与语言无关。
    private enum Tab {
        static let dashboard = "chart.bar.fill"
        static let browse    = "square.grid.2x2.fill"   // 仅 iPhone
        static let listings  = "list.bullet"            // 仅 iPad
        static let map       = "map.fill"               // 仅 iPad
        static let calendar  = "calendar"               // 仅 iPad
        static let alerts    = "bell.fill"
        static let settings  = "gear"
    }

    private var isPad: Bool { UIDevice.current.userInterfaceIdiom == .pad }

    /// 三个浏览视图在当前设备上怎么到达：(launch args, 目标 tab 的 symbol)。
    ///
    /// iPhone 上它们是 Browse 的三个子模式；iPad 上各占一个 tab。
    private func browse(_ mode: String, padTab: String) -> ([String], String) {
        isPad
            ? (["UI_TEST_TAB=\(mode)"], padTab)
            : (["UI_TEST_TAB=browse", "UI_TEST_BROWSE_MODE=\(mode)"], Tab.browse)
    }

    // MARK: - Helpers

    /// Launch App with screenshot mode + per-test extra args.
    /// 不在 setUp 里 launch 是因为不同 test 需要不同 args（tab/mode/login）。
    private func launch(extra: [String]) {
        var args = ["UI_TEST_SCREENSHOT_MODE"]
        args.append(contentsOf: extra)
        // 凭据从环境变量取，**不写在代码里**——这个仓库是公开的。
        // CI 由 GitHub secrets 注入；本地不设就自动退回访客模式。
        let env = ProcessInfo.processInfo.environment
        if let u = env["UI_TEST_USERNAME"], let p = env["UI_TEST_PASSWORD"],
           !u.isEmpty, !p.isEmpty {
            args += ["UI_TEST_USER=\(u)", "UI_TEST_PASS=\(p)"]
        }
        // 语言不在这里设——由 Screenshots.xctestplan 的 configuration 决定。
        //
        // 原先是读 UI_TEST_LOCALE 再拼 -AppleLanguages。那条路踩过一个坑：
        // xcodebuild 不转发不带 TEST_RUNNER_ 前缀的环境变量，变量根本到不了这
        // 里，于是五种语言跑出五套一模一样的英文截图，而张数和尺寸全合格。
        // 交给 test plan 之后，语言是构建配置的一部分，传不到就直接跑不起来。
        app.launchArguments = args
        app.launch()
    }

    /// 等主 UI 出现（tab bar 渲染完成）。
    /// LoginView 不会有 tab bar，所以 Login test 不调这个。
    /// 失败时把界面层级打出来。
    ///
    /// iPad 上 `app.tabBars` 不存在（iPadOS 26 的 TabView 渲染成别的东西），
    /// 而错误信息只说「tab bar 未出现」，不说它到底叫什么。云端跑一轮一小时，
    /// 靠猜元素类型的代价太高——让它把实际的层级报回来，一轮就知道。
    private func dumpHierarchy() -> String {
        String(app.debugDescription.prefix(3000))
    }

    private func waitForMainUI() {
        // 等 Dashboard 那个 tab 按钮出现，而不是等 `tabBars`——后者在 iPad 上
        // 不存在，60 秒等下来只会得到一句「tab bar 未出现」。
        let anchor = app.buttons[Tab.dashboard].firstMatch
        XCTAssertTrue(anchor.waitForExistence(timeout: 60),
                      "主界面未在 60s 内出现。当前界面层级：\n" + dumpHierarchy())
    }

    /// 断言选中的是 `symbol` 那个 tab。
    ///
    /// 「测试通过」和「拍对了」是两回事：访客模式下没有 Notifications 这个 tab，
    /// ``UI_TEST_TAB=notifications`` 把 selectedTab 设成一个不存在的值，SwiftUI
    /// 静默回落到第一个 tab，于是拍出一张名叫 05-Notifications、内容却是
    /// Dashboard 的图——尺寸正确、渲染完整，只有内容是错的。下游 verify 只查张数
    /// 和像素，查不出来。
    ///
    /// 定位方式换过三轮，见 ``Tab`` 的注释。
    private func assertTabSelected(_ symbol: String, _ label: String) {
        let button = app.buttons[symbol].firstMatch
        XCTAssertTrue(button.waitForExistence(timeout: 60),
                      "找不到「\(label)」这个 tab（\(symbol)）。当前界面层级：\n"
                      + dumpHierarchy())
        guard button.exists else { return }
        XCTAssertTrue(button.isSelected,
                      "选中的不是「\(label)」——launch arg 没生效，而截图会照拍不误")
    }

    /// 保存当前屏幕为 XCTAttachment，跟测试结果一起进 .xcresult 包。
    private func snap(named step: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        // 名字里不再带语言：语言由 test plan 决定，附件的 configurationName
        // 已经带着它，提取脚本按那个分桶。名字里再写一份只会有机会写错。
        let device = UIDevice.current.name.replacingOccurrences(of: " ", with: "-")
        attachment.name = "\(step)_\(device)"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
