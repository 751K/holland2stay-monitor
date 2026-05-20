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
        launch(extra: ["UI_TEST_TAB=notifications"])
        waitForMainUI()
        sleep(2)
        snap(named: "05-Notifications")
    }

    @MainActor
    func testCapture06_Settings() throws {
        launch(extra: ["UI_TEST_TAB=settings"])
        waitForMainUI()
        sleep(1)
        snap(named: "06-Settings")
    }

    // MARK: - Helpers

    /// Launch App with screenshot mode + per-test extra args.
    /// 不在 setUp 里 launch 是因为不同 test 需要不同 args（tab/mode/login）。
    private func launch(extra: [String]) {
        var args = ["UI_TEST_SCREENSHOT_MODE"]
        args.append(contentsOf: extra)
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
        XCTAssertTrue(tabs.waitForExistence(timeout: 15), "tab bar 未在 15s 内出现")
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
