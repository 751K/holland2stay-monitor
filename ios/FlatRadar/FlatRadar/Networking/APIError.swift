import Foundation

enum APIError: Error {
    case unauthorized(String)
    case forbidden(String)
    case notFound(String)
    case validation(String)
    case rateLimited(String)
    case serverError(String)
    case network(any Error)
    case decoding(any Error)
    case badResponse(Int)

    /// Create from backend error payload. Code never shown to user —
    /// clients branch on code, not message.
    static func fromPayload(code: String, message: String) -> APIError {
        switch code {
        case "unauthorized": return .unauthorized(message)
        case "forbidden":   return .forbidden(message)
        case "not_found":   return .notFound(message)
        case "validation":  return .validation(message)
        case "rate_limited": return .rateLimited(message)
        case "server_error": return .serverError(message)
        default:            return .serverError(message)
        }
    }
}

extension APIError: LocalizedError {
    var errorDescription: String? {
        switch self {
        case .unauthorized(let msg):  return msg
        case .forbidden(let msg):    return msg
        case .notFound(let msg):     return msg
        case .validation(let msg):   return msg
        case .rateLimited(let msg):  return msg
        case .serverError(let msg):  return msg
        case .network(let err):      return "Network error: \(err.localizedDescription)"
        case .decoding(let err):     return "Data error: \(err.localizedDescription)"
        case .badResponse(let code): return "Server returned HTTP \(code)"
        }
    }
}
