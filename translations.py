"""
UI translations for FlatRadar web panel.
Add new strings as key: {zh, en} pairs. Templates use _(key) to look up.
"""
import logging

logger = logging.getLogger(__name__)

TRANSLATIONS = {
    # ── Layout / Navigation ──────────────────────────────
    "app_title":            {"zh": "FlatRadar",               "en": "FlatRadar"},
    "skip_to_content":      {"zh": "跳到主要内容",               "en": "Skip to content"},
    "nav_section_admin":    {"zh": "管理",                      "en": "Admin"},
    "dashboard":            {"zh": "仪表盘",                  "en": "Dashboard"},
    "listings":             {"zh": "房源列表",                "en": "Listings"},
    "calendar":             {"zh": "日历",                    "en": "Calendar"},
    "stats":                {"zh": "统计",                    "en": "Statistics"},
    "users":                {"zh": "用户",                    "en": "Users"},
    "user_new_title":       {"zh": "新增用户",                "en": "New User"},
    "user_edit_title":      {"zh": "编辑用户",                "en": "Edit User"},
    "settings":             {"zh": "设置",                    "en": "Settings"},
    "notifications":        {"zh": "通知",                    "en": "Notifications"},
    "mark_all_read":        {"zh": "全部已读",                "en": "Mark all read"},
    "no_notifications":     {"zh": "暂无通知",                "en": "No notifications"},
    "toggle_theme":         {"zh": "切换主题",                "en": "Toggle theme"},
    "logout":               {"zh": "退出登录",                "en": "Logout"},
    "language":             {"zh": "语言",                    "en": "Language"},

    # ── Monitor badge ────────────────────────────────────
    "monitor_running":      {"zh": "监控运行中",              "en": "Monitor running"},
    "monitor_stopped":      {"zh": "监控未启动",              "en": "Monitor stopped"},
    "monitor_paused_short": {"zh": "系统暂停",                "en": "System paused"},
    "monitor_paused_title": {"zh": "系统监控已暂停",           "en": "Monitoring is paused"},
    "monitor_paused_hint":  {"zh": "后台监控当前未运行；新房源、状态变更通知和自动预订都会暂停，直到管理员重新启动监控。",
                             "en": "The background monitor is not running. New listing alerts, status updates, and auto-booking are paused until an admin starts it again."},
    "monitor_control_hint": {"zh": "管理员控制",              "en": "Admin controls"},
    "monitor_start":        {"zh": "启动监控",                "en": "Start monitor"},
    "monitor_stop":         {"zh": "暂停监控",                "en": "Pause monitor"},
    "monitor_checking":     {"zh": "检测中",                  "en": "Checking"},
    "upstream_maintenance_title":   {"zh": "Holland2Stay 平台维护中",
                                     "en": "Holland2Stay under maintenance"},
    "upstream_maintenance_hint":    {"zh": "对方运维窗口，监控已暂停；平台恢复后将自动继续。",
                                     "en": "Upstream is performing scheduled maintenance. Monitor will auto-resume."},
    "upstream_maintenance_since":   {"zh": "自",                      "en": "Since"},
    "system_info":          {"zh": "系统信息",                "en": "System Info"},
    "process_status":       {"zh": "进程状态",                "en": "Process Status"},
    "monitor_process":      {"zh": "监控进程",                "en": "Monitor Process"},
    "web_process":          {"zh": "Web 进程",                "en": "Web Process"},
    "database_stats":       {"zh": "数据库统计",              "en": "Database Stats"},
    "status_changes":       {"zh": "状态变更记录",            "en": "Status Changes"},
    "web_notifications":    {"zh": "Web 通知",                "en": "Web Notifications"},
    "unread":               {"zh": "未读",                    "en": "unread"},
    "configuration":        {"zh": "配置",                    "en": "Configuration"},
    "environment":          {"zh": "系统环境",                "en": "Environment"},
    "total_users":          {"zh": "用户总数",                "en": "Total Users"},
    "active_users":         {"zh": "已启用",                  "en": "Active"},
    "check_interval":       {"zh": "常规轮询间隔",            "en": "Check Interval"},
    "peak_interval":        {"zh": "高峰期间隔",              "en": "Peak Interval"},
    "min_interval":         {"zh": "最小间隔",                "en": "Min Interval"},
    "log_level":            {"zh": "日志级别",                "en": "Log Level"},
    "last_count":           {"zh": "上次抓取数量",            "en": "Last Count"},
    "platform":             {"zh": "平台",                    "en": "Platform"},
    "cities":               {"zh": "城市",                    "en": "Cities"},
    "log_viewer":           {"zh": "日志查看",                "en": "Log Viewer"},

    # ── 崩溃报告页（admin） ──────────────────────────────
    "crashes_title":              {"zh": "崩溃报告",         "en": "Crash Reports"},
    "crashes_total_suffix":       {"zh": "份",               "en": "reports"},
    "crashes_empty":              {"zh": "目前没有崩溃报告 🎉", "en": "No crash reports 🎉"},
    "crashes_empty_hint":         {"zh": "iOS App 崩溃后用户授权上传的报告会保存在 ", "en": "Reports uploaded after user consent are saved to "},
    "crashes_list":               {"zh": "报告列表",         "en": "Reports"},
    "crashes_col_time":           {"zh": "接收时间 (UTC)",   "en": "Received (UTC)"},
    "crashes_col_kind":           {"zh": "类型",             "en": "Kind"},
    "crashes_col_user":           {"zh": "用户",             "en": "User"},
    "crashes_col_app":            {"zh": "App",              "en": "App"},
    "crashes_col_device":         {"zh": "设备",             "en": "Device"},
    "crashes_col_ios":            {"zh": "iOS",              "en": "iOS"},
    "crashes_col_actions":        {"zh": "操作",             "en": "Actions"},
    "crashes_detail_title":       {"zh": "完整 JSON",        "en": "Full JSON"},
    "crashes_confirm_delete":     {"zh": "确定删除这份崩溃报告？",   "en": "Delete this crash report?"},
    "crashes_confirm_bulk_delete":{"zh": "确定删除选中的 {n} 份报告？", "en": "Delete {n} selected reports?"},
    "crashes_bulk_delete":        {"zh": "批量删除",         "en": "Delete selected"},
    "logs_monitor":         {"zh": "运行日志",                "en": "Monitor Log"},
    "logs_errors":          {"zh": "错误日志",                "en": "Errors Log"},
    "logs_web":             {"zh": "Web 日志",               "en": "Web Log"},
    "logs_search":          {"zh": "搜索关键词…",             "en": "Search keywords…"},
    "logs_level_all":       {"zh": "全部级别",                "en": "All levels"},
    # 提示串必须给全格式：_parse_log_time() 要求 YYYY-MM-DD 开头，
    # 写成 08-03 会被直接判为无效并静默忽略整个时间过滤。
    "logs_since_hint":      {"zh": "起始 2026-08-03 03:00",   "en": "From 2026-08-03 03:00"},
    "logs_until_hint":      {"zh": "结束 2026-08-03 04:00",   "en": "To 2026-08-03 04:00"},
    "logs_filter_reset":    {"zh": "重置",                    "en": "Reset"},
    "logs_no_match":        {"zh": "没有匹配的日志",           "en": "No matching logs"},
    "logs_scan_truncated":  {"zh": "仅扫描了最后",             "en": "scanned only last"},
    "logs_auto_scroll":     {"zh": "自动滚动",                "en": "Auto-scroll"},
    "pause":                {"zh": "暂停刷新",                "en": "Pause"},
    "loading":              {"zh": "加载中...",               "en": "Loading..."},
    "no_logs_yet":          {"zh": "暂无日志",                "en": "No logs yet"},
    "clear_logs":           {"zh": "清空当前日志",            "en": "Clear current log"},
    "clear_logs_confirm":   {"zh": "确定要清空当前日志文件吗？", "en": "Delete all entries in this log file?"},
    # ── Xior 按楼栋账号 ────────────────────────────────────
    "user_form_xior_note": {
        "zh": "Xior 每栋楼是独立的门户、独立账号。为哪栋楼填了账号，才会对那栋楼自动预订；"
              "账号需自行在该楼的 RENTCafe 页面注册。",
        "en": "Each Xior building is a separate portal with its own account. Auto-booking only "
              "runs for buildings you add credentials for; register on that building's RENTCafe site first.",
    },
    "user_form_xior_pick":   {"zh": "选择楼栋…",        "en": "Pick a building…"},
    "user_form_xior_add":    {"zh": "添加楼栋账号",      "en": "Add building"},
    "user_form_xior_remove": {"zh": "移除该楼账号",      "en": "Remove"},
    "user_form_xior_none_monitored": {
        "zh": "当前未监控任何 Xior 楼栋（见 .env 的 XIOR_CITIES）",
        "en": "No Xior buildings are being monitored (see XIOR_CITIES in .env)",
    },
    # ── 申请人档案（RENTCafe Applicant Info 自动填）────────
    "user_form_profile": {"zh": "申请人档案", "en": "Applicant profile"},
    # 这一块归 Xior 和 OurDomain 共用：两者都是 RENTCafe，申请表由
    # bookers/rentcafe.py 一份代码填（XiorBooker / OurDomainBooker 都继承它）。
    "user_form_profile_scope": {"zh": "Xior 与 OurDomain 共用",
                                "en": "Shared by Xior and OurDomain"},
    "user_form_profile_note": {
        # 这里是纯文本，模板不做 Markdown 渲染——写 **粗体** 只会把星号
        # 原样显示出来
        "zh": "半自动预订用这些资料自动填 RENTCafe 的申请表，并代传证件——"
              "平台在证件上传前拒绝保存申请表，而抢房是自动触发的。"
              "系统只填表，不付款：付款要填银行账号，那一步必须你自己做。",
        "en": "Used to auto-fill the RENTCafe application form and upload your ID — "
              "the platform refuses to save anything until the ID is there, and booking "
              "fires automatically. The system never pays: the payment step needs "
              "your bank details and stays with you.",
    },
    "profile_title":           {"zh": "称谓",       "en": "Title"},
    "profile_first_name":      {"zh": "名",         "en": "First name"},
    "profile_middle_name":     {"zh": "中间名",     "en": "Middle name"},
    "profile_no_middle_name":  {"zh": "我没有中间名", "en": "I don't have a middle name"},
    "profile_last_name":       {"zh": "姓",         "en": "Last name"},
    "profile_phone":           {"zh": "电话",       "en": "Phone"},
    "profile_gender":          {"zh": "性别",       "en": "Gender"},
    "profile_dob":             {"zh": "出生日期",   "en": "Date of birth"},
    "profile_nationality":     {"zh": "国籍",       "en": "Nationality"},
    "profile_country":         {"zh": "当前所在国", "en": "Country"},
    "profile_address":         {"zh": "地址",       "en": "Address"},
    "profile_postcode_city":   {"zh": "邮编 + 城市", "en": "Post code + city"},
    "profile_university":      {"zh": "就读大学",   "en": "University"},
    "profile_min_lease_term":  {"zh": "最短租期（月）", "en": "Min lease term (months)"},
    "profile_place_of_birth":  {"zh": "出生地（国家）", "en": "Place of birth (country)"},
    "profile_id_number":       {"zh": "证件号（护照/身份证）", "en": "ID / passport number"},
    "profile_student_number":  {"zh": "学号",         "en": "Student number"},
    # 2026-08-03 对着真实申请表补的字段（地址原来一格塞两样，对不上表单的四格）
    "profile_address_line2":   {"zh": "地址第二行（选填）", "en": "Address line 2 (optional)"},
    "profile_postcode":        {"zh": "邮编",       "en": "Post code"},
    "profile_city":            {"zh": "城市",       "en": "City"},
    "profile_housing_type":    {"zh": "当前住所性质", "en": "Current housing"},
    "profile_id_country":      {"zh": "证件签发国", "en": "ID issuing country"},
    # 背景调查三问：申请表上的原文提问，系统不替用户作答
    "profile_ever_evicted":    {"zh": "是否曾被驱逐出租住房？", "en": "Have you ever been evicted?"},
    "profile_ever_convicted":  {"zh": "是否曾被定罪？", "en": "Have you ever been convicted of a felony?"},
    "profile_criminal_charges": {"zh": "是否有未决刑事指控？", "en": "Any pending criminal charges?"},
    "profile_id_doc":        {"zh": "护照 / 身份证扫描件",
                              "en": "Passport / ID document"},
    "profile_id_doc_delete": {"zh": "删除已上传的证件", "en": "Delete stored document"},
    "profile_id_doc_hint": {
        "zh": "平台在证件上传前拒绝保存申请表，而抢房是系统自动触发的，"
              "所以文件需要提前存好（加密落盘）。≤5MB，支持 pdf/jpg/png/doc 等。",
        "en": "The platform refuses to save the application until the ID is "
              "uploaded, and booking fires automatically, so the file must be "
              "stored in advance (encrypted at rest). Max 5MB.",
    },
    "profile_consent_label": {
        "zh": "授权系统代我勾选申请表上的信用/背景调查声明",
        "en": "Authorise the system to accept the screening declarations on my behalf",
    },
    "profile_consent_note": {
        "zh": "申请表上有两句法律声明：授权做信用/参考/背景调查，以及确认所填属实。"
              "不授权则系统只填表、不提交，需你自己在浏览器里勾选并保存。",
        "en": "The application carries two legal declarations: authorising a credit / "
              "reference / background check, and confirming the details are true. "
              "Without this, the system fills the form but will not submit it.",
    },
    "profile_consent_given_at": {"zh": "已于", "en": "Authorised at"},
    "profile_incomplete": {
        "zh": "档案不完整，半自动预订不会触发。还缺：",
        "en": "Profile incomplete — semi-automated booking will not run. Missing:",
    },
    # ── 公告群发 ──────────────────────────────────────────
    "announce_title":       {"zh": "发布公告",                 "en": "Announcement"},
    "announce_hint":        {"zh": "发给所有开启通知的用户",     "en": "Sent to all users with notifications on"},
    "announce_title_ph":    {"zh": "标题（必填）",              "en": "Title (required)"},
    "announce_body_ph":     {"zh": "正文",                     "en": "Body"},
    "announce_preview":     {"zh": "预览送达范围",              "en": "Preview reach"},
    "announce_send":        {"zh": "发送",                     "en": "Send"},
    "announce_need_title":  {"zh": "标题不能为空",              "en": "Title is required"},
    "announce_confirm":     {"zh": "确认发送？群发不可撤回。",    "en": "Send now? Broadcasts cannot be undone."},
    "announce_preview_result": {"zh": "将发给",                "en": "Would reach"},
    "announce_skipped":     {"zh": "已关通知跳过",              "en": "skipped (notifications off)"},
    "announce_sent":        {"zh": "已发送",                   "en": "Sent"},
    "announce_devices":     {"zh": "台设备",                   "en": "devices"},
    # ── 数据健康面板 ──────────────────────────────────────
    "monitoring_title":         {"zh": "数据健康",             "en": "Data Health"},
    "monitoring_sources":       {"zh": "各平台状态",           "en": "Per-source status"},
    "monitoring_recent_rounds": {"zh": "最近轮次",             "en": "Recent rounds"},
    # 「—」必须解释。Xior 按 source 节流后每 10 分钟才跑一轮，表里于是出现
    # 一长列「—」，看起来像这个平台挂了——实际只是那几轮没安排它。
    "monitoring_cell_hint":     {"zh": "格式：房源数（完整/任务）；「—」表示该轮未安排此平台",
                                 "en": "Format: listings (complete/targets); “—” = platform not scheduled that round"},
    "monitoring_round":         {"zh": "轮次",                 "en": "Round"},
    "monitoring_window":        {"zh": "判定窗口（轮）",        "en": "Window (rounds)"},
    "monitoring_heartbeat":     {"zh": "心跳",                 "en": "Heartbeat"},
    "monitoring_last_round":    {"zh": "最近一轮",             "en": "Last round"},
    "monitoring_last_listings": {"zh": "最近房源数",           "en": "Last listings"},
    "monitoring_completeness":  {"zh": "完整扫描率",           "en": "Completeness"},
    "monitoring_fail_streak":   {"zh": "连续失败",             "en": "Fail streak"},
    "monitoring_zero_streak":   {"zh": "连续零房源",           "en": "Zero streak"},
    "monitoring_last_success":  {"zh": "最近成功",             "en": "Last success"},
    "monitoring_no_data":       {"zh": "暂无遥测数据",          "en": "No telemetry yet"},
    "monitoring_times_in":      {"zh": "时间为",               "en": "times in"},
    "monitoring_status_ok":      {"zh": "正常",                "en": "OK"},
    "monitoring_status_warn":    {"zh": "注意",                "en": "Warn"},
    "monitoring_status_down":    {"zh": "故障",                "en": "Down"},
    "monitoring_status_unknown": {"zh": "无数据",              "en": "Unknown"},
    # ── Dashboard ────────────────────────────────────────
    # 文案要和 templates/system.html 里 scheduleReload() 的 30000ms 对上
    "auto_refresh":         {"zh": "每 30 秒自动刷新",         "en": "Auto-refresh every 30s"},
    "total_listings":       {"zh": "数据库房源",              "en": "DB listings"},
    "last_scrape":          {"zh": "最近抓取",                "en": "Last scrape"},
    "items_unit":           {"zh": "条",                     "en": ""},
    "items_total_prefix":   {"zh": "共 ",                    "en": ""},
    "recent_listings":      {"zh": "最新房源",                "en": "Recent listings"},
    "view_all":             {"zh": "查看全部",                "en": "View all"},
    "changes_48h":          {"zh": "近 48h 状态变更",         "en": "Status changes (48h)"},
    "no_data":              {"zh": "暂无数据",                "en": "No data"},
    "no_changes":           {"zh": "暂无变更",                "en": "No changes"},
    "dash_total_listings":  {"zh": "活跃房源总数",            "en": "Total Listings"},
    "dash_new_today_suffix":{"zh": "今日新增",                "en": "new today"},
    "dash_new_24h":         {"zh": "今日新增",                "en": "New (24h)"},
    "dash_changes_24h":     {"zh": "状态变更",                "en": "Changes (24h)"},
    "dash_items_per_run":   {"zh": "单次抓取量",              "en": "Items / run"},
    "dash_uptime":          {"zh": "持续运行时间",            "en": "System Uptime"},
    "dash_active_cities":   {"zh": "活跃城市",                "en": "Active Cities"},
    "dash_of_targets":      {"zh": "配置目标",                "en": "of"},
    "dash_cities_with_listings": {"zh": "个城市有房源",        "en": "with listings"},
    "dash_platforms":       {"zh": "支持平台",                "en": "Platforms"},
    "dash_of_total":        {"zh": "共",                     "en": "of"},
    "monitor_live":         {"zh": "运行中",                  "en": "Live"},
    "monitor_stopped_short":{"zh": "已停止",                  "en": "Stopped"},
    "dash_updated":         {"zh": "更新于",                  "en": "Updated"},
    "dash_refresh":         {"zh": "刷新",                    "en": "Refresh"},

    # ── Table columns ────────────────────────────────────
    "col_listing":          {"zh": "房源",                    "en": "Listing"},
    "col_status":           {"zh": "状态",                    "en": "Status"},
    "col_rent":             {"zh": "租金",                    "en": "Rent"},
    "col_area":             {"zh": "面积",                    "en": "Area"},
    "col_floor":            {"zh": "楼层",                    "en": "Floor"},
    "col_type":             {"zh": "户型",                    "en": "Type"},
    "col_occupancy":        {"zh": "入住",                    "en": "Occupancy"},
    "col_available":        {"zh": "可入住",                  "en": "Available"},
    "col_building":         {"zh": "楼盘",                    "en": "Building"},
    "col_city":             {"zh": "城市",                    "en": "City"},
    "col_source":           {"zh": "平台",                    "en": "Platform"},
    "col_first_seen":       {"zh": "首次发现",                "en": "First seen"},
    "col_last_seen":        {"zh": "最后出现",                "en": "Last seen"},
    "col_found":            {"zh": "发现",                    "en": "Found"},
    "col_change":           {"zh": "变更",                    "en": "Change"},
    "col_time":             {"zh": "时间",                    "en": "Time"},
    "col_name":             {"zh": "房源名称",                "en": "Name"},

    # ── Filter / Search ──────────────────────────────────
    "filter_status":        {"zh": "状态",                    "en": "Status"},
    "filter_all":           {"zh": "全部",                    "en": "All"},
    "filter_all_status":    {"zh": "全部状态",                "en": "All statuses"},
    "past_24h":             {"zh": "近 24 小时",              "en": "Past 24h"},
    "status_book":          {"zh": "可预订",                   "en": "Available"},
    "status_lottery":       {"zh": "可抽签",                   "en": "Lottery"},
    "status_reserved":      {"zh": "已预留",                   "en": "Reserved"},
    # 平台不会说「这个单元没了」，只是把它从列表里拿掉；这个状态是系统据此
    # 推出来的，不是平台确认的。两者可信度差得远，必须让用户看得出来。
    "status_inferred":      {"zh": "推测",                     "en": "Inferred"},
    "status_inferred_hint": {
        "zh": "平台已不再列出该房源，状态由系统推测，未经平台确认",
        "en": "No longer listed by the platform — status inferred, not confirmed",
    },
    "filter_city":          {"zh": "城市",                    "en": "City"},
    "filter_source":        {"zh": "平台",                    "en": "Platform"},
    "filter_name":          {"zh": "名称",                    "en": "Name"},
    "filter_max_rent":      {"zh": "最高租金",                "en": "Max rent"},
    "filter_min_area":      {"zh": "最小面积",                "en": "Min area"},
    "filter_all_cities":    {"zh": "全部城市",                "en": "All cities"},
    "filter_all_sources":   {"zh": "全部平台",                "en": "All platforms"},
    "filter_contract":      {"zh": "合同类型",                "en": "Contract"},
    "filter_all_contract":  {"zh": "全部合同",                "en": "All contracts"},
    "filter_type":          {"zh": "房型",                    "en": "Type"},
    "filter_occupancy":     {"zh": "入住人数",                "en": "Occupancy"},
    "filter_tenant":        {"zh": "租客要求",                "en": "Tenant"},
    "filter_all_tenant":    {"zh": "全部租客",                "en": "All tenants"},
    "filter_energy":        {"zh": "最低能耗",                "en": "Min energy"},
    "filter_min_energy":    {"zh": "不限",                    "en": "Any"},
    "filter_finishing":     {"zh": "装修类型",                "en": "Furnishing"},
    "filter_all_finishing": {"zh": "全部装修",                "en": "All"},
    "filter_search":        {"zh": "搜索",                    "en": "Search"},
    "filter_placeholder":   {"zh": "搜索房源名称…",           "en": "Search name…"},
    "filter_btn":           {"zh": "筛选",                    "en": "Filter"},
    "filter_clear":         {"zh": "清除",                    "en": "Clear"},
    "filter_no_results":    {"zh": "暂无房源数据",            "en": "No listings found"},

    # ── Map ──────────────────────────────────────────────
    "map_title":            {"zh": "地图",                    "en": "Map"},
    "map_loading":          {"zh": "加载中…",                 "en": "Loading…"},
    "map_geocode_btn":      {"zh": "解析地址",                 "en": "Geocode"},
    "map_geocode_hint":     {"zh": "解析所有未缓存地址",       "en": "Geocode all uncached addresses"},
    "map_count":            {"zh": "共 ",                     "en": ""},
    "map_listings_shown":   {"zh": " 套可展示房源",           "en": " listings shown"},
    "map_load_error":       {"zh": "地图数据加载失败",        "en": "Map data load failed"},

    # ── Calendar ─────────────────────────────────────────
    "cal_direct_book":      {"zh": "可直订",                  "en": "Direct book"},
    "cal_lottery":          {"zh": "摇号",                    "en": "Lottery"},
    "cal_select_date":      {"zh": "请选择日期",              "en": "Select a date"},
    "cal_click_hint":       {"zh": "点击日历中有房源的日期",    "en": "Click a date with listings"},
    "cal_load_error":       {"zh": "数据加载失败",            "en": "Failed to load data"},
    "cal_sets":             {"zh": "套",                     "en": " units"},
    "cal_month":            {"zh": "月",                     "en": ""},
    "cal_month_view":       {"zh": "月视图",                  "en": "Month view"},
    "cal_list_view":        {"zh": "列表",                    "en": "List"},
    "cal_year":             {"zh": "年",                     "en": " "},
    "cal_all":              {"zh": "全部",                    "en": "All"},
    "cal_city_label":       {"zh": "城市",                    "en": "City"},
    "cal_prev_month":       {"zh": "上个月",                  "en": "Previous"},
    "cal_next_month":       {"zh": "下个月",                  "en": "Next"},
    "cal_months":           {"zh": ["一月","二月","三月","四月","五月","六月","七月","八月","九月","十月","十一月","十二月"],
                             "en": ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]},
    "cal_dow":              {"zh": ["日","一","二","三","四","五","六"],
                             "en": ["Su","Mo","Tu","We","Th","Fr","Sa"]},
    "cal_dow_full":         {"zh": ["周日","周一","周二","周三","周四","周五","周六"],
                             "en": ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]},

    # ── Statistics ───────────────────────────────────────
    "stats_title":          {"zh": "数据统计",                "en": "Statistics"},
    "stats_range":          {"zh": "时间范围：",              "en": "Range: "},
    "stats_7d":             {"zh": "近 7 天",                 "en": "7 days"},
    "stats_30d":            {"zh": "近 30 天",                "en": "30 days"},
    "stats_90d":            {"zh": "近 90 天",                "en": "90 days"},
    "stats_new_7d":         {"zh": "近 7 天新增",             "en": "New (7 days)"},
    "stats_new_trend":      {"zh": "新增房源趋势",            "en": "New listings trend"},
    "stats_change_trend":   {"zh": "状态变更趋势",            "en": "Status change trend"},
    "stats_city_dist":      {"zh": "城市分布",                "en": "City distribution"},
    "stats_source_dist":    {"zh": "平台分布",                "en": "Platform distribution"},
    "stats_status_dist":    {"zh": "状态分布",                "en": "Status distribution"},
    "stats_price_dist":     {"zh": "租金分布",                "en": "Rent distribution"},
    "stats_hourly_dist":    {"zh": "房源上线时间分布（荷兰时间）", "en": "Listing drop time (NL time)"},
    # 图表里的 feature 取值。这些是**上游返回的数据值**，不是界面文案，所以
    # 原本一直以英文/荷兰语原样显示。合同类的同义值已在读取层合并
    # （见 mstorage/_charts._FEATURE_SYNONYMS），这里只负责显示。
    "feat_indefinite":      {"zh": "不定期",          "en": "Indefinite"},
    "feat_6_months_max":    {"zh": "最长 6 个月",      "en": "6 months max"},
    "feat_4_months_max":    {"zh": "最长 4 个月",      "en": "4 months max"},
    "feat_employed_only":   {"zh": "仅限在职",         "en": "Employed only"},
    "feat_student_employed":{"zh": "学生或在职",       "en": "Student or employed"},
    "feat_student_only":    {"zh": "仅限学生",         "en": "Student only"},
    "feat_custom":          {"zh": "其它要求",         "en": "Custom"},
    "chart_no_data":        {"zh": "该时间范围内暂无数据",
                             "en": "No data in this time range"},
    "stats_tenant_dist":    {"zh": "租客要求分布",              "en": "Tenant requirement distribution"},
    "stats_contract_dist":  {"zh": "合同类型分布",              "en": "Contract type distribution"},
    "stats_type_dist":      {"zh": "户型分布",                  "en": "Type distribution"},
    "stats_energy_dist":    {"zh": "能耗标签分布",              "en": "Energy label distribution"},
    "stats_area_dist":      {"zh": "面积分布",                  "en": "Area distribution"},
    "stats_floor_dist":     {"zh": "楼层分布",                  "en": "Floor distribution"},

    # ── Settings ─────────────────────────────────────────
    "settings_title":       {"zh": "全局设置",                "en": "Global Settings"},
    "settings_meta":        {"zh": "影响所有用户的抓取行为",    "en": "Scraping behavior for all users"},
    "settings_user_hint":   {"zh": "通知渠道、过滤条件和自动预订请前往", "en": "For notification channels, filters and auto-booking go to "},
    "settings_user_link":   {"zh": "用户管理",                "en": "User Management"},
    "settings_user_suffix": {"zh": "页面按用户独立配置。",      "en": " for per-user config."},
    "settings_scrape":      {"zh": "抓取配置",                "en": "Scrape Config"},
    "settings_interval":    {"zh": "常规轮询间隔（秒）",        "en": "Poll interval (seconds)"},
    "settings_interval_hint":{"zh": "非高峰期的正常轮询间隔，默认 300 秒（5 分钟）。", "en": "Normal poll interval outside peak hours. Default 300s (5 min)."},
    "settings_interval_warn":{"zh": "低于 30 秒极易遇到频繁请求限制或封IP。", "en": "Below 30s will likely trigger rate limiting or IP bans."},
    "settings_log_level":   {"zh": "日志级别",                "en": "Log level"},
    "settings_smart_poll":  {"zh": "智能轮询",                "en": "Smart Polling"},
    "settings_smart_desc":  {"zh": "自适应轮询：高峰期从「最大间隔」出发，每轮成功后自动缩短 5%，逼近「最小间隔」；遭遇 429 限流后立即翻倍退避。非高峰时重置为最大间隔。",
                             "en": "Adaptive polling: starts from max interval during peak, auto-reduces by 5% per success toward min interval; doubles on 429 rate-limit. Resets outside peak."},
    "settings_peak_max":    {"zh": "高峰期最大间隔（秒）",      "en": "Peak max interval (s)"},
    "settings_peak_max_default":{"zh": "默认 60",             "en": "default 60"},
    "settings_peak_max_hint":{"zh": "自适应起点，也是限流退避后的恢复目标", "en": "Adaptive starting point, also the recovery target after backoff"},
    "settings_peak_min":    {"zh": "高峰期最小间隔（秒）",      "en": "Peak min interval (s)"},
    "settings_peak_min_default":{"zh": "默认 15",             "en": "default 15"},
    "settings_peak_min_hint":{"zh": "自适应下限，建议 ≥ 15s",   "en": "Adaptive floor, recommend ≥ 15s"},
    "settings_peak_start":  {"zh": "高峰① 开始（荷兰时间）",   "en": "Peak ① start (NL time)"},
    "settings_peak_end":    {"zh": "高峰① 结束（荷兰时间）",   "en": "Peak ① end (NL time)"},
    "settings_peak_start_2":{"zh": "高峰② 开始（荷兰时间）",   "en": "Peak ② start (NL time)"},
    "settings_peak_end_2":  {"zh": "高峰② 结束（荷兰时间）",   "en": "Peak ② end (NL time)"},
    "settings_jitter":      {"zh": "抖动比例",                "en": "Jitter ratio"},
    "settings_jitter_default":{"zh": "默认 0.20",             "en": "default 0.20"},
    "settings_jitter_hint": {"zh": "破坏机械规律性，避免模式识别", "en": "Break mechanical patterns, avoid detection"},
    "settings_weekdays":    {"zh": "仅工作日启用高峰轮询",      "en": "Peak polling on weekdays only"},
    "settings_weekdays_hint":{"zh": "荷兰早 8:30–10:00 新房源上架最集中。", "en": "New listings drop most between 8:30–10:00 NL time."},
    "settings_heartbeat_interval": {"zh": "心跳间隔（分钟）",     "en": "Heartbeat interval (min)"},
    "settings_heartbeat_default":  {"zh": "默认 60",             "en": "default 60"},
    "settings_heartbeat_hint":     {"zh": "定时发送汇总心跳通知，确认监控进程仍在运行。设为 0 禁用心跳。", "en": "Send periodic summary heartbeat to confirm the monitor is alive. Set 0 to disable."},
    "settings_sources":     {"zh": "监控平台",                "en": "Monitored Platforms"},
    # 平台从 2 个涨到 4 个时这句没跟着改，一直只提 H2S / OurDomain。
    # 别再往里数平台名——加平台时又会漏。
    "settings_sources_hint":{"zh": "可以任意组合，至少需要保留一个平台。", "en": "Enable any combination. At least one platform must remain enabled."},
    "settings_cities":      {"zh": "监控城市",                "en": "Monitored Cities"},
    "settings_h2s_cities":  {"zh": "H2S 监控城市",            "en": "H2S Monitored Cities"},
    "settings_cities_hint": {"zh": "至少选一个城市。监控多个城市会略增加抓取时间。", "en": "Select at least one city. More cities slightly increase scrape time."},
    "settings_ourdomain_cities": {"zh": "OurDomain 监控楼盘",  "en": "OurDomain Buildings"},
    # 同上：这句写"当前支持 Amsterdam Diemen"时只有一个楼盘，现在列了两个
    "settings_ourdomain_hint": {"zh": "只有启用 OurDomain 平台时才会抓取。", "en": "Scraped only when the OurDomain platform is enabled."},
    "settings_xior_cities":     {"zh": "Xior 监控楼盘",       "en": "Xior Buildings"},
    "settings_xior_hint":       {"zh": "荷兰 30 栋楼（15 城市）。只有启用 Xior 平台时才会抓取。", "en": "30 buildings across 15 Dutch cities. Scraped only when the Xior platform is enabled."},
    "settings_save":        {"zh": "保存配置",                "en": "Save Config"},
    "settings_apply":       {"zh": "立即生效",                "en": "Apply Now"},
    "settings_apply_hint":  {"zh": "热重载 .env / 用户配置，不重新加载代码",
                              "en": "Hot reload .env / user config (does NOT reload Python code)"},
    "settings_restart":     {"zh": "重启进程",                "en": "Restart"},
    "settings_restart_hint":{"zh": "完整 stop + start，重新加载代码（改 Python 代码后用）",
                              "en": "Full stop + start. Reloads Python code (use after code changes)"},
    "settings_cancel":      {"zh": "取消",                    "en": "Cancel"},
    "settings_danger_zone": {"zh": "危险操作区",              "en": "Danger Zone"},
    "settings_danger_desc": {"zh": "清空数据库将删除所有历史房源记录、状态变更、通知和图表数据。此操作不可恢复。",
                             "en": "Resetting the database will delete ALL historical listings, status changes, notifications, and chart data. This cannot be undone."},
    "settings_reset_btn":   {"zh": "清空数据库",              "en": "Reset Database"},
    "settings_reset_confirm":{"zh": "确认清空数据库",          "en": "Confirm Database Reset"},
    "settings_reset_warn":  {"zh": "此操作将删除所有历史数据，包括房源列表及快照、状态变更记录、图表统计数据、Web 通知历史。此操作不可恢复。",
                             "en": "This will delete all historical data: listings & snapshots, status change records, chart statistics, web notification history. This cannot be undone."},
    "settings_reset_doing": {"zh": "清空中...",               "en": "Resetting..."},
    "settings_sending":     {"zh": "发送中...",               "en": "Sending..."},
    "settings_config_saved":{"zh": "全局配置已保存",           "en": "Global config saved"},
    "settings_request_fail":{"zh": "请求失败",                "en": "Request failed"},
    # 侧栏赞助入口。措辞刻意克制——「赞助」不是「捐款」，也不加感叹号：
    # 它挨着隐私条款和使用条款，语气得和它们一致。
    "donate":               {"zh": "赞助开发者",              "en": "Support the developer"},
    "settings_no_source":   {"zh": "至少需要启用一个平台，已保留 Holland2Stay。", "en": "At least one platform required. Holland2Stay kept."},
    # 平台开着但一个楼盘都没勾。已按你的选择保存，只是提醒——真要停掉整个
    # 平台，取消勾选平台本身更直接。
    "settings_source_no_target": {
        "zh": "{sources} 已启用但未勾选任何楼盘，这些平台不会抓取。若要停用平台，请取消勾选平台本身。",
        "en": "{sources} enabled but no property selected; these platforms will not be scraped. To disable a platform, uncheck the platform itself.",
    },
    # 真实环境变量盖过 app_settings 时的提示。不提示的话，改了没反应会被当成 bug。
    # 仪表盘覆盖范围横幅。{cities} 由 Config.monitored_city_names() 填入——
    # 分隔符与语序中英不同，所以整句带占位符，不在模板里拼接。
    "dash_coverage_notice": {
        "zh": "当前监控的城市：{cities}。需要其他城市，请",
        "en": "Currently monitoring: {cities}. For other cities, please ",
    },
    "dash_coverage_contact": {"zh": "联系支持", "en": "contact support"},
    "settings_invalid_value": {
        "zh": "配置格式有误，未保存：",
        "en": "Invalid config format, nothing saved:",
    },
    "settings_env_override": {
        "zh": "以下配置被环境变量覆盖，在这里修改不会生效：",
        "en": "These settings are overridden by environment variables and cannot be changed here:",
    },

    # ── Users ────────────────────────────────────────────
    "users_title":          {"zh": "用户管理",                "en": "User Management"},
    "users_new":            {"zh": "新增用户",                "en": "New User"},
    "users_empty":          {"zh": "暂无用户",                "en": "No users yet"},
    "users_empty_hint":     {"zh": "点击「新增用户」添加第一个用户，配置通知渠道和过滤条件即可开始监控。",
                             "en": "Click 'New User' to add your first user with notification channels and filters."},
    "users_enabled":        {"zh": "启用",                    "en": "Enabled"},
    "users_disabled":       {"zh": "停用",                    "en": "Disabled"},
    "users_notif_off":      {"zh": "通知已关闭",              "en": "Notifications off"},
    "users_no_channels":    {"zh": "无通知渠道",              "en": "No channels"},
    "users_no_filter":      {"zh": "无过滤条件",              "en": "No filters"},
    "users_filter_prefix":  {"zh": "以下",                    "en": "Under "},
    "users_auto_on":        {"zh": "自动预订已开启",          "en": "Auto-book enabled"},
    "users_auto_off":       {"zh": "自动预订未开启",          "en": "Auto-book disabled"},
    "users_dry_run":        {"zh": "试运行",                  "en": "Dry run"},
    "users_edit":           {"zh": "编辑",                    "en": "Edit"},
    "users_test_notif":     {"zh": "测试通知",                "en": "Test notify"},
    "users_toggle_on":      {"zh": "启用",                    "en": "Enable"},
    "users_toggle_off":     {"zh": "停用",                    "en": "Disable"},
    "users_delete":         {"zh": "删除",                    "en": "Delete"},
    "users_delete_confirm": {"zh": "确认删除",                "en": "Confirm delete"},
    "users_delete_msg":     {"zh": "确定要删除用户",           "en": "Delete user "},
    "users_delete_warn":    {"zh": "此操作无法撤销。",          "en": "This cannot be undone."},
    "users_rank_hint":      {"zh": "自动预订优先级（越小越优先）", "en": "Auto-book priority (lower = higher priority)"},
    "users_rank_up":        {"zh": "上移（提高优先级）",          "en": "Move up (higher priority)"},
    "users_rank_down":      {"zh": "下移（降低优先级）",          "en": "Move down (lower priority)"},
    "users_drag_hint":      {"zh": "拖拽调整优先级",              "en": "Drag to reorder priority"},
    "users_user_deleted":   {"zh": "已删除",                  "en": "deleted"},

    # ── User Form ────────────────────────────────────────
    "user_form_new":        {"zh": "新增用户",                "en": "New User"},
    "user_form_edit":       {"zh": "编辑用户 · ",             "en": "Edit User · "},
    "user_form_save":       {"zh": "保存修改",                "en": "Save Changes"},
    "user_form_create":     {"zh": "创建用户",                "en": "Create User"},
    "user_form_cancel":     {"zh": "取消",                    "en": "Cancel"},
    "user_form_test":       {"zh": "发送测试通知",            "en": "Send Test Notification"},
    "user_form_testing":    {"zh": "发送中…",                 "en": "Sending…"},
    "user_form_section1":   {"zh": "基本信息",                "en": "Basic Info"},
    "user_form_name":       {"zh": "用户名称",                "en": "User Name"},
    "user_form_status":     {"zh": "账户状态",                "en": "Account Status"},
    "user_form_enable":     {"zh": "启用此用户",              "en": "Enable this user"},
    "user_form_section2":   {"zh": "通知渠道",                "en": "Notification Channels"},
    "user_form_notif_on":   {"zh": "开启通知",                "en": "Enable notifications"},
    "user_form_recipient":  {"zh": "收件人（手机号或 Apple ID）", "en": "Recipient (phone or Apple ID)"},
    "user_form_bot_token":  {"zh": "Bot Token",              "en": "Bot Token"},
    "user_form_chat_id":    {"zh": "Chat ID",                "en": "Chat ID"},
    "user_form_smtp_host":  {"zh": "SMTP Host",              "en": "SMTP Host"},
    "user_form_port":       {"zh": "Port",                   "en": "Port"},
    "user_form_security":   {"zh": "Security",               "en": "Security"},
    "user_form_smtp_user":  {"zh": "SMTP Username（可选）",    "en": "SMTP Username (optional)"},
    "user_form_smtp_pass":  {"zh": "SMTP Password / App Password", "en": "SMTP Password / App Password"},
    "user_form_from":       {"zh": "From",                   "en": "From"},
    "user_form_to":         {"zh": "To",                     "en": "To"},
    "user_form_smtp_hint":  {"zh": "常见配置：Gmail smtp.gmail.com:587 + STARTTLS", "en": "Common: Gmail smtp.gmail.com:587 + STARTTLS"},
    "user_form_account_sid":{"zh": "Account SID",            "en": "Account SID"},
    "user_form_auth_token": {"zh": "Auth Token",             "en": "Auth Token"},
    "user_form_section3":   {"zh": "通知过滤条件",            "en": "Notification Filters"},
    "user_form_filter_hint":{"zh": "留空 = 不限制",           "en": "Leave empty = no limit"},
    # 平台适用范围的统一说明。措辞要说清「其余平台不受影响」——只写「仅对 X
    # 生效」会被读成「其它平台会被排除」，那是相反的意思。
    #
    # 不要在这里复述徽标的具体措辞（原先写的是「带『仅 …』标记的」）：徽标现在
    # 两种形态并存——「Xior 除外」与「仅 Holland2Stay」，哪种取决于点名哪一边
    # 更短，见 config.dim_scope_badge。写死一种会和另一种对不上。
    "user_form_dim_scope_intro": {
        "zh": "带平台标记的条件只对部分平台生效：其余平台不提供该属性，"
              "它们的房源不受该条件影响，会照常通知。悬停标记可看具体平台。",
        "en": "Conditions with a platform marker apply to some platforms only: "
              "the others do not report that attribute, so their listings are "
              "unaffected and still notify. Hover the marker to see which.",
    },
    "user_form_max_rent":   {"zh": "最高月租（€）",           "en": "Max rent (€)"},
    "user_form_min_area":   {"zh": "最小面积（m²）",          "en": "Min area (m²)"},
    "user_form_min_floor":      {"zh": "最低楼层",                "en": "Min floor"},
    "multi_select_placeholder": {"zh": "不限",                    "en": "All"},
    "select_all":           {"zh": "全选",                    "en": "Select all"},
    "deselect_all":         {"zh": "取消全选",                 "en": "Deselect all"},
    "user_form_occupancy":  {"zh": "允许入住类型",            "en": "Allowed occupancy"},
    "user_form_types":      {"zh": "允许户型",                "en": "Allowed types"},
    "user_form_hoods":      {"zh": "允许片区",                "en": "Allowed neighborhoods"},
    "user_form_cities":     {"zh": "允许城市",                "en": "Allowed cities"},
    "user_form_offer":      {"zh": "合同类型",                "en": "Contract type"},
    "user_form_sources":    {"zh": "平台",           "en": "Platform"},
    "user_form_tenant":     {"zh": "租客要求",                "en": "Tenant requirement"},
    "user_form_promo":      {"zh": "标签/促销",              "en": "Offer / Promo"},
    "user_form_section4":   {"zh": "自动预订",                "en": "Auto Booking"},
    "user_form_ab_enable":  {"zh": "开启自动预订",            "en": "Enable auto-booking"},
    "user_form_ab_dry":     {"zh": "试运行模式",              "en": "Dry run mode"},
    "user_form_ab_dry_badge":{"zh": "推荐先开启",             "en": "Recommended"},
    "user_form_ab_email":   {"zh": "邮箱",                   "en": "Email"},
    "user_form_rentcafe_hint": {"zh": "请先在浏览器中手动注册 RENTCafe 账号，再填写下方信息", "en": "Register your RENTCafe account in a browser first, then fill in below"},
    "user_form_ab_h2s":       {"zh": "Holland2Stay 账号",     "en": "Holland2Stay account"},
    "user_form_ab_xior":      {"zh": "Xior 账号",             "en": "Xior account"},
    "user_form_ab_ourdomain": {"zh": "OurDomain 账号",        "en": "OurDomain account"},
    "user_form_ab_pass":    {"zh": "密码",                   "en": "Password"},
    "user_form_first_name": {"zh": "名",                     "en": "First Name"},
    "user_form_last_name":  {"zh": "姓",                     "en": "Last Name"},
    "user_form_phone":      {"zh": "电话",                   "en": "Phone"},
    "user_form_birth_date": {"zh": "出生日期",                "en": "Birth Date"},
    "user_form_ab_payment": {"zh": "支付方式",                "en": "Payment method"},
    "user_form_ab_ideal":   {"zh": "iDEAL（荷兰网银，推荐）",  "en": "iDEAL (Dutch online banking, recommended)"},
    "user_form_ab_visa":    {"zh": "Visa 信用卡",             "en": "Visa credit card"},
    "user_form_ab_mc":      {"zh": "Mastercard 信用卡",       "en": "Mastercard credit card"},
    "user_form_ab_filter":  {"zh": "自动预订专用过滤（独立于通知过滤）", "en": "Auto-book specific filters (independent from notification filters)"},
    "user_form_copy_filter": {"zh": "从通知过滤复制", "en": "Copy from notification filters"},
    "user_form_ab_safety":  {"zh": "安全设计：自动预订只执行到「加入购物车」，不会自动付款。完成后通知你确认。",
                             "en": "Safety design: auto-booking only goes as far as 'add to cart'. It will NOT auto-pay. You'll be notified to confirm."},
    "user_form_pw_keep":    {"zh": "已保存，留空保留原密码",    "en": "Saved, leave empty to keep"},

    # ── Email 模式切换（shared / custom） ─────────────────
    "user_form_email_shared":      {"zh": "使用 FlatRadar 邮件服务",   "en": "Use FlatRadar email service"},
    "user_form_email_shared_hint": {"zh": "推荐：仅需填收件邮箱，无需配置 SMTP", "en": "Recommended — only fill the recipient address, no SMTP setup"},
    "user_form_email_custom":      {"zh": "使用自己的 SMTP 服务器",     "en": "Use your own SMTP server"},
    "user_form_email_custom_hint": {"zh": "高级：自填 SMTP 主机/账号/密码", "en": "Advanced — bring your own SMTP host/account/password"},
    "user_form_email_verified":      {"zh": "邮箱已验证",                  "en": "Email verified"},
    "user_form_email_unverified":    {"zh": "邮箱未验证（通知暂不会发出）",  "en": "Email not verified (notifications paused)"},
    "user_form_email_resend_verify": {"zh": "重发验证邮件",                "en": "Resend verification email"},

    # ── 登录（iOS App / Web 共用） ───────────────────────
    "user_form_app_login":               {"zh": "登录",                      "en": "Login"},
    "user_form_app_login_hint":          {"zh": "允许该用户用此名字 + 密码登录手机 App 和网页端，查看自己的数据并接收推送。", "en": "Allow this user to sign in to the iOS App and web with this name + password to view their own data and receive notifications."},
    "user_form_app_login_enable":        {"zh": "启用登录",                  "en": "Enable login"},
    "user_form_app_password":            {"zh": "登录密码",                  "en": "Login password"},
    "user_form_app_password_set":        {"zh": "至少 8 个字符",             "en": "At least 8 characters"},
    "user_form_app_password_set_label":  {"zh": "已设置（留空保留）",        "en": "Set (leave empty to keep)"},
    "user_form_app_password_unset_label":{"zh": "尚未设置登录密码",          "en": "Login password not set"},
    "user_form_app_password_clear":      {"zh": "清除已保存的密码",          "en": "Clear saved password"},

    # ── App 账户管理页 ───────────────────────────────────
    "app_accounts_title":     {"zh": "客户端管理",                "en": "Client Management"},
    "app_accounts_meta":      {"zh": "查看和撤销 iOS App / 第三方客户端签发的 Bearer Token。", "en": "View and revoke Bearer Tokens issued to iOS App / third-party clients."},
    "app_accounts_empty":     {"zh": "暂无活跃会话",                "en": "No active sessions"},
    # 「推送设备」这一整个 tab 之前是硬编码中文，英文界面照样是中文表头
    "app_accounts_tab_sessions": {"zh": "App 会话",                 "en": "App sessions"},
    "app_accounts_tab_devices":  {"zh": "推送设备",                 "en": "Push devices"},
    "devices_empty":          {"zh": "暂无推送设备注册。",           "en": "No push devices registered."},
    "devices_col_device":     {"zh": "设备",                        "en": "Device"},
    "devices_col_platform":   {"zh": "平台",                        "en": "Platform"},
    "devices_col_user":       {"zh": "用户",                        "en": "User"},
    "devices_col_model":      {"zh": "型号",                        "en": "Model"},
    "devices_col_env":        {"zh": "环境",                        "en": "Environment"},
    "devices_col_created":    {"zh": "注册时间",                    "en": "Registered"},
    "devices_col_last_seen":  {"zh": "最后活跃",                    "en": "Last seen"},
    "devices_col_status":     {"zh": "状态",                        "en": "Status"},
    "devices_col_actions":    {"zh": "操作",                        "en": "Actions"},
    "devices_disabled":       {"zh": "已禁用",                      "en": "Disabled"},
    "devices_active":         {"zh": "活跃",                        "en": "Active"},
    "devices_disable":        {"zh": "禁用",                        "en": "Disable"},
    "devices_disable_confirm":{"zh": "禁用此设备？不再接收推送。",    "en": "Disable this device? It will stop receiving push."},
    "devices_test_push_hint": {"zh": "发送测试推送（APNs + FCM）",   "en": "Send a test push (APNs + FCM)"},
    "app_accounts_show_revoked":{"zh": "显示已撤销",                "en": "Show revoked"},
    "app_accounts_hide_revoked":{"zh": "隐藏已撤销",                "en": "Hide revoked"},
    "app_accounts_device":    {"zh": "设备",                       "en": "Device"},
    "app_accounts_role":      {"zh": "身份",                       "en": "Role"},
    "app_accounts_user":      {"zh": "归属用户",                   "en": "User"},
    "app_accounts_created":   {"zh": "签发时间",                   "en": "Created"},
    "app_accounts_last_used": {"zh": "最近使用",                   "en": "Last used"},
    "app_accounts_expires":   {"zh": "过期时间",                   "en": "Expires"},
    "app_accounts_status":    {"zh": "状态",                       "en": "Status"},
    "app_accounts_actions":   {"zh": "操作",                       "en": "Actions"},
    "app_accounts_revoke":    {"zh": "撤销",                       "en": "Revoke"},
    "app_accounts_revoked":   {"zh": "已撤销",                     "en": "Revoked"},
    "app_accounts_active":    {"zh": "活跃",                       "en": "Active"},
    "app_accounts_expired":   {"zh": "已过期",                     "en": "Expired"},
    "app_accounts_never":     {"zh": "—",                          "en": "—"},
    "app_accounts_no_expiry": {"zh": "永不过期",                   "en": "Never expires"},
    "app_accounts_confirm":   {"zh": "确定撤销这个会话吗？此设备需要重新登录。", "en": "Revoke this session? The device will need to log in again."},
    "app_accounts_revoked_ok":{"zh": "会话已撤销",                  "en": "Session revoked"},
    "app_accounts_revoke_fail":{"zh": "撤销失败（可能已撤销）",     "en": "Revoke failed (may already be revoked)"},

    "user_form_token_keep": {"zh": "已保存，留空保留原令牌",    "en": "Saved, leave empty to keep"},
    "user_form_result":     {"zh": "测试结果",                "en": "Test result"},
    "user_form_reload_hint":{"zh": "保存后点击「立即生效」按钮通知 monitor.py 热重载。", "en": "After saving, click 'Apply Now' to hot-reload monitor.py."},

    # ── Guest mode ───────────────────────────────────────
    "guest_mode":           {"zh": "访客模式",            "en": "Guest mode"},

    # ── Login ────────────────────────────────────────────
    # 落地页的 <title> 与 meta description。
    #
    # ⚠️ 这两条**只给搜索引擎和分享卡片看**，不是界面文案，改动前先想清楚：
    # `/` 对匿名访客 302 到 `/login`，所以这就是搜索结果里显示的那一行。
    # 原先写的是「登录 · FlatRadar」——一个 26.9KB 的落地页，标题却只说「登录」，
    # 搜「荷兰租房监控」的人看到它不会点。
    #
    # 刻意不在标题里写 Holland2Stay / Xior：那样 SEO 更强，但一来提高了对被监控
    # 平台的曝光，二来在自己的标题里用别家商标另有一层风险。见 docs/ARCHITECTURE。
    #
    # 长度按搜索结果的显示上限控制：标题 ≤ 30 中文字，描述 ≤ 80 中文字。
    "login_title": {
        "zh": "FlatRadar · 荷兰租房房源监控与提醒",
        "en": "FlatRadar · Dutch Rental Listing Alerts",
    },
    # ── 用户配置页的「App 推送」卡片 ─────────────────────────────
    #: 注意它**不是** notification_channels 里的一个值——推送走 device_tokens，
    #: 由 App 登录时登记。这张卡只说明，不勾选。
    "channel_app_push": {
        "zh": "App 推送",
        "en": "App push",
    },
    "channel_app_push_badge": {
        "zh": "免配置",
        "en": "no setup",
    },
    "channel_app_push_hint": {
        "zh": "安装 App 并用本账号登录即可",
        "en": "Install the app and sign in with this account",
    },
    "channel_app_push_active": {
        "zh": "已连接 %(n)s 台设备",
        "en": "%(n)s device(s) connected",
    },
    "channel_app_push_help": {
        "zh": "下载 App 后使用账号登录即可收到通知。",
        "en": "Download the app and sign in with this account to start receiving notifications.",
    },
    #: 「设置」页的地图显示范围。runtime 类配置，住在 app_settings 表。
    "settings_map_max_age": {
        "zh": "地图显示范围（天）",
        "en": "Map age limit (days)",
    },
    "settings_map_max_age_default": {
        "zh": "默认 14",
        "en": "default 14",
    },
    "settings_map_max_age_hint": {
        "zh": "只在地图上显示这么多天内还被抓到过的房源。已成交的房源会长期留在库里，不过滤会让几个月前就下架的单元一直钉在图上。填 0 显示全部。",
        "en": "Only show listings still seen within this many days. Taken listings stay in the database indefinitely; without this, units delisted months ago remain pinned on the map. Set 0 to show everything.",
    },
    # ── 新用户引导（app/services/onboarding_service.py）─────────────
    #: 清单标题。只在「还收不到通知」时出现，配好即消失。
    "onb_title": {
        "zh": "还差一步就能收到房源提醒",
        "en": "One step left before you get alerts",
    },
    "onb_title_done": {
        "zh": "提醒已开启",
        "en": "Alerts are on",
    },
    #: 第一步：筛选条件。空不是错，所以措辞是描述而不是催促。
    "onb_step_filter": {
        "zh": "筛选条件",
        "en": "Filters",
    },
    "onb_filter_empty": {
        "zh": "未设置——当前会把全部城市、全部价位的房源都推给你",
        "en": "Not set — you'll be alerted about every listing, any city, any price",
    },
    "onb_filter_set": {
        "zh": "已设置 %(n)s 项",
        "en": "%(n)s set",
    },
    #: 第二步：接收方式。这一步是真正的闸。
    "onb_step_route": {
        "zh": "接收方式",
        "en": "How you get alerted",
    },
    "onb_route_none": {
        "zh": "没有任何接收方式——你现在收不到通知",
        "en": "No delivery route — you are not receiving anything",
    },
    "onb_route_toggle_off": {
        "zh": "通知总开关是关着的——你现在收不到通知",
        "en": "Notifications are switched off — you are not receiving anything",
    },
    "onb_route_account_off": {
        "zh": "账号已停用——所有通知都已停止",
        "en": "Account is disabled — all alerts are stopped",
    },
    "onb_route_ok": {
        "zh": "已启用：",
        "en": "Active: ",
    },
    #: 第三步：验证。配好之后才有意义，所以只在前两步过了才出现。
    "onb_step_verify": {
        "zh": "确认能收到",
        "en": "Confirm it works",
    },
    "onb_verify_hint": {
        "zh": "发一条测试通知，确认它真的到你手上",
        "en": "Send a test notification and check it actually arrives",
    },
    "onb_verify_btn": {
        "zh": "发送测试通知",
        "en": "Send test",
    },
    "onb_go_settings": {
        "zh": "去配置接收方式",
        "en": "Set up delivery",
    },
    #: 两个按钮都写「去设置」会让人不知道点哪个通向什么。
    "onb_go_filter": {
        "zh": "去设置筛选条件",
        "en": "Set filters",
    },
    #: 面板铃铛是全局流水，不按用户筛选——不说清楚会让人以为筛选生效了。
    "onb_bell_note": {
        "zh": "下面的通知列表是全站流水，不按你的筛选条件过滤。只有上面配好的接收方式才会按条件推给你。",
        "en": "The notification feed below is site-wide and unfiltered. Only the delivery routes above respect your filters.",
    },
    "onb_guide_link": {
        "zh": "完整使用说明",
        "en": "Full guide",
    },
    #: 价格右上角那个星号的 tooltip。只有 OurDomain / OurCampus 会出现。
    "rent_basis_hint": {
        "zh": "此价格为基础租金，服务费另计：",
        "en": "Base rent; service costs are charged separately:",
    },
    "login_meta_description": {
        "zh": "自动监控荷兰多个租房平台的新房源，可按城市、租金、面积、租客资格"
              "筛选，命中立刻推送到手机。支持 iOS 与 Android，免费使用。",
        # 英文按 ~155 字符控制：超过会被搜索结果截断，句尾的信息就白写了。
        # 中文按 ~80 字算，两边上限不同是因为搜索结果按像素宽度截。
        "en": "Tracks new rental listings across Dutch housing platforms. Filter "
              "by city, rent, size and tenant eligibility, and get an instant "
              "push when one matches.",
    },
    "login_header":         {"zh": "FlatRadar",               "en": "FlatRadar"},
    "login_subtitle":       {"zh": "请登录以继续访问",          "en": "Sign in to continue"},
    "login_username":       {"zh": "用户名",                  "en": "Username"},
    "login_password":       {"zh": "密码",                    "en": "Password"},
    "login_btn":            {"zh": "登录",                    "en": "Sign in"},
    "login_footer":         {"zh": "通过 WEB_USERNAME / WEB_PASSWORD 在 .env 中配置凭据",
                             "en": "Configure credentials via WEB_USERNAME / WEB_PASSWORD in .env"},
    "login_error":          {"zh": "用户名或密码错误",          "en": "Invalid username or password"},

    # ── Multi-role (admin / user / guest) ────────────────
    "my_account":           {"zh": "我的账号",                 "en": "My Account"},
    "user_form_self_only_hint": {
        "zh": "你正在编辑自己的账号。可调整通知渠道与过滤条件；账户名、客户端登录开关及自动预订仅 admin 可修改。",
        "en": "You are editing your own account. You can adjust notification channels and filters. Account name, client login switch, and auto-booking can only be changed by admin.",
    },

    # ── Badges / Status ──────────────────────────────────
    "badge_book":           {"zh": "直订",                    "en": "Direct"},
    "badge_lottery":        {"zh": "摇号",                    "en": "Lottery"},
    "badge_dry_run":        {"zh": "试运行",                  "en": "Dry run"},
    "badge_recommend":      {"zh": "推荐先开启",              "en": "Recommended"},
    "badge_macos":          {"zh": "macOS 专属",              "en": "macOS only"},
    "badge_free":           {"zh": "免费",                    "en": "Free"},
    "badge_cross_platform": {"zh": "跨平台",                  "en": "Cross-platform"},
    "badge_twilio":         {"zh": "Twilio 付费",             "en": "Twilio paid"},
    "badge_wip":            {"zh": "开发中，暂不可用",          "en": "WIP — not yet available"},

    # ── Time ─────────────────────────────────────────────
    "time_seconds":         {"zh": "秒前",                    "en": "s ago"},
    "time_minutes":         {"zh": "分钟前",                  "en": "m ago"},
    "time_hours":           {"zh": "小时前",                  "en": "h ago"},
    "time_days":            {"zh": "天前",                    "en": "d ago"},

    # ── Misc ─────────────────────────────────────────────
    "per_month":            {"zh": "/月",                     "en": "/mo"},
    "no_message":           {"zh": "—",                       "en": "—"},
    "confirm":              {"zh": "确认清空",                "en": "Confirm Reset"},
    "domain_hint":          {"zh": "通过 WEB_USERNAME / WEB_PASSWORD 在 .env 中配置凭据",
                             "en": "Configure credentials via WEB_USERNAME / WEB_PASSWORD in .env"},
    "test_result_title":    {"zh": "通知测试结果",            "en": "Notification test result"},
}


def tr(key: str, lang: str) -> str:
    """Look up a translation key. Falls back to zh if key missing."""
    entry = TRANSLATIONS.get(key)
    if not entry:
        logger.warning("Missing translation key: %s", key)
        return key
    return entry.get(lang, entry.get("zh", key))
