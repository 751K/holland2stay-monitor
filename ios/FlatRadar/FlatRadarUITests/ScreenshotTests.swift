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
        snap(named: "00-Login")
    }

    @MainActor
    func testCapture01_Dashboard() throws {
        launch(extra: ["UI_TEST_TAB=dashboard"])
        waitForMainUI()
        assertOnScreen("Dashboard")
        // 给 Dashboard chart 渲染
        sleep(2)
        snap(named: "01-Dashboard")
    }

    @MainActor
    func testCapture02_Listings() throws {
        launch(extra: ["UI_TEST_TAB=browse", "UI_TEST_BROWSE_MODE=list"])
        waitForMainUI()
        sleep(2)
        snap(named: "02-Listings")
    }

    @MainActor
    func testCapture03_Map() throws {
        launch(extra: ["UI_TEST_TAB=browse", "UI_TEST_BROWSE_MODE=map"])
        waitForMainUI()
        // Leaflet 渲染稍慢
        sleep(3)
        snap(named: "03-Map")
    }

    @MainActor
    func testCapture04_Calendar() throws {
        launch(extra: ["UI_TEST_TAB=browse", "UI_TEST_BROWSE_MODE=calendar"])
        waitForMainUI()
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
        assertOnScreen("Alerts")
        sleep(2)
        snap(named: "05-Notifications")
    }

    @MainActor
    func testCapture06_Settings() throws {
        launch(extra: ["UI_TEST_TAB=settings"])
        waitForMainUI()
        assertOnScreen("Settings")
        sleep(1)
        snap(named: "06-Settings")
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
        // locale 从环境变量传，给跨语言批量截图用
        if let locale = ProcessInfo.processInfo.environment["UI_TEST_LOCALE"] {
            args += ["-AppleLanguages", "(\(locale))", "-AppleLocale", locale]
        }
        app.launchArguments = args
        app.launch()
    }

    /// 等主 UI 出现（tab bar 渲染完成）。
    /// LoginView 不会有 tab bar，所以 Login test 不调这个。
    private func waitForMainUI() {
        let tabs = app.tabBars.firstMatch
        // 60 而不是 15：CI 的 runner 上一次冷启动就要一分多钟，15 秒是按本机
        // 真机调的。第一次跑云端时 Listings 就死在
        // "Timed out while evaluating UI query"。
        XCTAssertTrue(tabs.waitForExistence(timeout: 60), "tab bar 未在 60s 内出现")
    }

    /// 断言当前确实停在 `title` 那一屏。
    ///
    /// 为什么要有这一步
    /// ----------------
    /// 「测试通过」和「拍对了」是两回事。访客模式下 tab bar 里没有
    /// Notifications，``UI_TEST_TAB=notifications`` 把 selectedTab 设成一个不
    /// 存在的值，SwiftUI 静默回落到第一个 tab——于是拍出一张名叫
    /// 05-Notifications、内容却是 Dashboard 的图。测试通过、尺寸正确、渲染
    /// 完整，只有内容是错的；而下游 verify 只查张数和像素，查不出来。
    private func assertOnScreen(_ title: String, timeout: TimeInterval = 30) {
        let heading = app.staticTexts[title]
        XCTAssertTrue(heading.waitForExistence(timeout: timeout),
                      "没有停在「\(title)」那一屏——多半是 launch arg 没生效，"
                      + "而截图会照拍不误")
    }

    /// 保存当前屏幕为 XCTAttachment，跟测试结果一起进 .xcresult 包。
    private func snap(named step: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        let locale = ProcessInfo.processInfo.environment["UI_TEST_LOCALE"] ?? "en-US"
        let device = UIDevice.current.name.replacingOccurrences(of: " ", with: "-")
        attachment.name = "\(step)_\(device)_\(locale)"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
