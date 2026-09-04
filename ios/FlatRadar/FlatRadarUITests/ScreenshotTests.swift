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
        snap(named: "00-Login")
    }

    @MainActor
    func testCapture01_Dashboard() throws {
        launch(extra: ["UI_TEST_TAB=dashboard"])
        waitForMainUI()
        assertTabSelected(0, "Dashboard")
        // 给 Dashboard chart 渲染
        sleep(2)
        snap(named: "01-Dashboard")
    }

    @MainActor
    func testCapture02_Listings() throws {
        let (args, idx) = browse("list", padIndex: 1)
        launch(extra: args)
        waitForMainUI()
        assertTabSelected(idx, "Listings")
        sleep(2)
        snap(named: "02-Listings")
    }

    @MainActor
    func testCapture03_Map() throws {
        let (args, idx) = browse("map", padIndex: 2)
        launch(extra: args)
        waitForMainUI()
        assertTabSelected(idx, "Map")
        // Leaflet 渲染稍慢
        sleep(3)
        snap(named: "03-Map")
    }

    @MainActor
    func testCapture04_Calendar() throws {
        let (args, idx) = browse("calendar", padIndex: 3)
        launch(extra: args)
        waitForMainUI()
        assertTabSelected(idx, "Calendar")
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
        let bar = app.tabBars.firstMatch
        if bar.waitForExistence(timeout: 60), bar.buttons.count > alertsIndex {
            bar.buttons.element(boundBy: alertsIndex).tap()
        }
        assertTabSelected(alertsIndex, "Alerts")
        sleep(2)
        snap(named: "05-Notifications")
    }

    @MainActor
    func testCapture06_Settings() throws {
        launch(extra: ["UI_TEST_TAB=settings"])
        waitForMainUI()
        assertTabSelected(settingsIndex, "Settings")
        sleep(1)
        snap(named: "06-Settings")
    }

    // MARK: - 设备差异

    /// iPad 与 iPhone 的 tab 结构不同，截图脚本必须按设备分别取值。
    ///
    /// - iPhone（4 个）：Dashboard / Browse / Alerts / Settings
    ///   List、Map、Calendar 是 Browse 里的三个子模式，靠 UI_TEST_BROWSE_MODE 选。
    /// - iPad（6 个）：Dashboard / Listings / Map / Calendar / Alerts / Settings
    ///   三个视图各占一个 tab，没有 Browse。
    ///
    /// 2026-09-04 实测：iPad job 七张只出了 00-Login。原因就是这里——
    /// `UI_TEST_TAB=browse` 在 iPad 上落不了位，而断言用的又是 iPhone 的序号
    /// （Alerts=2、Settings=3，iPad 上是 4 和 5）。
    private var isPad: Bool { UIDevice.current.userInterfaceIdiom == .pad }

    /// 三个浏览视图在当前设备上怎么到达：(launch args, tab 序号)。
    private func browse(_ mode: String, padIndex: Int) -> ([String], Int) {
        isPad
            ? (["UI_TEST_TAB=\(mode)"], padIndex)
            : (["UI_TEST_TAB=browse", "UI_TEST_BROWSE_MODE=\(mode)"], 1)
    }

    private var alertsIndex: Int { isPad ? 4 : 2 }
    private var settingsIndex: Int { isPad ? 5 : 3 }

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

    /// 断言选中的是第 `index` 个 tab。
    ///
    /// 为什么按序号而不按标题
    /// ----------------------
    /// 「测试通过」和「拍对了」是两回事：访客模式下 tab bar 里没有
    /// Notifications，``UI_TEST_TAB=notifications`` 把 selectedTab 设成一个不
    /// 存在的值，SwiftUI 静默回落到第一个 tab，于是拍出一张名叫
    /// 05-Notifications、内容却是 Dashboard 的图——尺寸正确、渲染完整，只有内容
    /// 是错的。
    ///
    /// 第一版按导航栏标题断言（``app.staticTexts["Alerts"]``）。那样写在跑非英
    /// 文语言时会**全部失败**——标题本身是翻译过的。序号与语言无关。
    ///
    /// 登录后的 tab 顺序：0 Dashboard / 1 Browse / 2 Alerts / 3 Settings。
    /// 访客没有 Alerts，只有三个。
    private func assertTabSelected(_ index: Int, _ label: String) {
        let bar = app.tabBars.firstMatch
        XCTAssertTrue(bar.waitForExistence(timeout: 60), "tab bar 未出现")
        let buttons = bar.buttons
        XCTAssertTrue(index < buttons.count,
                      "tab bar 只有 \(buttons.count) 个 tab，取不到第 \(index) 个"
                      + "（\(label)）——多半是没登录成功，访客看不到 Alerts")
        guard index < buttons.count else { return }
        XCTAssertTrue(buttons.element(boundBy: index).isSelected,
                      "选中的不是第 \(index) 个 tab（\(label)）"
                      + "——launch arg 没生效，而截图会照拍不误")
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
