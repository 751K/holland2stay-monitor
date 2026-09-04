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
        XCTAssertFalse(tabQuery(.dashboard).firstMatch.exists,
                       "登录页上不该有主界面的 tab——说明它自动登录了。"
                       + "当前按钮清单：\n" + buttonInventory())
        snap(named: "00-Login")
    }

    @MainActor
    func testCapture01_Dashboard() throws {
        launch(extra: ["UI_TEST_TAB=dashboard"])
        waitForMainUI()
        selectTab(Tab.dashboard, "Dashboard")
        // 给 Dashboard chart 渲染
        sleep(2)
        snap(named: "01-Dashboard")
    }

    @MainActor
    func testCapture02_Listings() throws {
        let (args, tab) = browse("list", padTab: Tab.listings)
        launch(extra: args)
        waitForMainUI()
        selectTab(tab, "Listings")
        sleep(2)
        snap(named: "02-Listings")
    }

    @MainActor
    func testCapture03_Map() throws {
        let (args, tab) = browse("map", padTab: Tab.map)
        launch(extra: args)
        waitForMainUI()
        selectTab(tab, "Map")
        // Leaflet 渲染稍慢
        sleep(3)
        snap(named: "03-Map")
    }

    @MainActor
    func testCapture04_Calendar() throws {
        let (args, tab) = browse("calendar", padTab: Tab.calendar)
        launch(extra: args)
        waitForMainUI()
        selectTab(tab, "Calendar")
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
        // 这条最早改成「直接点」，现在其余几条也走同一个 selectTab。
        selectTab(Tab.alerts, "Alerts")
        sleep(2)
        snap(named: "05-Notifications")
    }

    @MainActor
    func testCapture06_Settings() throws {
        launch(extra: ["UI_TEST_TAB=settings"])
        waitForMainUI()
        selectTab(Tab.settings, "Settings")
        sleep(1)
        snap(named: "06-Settings")
    }

    // MARK: - 设备差异

    /// 一个 tab 的两种定位方式。
    ///
    /// 定位方式换过四轮，每一轮都是被上一轮的盲区打回来的：
    ///
    /// 1. **标题**（`app.staticTexts["Alerts"]`）——标题是翻译过的，非英文语言
    ///    下全线失败。
    /// 2. **序号**——iPhone 四个 tab、iPad 六个，Alerts 分别在 2 和 4。
    /// 3. **SF Symbol 名**——iPadOS 26 的 TabView 渲染成一个普通 `Other` 容器
    ///    装着六个 `Button`，Button 的 identifier 恰好是 symbol 名：
    ///
    ///        Button, identifier: 'chart.bar.fill', label: 'Dashboard', Selected
    ///
    ///    这是从 iPad 的层级 dump 里读出来的，然后我把它当成了两种设备的共同
    ///    事实。**iPhone 上不成立。** build 262 里 iPhone 七条挂了六条，全是
    ///    「主界面未在 60s 内出现」——而层级里明明有 'Panel de control'，
    ///    人早就登录进主界面了，只是这个锚点找不到。改成 symbol 之前用的
    ///    `app.tabBars` 在 iPhone 上一直是好的，坏的只有 iPad；我修好 iPad 的
    ///    同时打断了 iPhone，因为我拿单设备的证据当了普遍事实。
    /// 4. **App 自己声明的 identifier**——`MainTabView` 给每个 tabItem 挂了
    ///    `.accessibilityIdentifier("tab-…")`。不依赖任何一种设备把 Label 渲染
    ///    成什么，两边都是同一个字符串。symbol 作为兜底留着，万一某个系统版本
    ///    不把 identifier 透出来，iPad 那条路还在。
    private struct Tab {
        let id: String
        let symbol: String

        static let dashboard = Tab(id: "tab-dashboard", symbol: "chart.bar.fill")
        static let browse    = Tab(id: "tab-browse",    symbol: "square.grid.2x2.fill")
        static let listings  = Tab(id: "tab-listings",  symbol: "list.bullet")
        static let map       = Tab(id: "tab-map",       symbol: "map.fill")
        static let calendar  = Tab(id: "tab-calendar",  symbol: "calendar")
        static let alerts    = Tab(id: "tab-alerts",    symbol: "bell.fill")
        static let settings  = Tab(id: "tab-settings",  symbol: "gear")
    }

    private var isPad: Bool { UIDevice.current.userInterfaceIdiom == .pad }

    /// 三个浏览视图在当前设备上怎么到达：(launch args, 目标 tab 的 symbol)。
    ///
    /// iPhone 上它们是 Browse 的三个子模式；iPad 上各占一个 tab。
    private func browse(_ mode: String, padTab: Tab) -> ([String], Tab) {
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
        // 截图现在只在 Xcode Cloud 上跑，值由 ci_scripts/ci_post_clone.sh
        // 写进 test plan 的 environmentVariableEntries（Xcode Cloud 禁止
        // TEST_RUNNER_ 前缀，而 xcodebuild 只转发这个前缀，那个脚本里写了
        // 完整缘由）。本地不设就自动退回访客模式。
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

    /// 一个 tab 按钮：App 声明的 identifier 优先，SF Symbol 兜底。
    ///
    /// 用 identifier 上的谓词而不是两次 `app.buttons[...]`：两种写法都能匹配，
    /// 但谓词只需要**一次**等待就同时覆盖两条路，不必先把一条等超时。
    private func tabQuery(_ tab: Tab) -> XCUIElementQuery {
        app.buttons.matching(
            NSPredicate(format: "identifier == %@ OR identifier == %@",
                        tab.id, tab.symbol))
    }

    /// 失败时打印的诊断。**必须短，而且要点在最前面。**
    ///
    /// 上一版打的是 `app.debugDescription.prefix(3000)`——整棵界面层级的前 3000
    /// 字符。iPhone 上那三千字符全是嵌套的 `Other` 容器，tab bar 在树的末尾，
    /// 一个字都没印到。更糟的是 App Store Connect 的 issues 接口把失败信息截在
    /// 三千字符左右，所以「多打一点」这条路本来就是堵死的：跑完一整轮，仍然
    /// 不知道那些按钮到底叫什么。
    ///
    /// 换成只印按钮清单——正好是要回答的那个问题，而且短到不会被截断。
    private func buttonInventory() -> String {
        var lines = ["tabBars=\(app.tabBars.count)"]
        for bar in app.tabBars.allElementsBoundByIndex {
            for b in bar.buttons.allElementsBoundByIndex {
                lines.append("  [tabBar] id=\(b.identifier.debugDescription) "
                             + "label=\(b.label.debugDescription) sel=\(b.isSelected)")
            }
        }
        let all = app.buttons.allElementsBoundByIndex
        lines.append("app.buttons=\(all.count)")
        for b in all.prefix(24) {
            lines.append("  id=\(b.identifier.debugDescription) "
                         + "label=\(b.label.debugDescription) sel=\(b.isSelected)")
        }
        if all.count > 24 { lines.append("  …(还有 \(all.count - 24) 个)") }
        return lines.joined(separator: "\n")
    }

    /// **任意**一个 tab 按钮。
    ///
    /// 「主界面出来了没有」不能盯住某一个具体的 tab。iPad 的六个 tab 是分页的：
    /// App 直接落在 Settings 上时，tab bar 显示的是后半页，dashboard 在「上一页」
    /// 里，根本不进无障碍树。盯着 dashboard 等，就会在主界面明明已经渲染完的
    /// 情况下等满 60 秒——build 265 的 06-Settings 就是这么挂的，而清单里
    /// tab-settings 正亮着 sel=true。
    private var anyTabQuery: XCUIElementQuery {
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "tab-"))
    }

    /// 等主 UI 出现。LoginView 上没有 tab，所以 Login 那条不调这个。
    private func waitForMainUI() {
        XCTAssertTrue(anyTabQuery.firstMatch.waitForExistence(timeout: 60),
                      "主界面未在 60s 内出现。当前按钮清单：\n" + buttonInventory())
    }

    /// 落到指定 tab：launch arg 没生效就直接点它。
    ///
    /// `UI_TEST_TAB=` 这条路是不可靠的——登录是异步的，tab 是同步设的，
    /// MainTabView 挂载时又会把 selectedTab 读回默认值。表现是随机的：build 265
    /// 里 03-Map 第一次失败、重跑通过，02-Listings 两次都停在 Dashboard。
    ///
    /// Notifications 那条早就改成直接点了，一直很稳。这里把同样的做法推广到
    /// 其余几条：等按钮出现 → 没选中就点一下 → 再断言。断言留着不动，
    /// 「点了但没落位」仍然要红——点击是让它更可能对，不是替代验证。
    private func selectTab(_ tab: Tab, _ label: String) {
        let button = tabQuery(tab).firstMatch
        XCTAssertTrue(button.waitForExistence(timeout: 60),
                      "找不到「\(label)」这个 tab（\(tab.id) / \(tab.symbol)）。"
                      + "当前按钮清单：\n" + buttonInventory())
        guard button.exists else { return }
        if !button.isSelected {
            guard button.isHittable else {
                XCTFail("「\(label)」这个 tab 在但点不到——多半在 tab bar 的另一页上。"
                        + "当前按钮清单：\n" + buttonInventory())
                return
            }
            button.tap()
        }
        assertTabSelected(tab, label)
    }

    /// 断言选中的是 `tab`。
    ///
    /// 「测试通过」和「拍对了」是两回事：访客模式下没有 Notifications 这个 tab，
    /// ``UI_TEST_TAB=notifications`` 把 selectedTab 设成一个不存在的值，SwiftUI
    /// 静默回落到第一个 tab，于是拍出一张名叫 05-Notifications、内容却是
    /// Dashboard 的图——尺寸正确、渲染完整，只有内容是错的。下游 verify 只查张数
    /// 和像素，查不出来。
    private func assertTabSelected(_ tab: Tab, _ label: String) {
        let button = tabQuery(tab).firstMatch
        XCTAssertTrue(button.waitForExistence(timeout: 60),
                      "找不到「\(label)」这个 tab（\(tab.id) / \(tab.symbol)）。"
                      + "当前按钮清单：\n" + buttonInventory())
        guard button.exists else { return }
        // 失败信息里要带上「当时选中的到底是哪个」：第一版只写了一句「选中的
        // 不是 Listings」，云端跑一轮回来只有 63 个字，还得再猜一轮。
        let selected = app.buttons.allElementsBoundByIndex
            .filter { $0.exists && $0.isSelected }
            .map { $0.identifier.isEmpty ? $0.label : $0.identifier }
        XCTAssertTrue(button.isSelected,
                      "选中的不是「\(label)」（\(tab.id)）——launch arg 没生效，"
                      + "而截图会照拍不误。当前选中的是：\(selected)\n"
                      + buttonInventory())
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
