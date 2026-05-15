import Foundation

/// Server-Sent Events (text/event-stream) 解析器。
///
/// 为什么自己写
/// ------------
/// EventSource 不在 Apple SDK 里；第三方 ``LDSwiftEventSource`` 体量太大。
/// SSE 协议本身只有 ~10 行格式（``data:``、``event:``、``retry:``、``:`` 注释、
/// 空行结束 record），加上 URLSession iOS 15+ 的 ``bytes(for:)`` async sequence
/// API，用 100 行 Swift 自己写最干净。
///
/// 用法
/// ----
/// ```swift
/// let client = SSEClient(url: url, bearerToken: token)
/// for try await event in client.events() {
///     switch event {
///     case .data(let payload): // 一条 data: 完整行
///     case .retry(let ms):     // server 告知重连间隔
///     case .keepalive:         // : 开头的注释，保活心跳
///     }
/// }
/// ```
///
/// 重连
/// ----
/// 解析器只负责单条连接；连接中断时 throws，调用方（NotificationsStore）
/// 跑指数退避循环重连。
struct SSEClient: Sendable {
    let url: URL
    let bearerToken: String?
    let timeout: TimeInterval

    init(url: URL, bearerToken: String? = nil, timeout: TimeInterval = 0) {
        self.url = url
        self.bearerToken = bearerToken
        self.timeout = timeout
    }

    enum Event: Sendable, Equatable {
        case data(String)            // "data: ..." 累积后的 payload
        case retry(milliseconds: Int)
        case keepalive
    }

    enum SSEError: Error, Sendable {
        case badResponse(Int)
        case cancelled
    }

    /// 拉一个 AsyncThrowingStream；调用方用 ``for try await`` 消费。
    ///
    /// 注意：返回的 stream 只在当前 Task 内有效；Task 被 cancel 时 stream 自动终止。
    func events() -> AsyncThrowingStream<Event, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    try await self.consume(into: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func consume(into continuation: AsyncThrowingStream<Event, Error>.Continuation) async throws {
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        if let tok = bearerToken {
            req.setValue("Bearer \(tok)", forHTTPHeaderField: "Authorization")
        }
        if timeout > 0 {
            req.timeoutInterval = timeout
        }

        let (bytes, response) = try await URLSession.shared.bytes(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw SSEError.badResponse(0)
        }
        guard (200...299).contains(http.statusCode) else {
            throw SSEError.badResponse(http.statusCode)
        }

        // 解析协议（针对本项目后端的简化版）：
        //
        // 后端的 SSE 永远是单行 data:
        //   data: <一行 JSON>\n\n
        //
        // 标准 SSE 规范要求多个连续 ``data:`` 行累积、空行 emit。我们后端不会
        // 发多行 data，于是这里**直接在 data: 行就 emit**，不依赖空行。
        //
        // 为什么必须放弃"空行 emit"
        // -----------------------
        // ``URLSession.AsyncBytes.lines`` 在收到最后一个 ``\n`` 后不会立即把
        // 空字符串作为 line emit——它要等下一段数据到达才能确认 record 边界。
        // 后端发完 ``\n\n`` 之后下一条是 5 秒后的 keepalive；这中间 iOS 端就
        // 一直收不到 data event 触发，看起来像 SSE 卡死。
        //
        // 直接在 data: 行 emit 解决这个延迟，对单行 data 的后端完全等价。
        for try await line in bytes.lines {
            try Task.checkCancellation()
            if line.isEmpty {
                continue  // record 分隔空行：已经在 data: 行 emit 过了
            }
            if line.hasPrefix(":") {
                // SSE 注释行；后端 ": keepalive" 走这里
                continuation.yield(.keepalive)
                continue
            }
            if line.hasPrefix("data:") {
                let payload = line
                    .dropFirst(5)
                    .drop(while: { $0 == " " })
                continuation.yield(.data(String(payload)))
                continue
            }
            if line.hasPrefix("retry:") {
                let raw = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
                if let ms = Int(raw) {
                    continuation.yield(.retry(milliseconds: ms))
                }
                continue
            }
            // event: / id: 字段当前不需要
        }
    }
}
