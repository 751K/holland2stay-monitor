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

    /// 401 or 403 — the session should be cleared and user redirected to login.
    var isAuthError: Bool {
        switch self {
        case .unauthorized, .forbidden: return true
        default: return false
        }
    }

    /// Errors that make sense to retry (network blip, server hiccup).
    var isRetryable: Bool {
        switch self {
        case .network, .serverError, .rateLimited: return true
        case .badResponse(let code): return code >= 500 || code == 429
        default: return false
        }
    }
}

extension APIError: LocalizedError {
    var errorDescription: String? {
        switch self {
        case .unauthorized: return String(localized: "Session Expired")
        case .forbidden:    return String(localized: "Access Denied")
        case .notFound:     return String(localized: "Not Found")
        case .validation:   return String(localized: "Invalid Request")
        case .rateLimited:  return String(localized: "Too Many Requests")
        case .serverError:  return String(localized: "Server Error")
        case .network:      return String(localized: "Connection Failed")
        case .decoding:     return String(localized: "Data Error")
        case .badResponse:  return String(localized: "Unexpected Response")
        }
    }

    var failureReason: String? {
        switch self {
        case .unauthorized(let msg): return msg
        case .forbidden(let msg):    return msg
        case .notFound(let msg):     return msg
        case .validation(let msg):   return msg
        case .rateLimited(let msg):  return msg
        case .serverError(let msg):  return msg
        case .network(let err):      return String(localized: "Network error: \(err.localizedDescription)")
        case .decoding:              return String(localized: "The server returned data in an unexpected format.")
        case .badResponse(let code): return String(localized: "Server returned HTTP \(code)")
        }
    }

    var recoverySuggestion: String? {
        switch self {
        case .unauthorized: return String(localized: "Please sign in again.")
        case .forbidden:    return String(localized: "You don't have permission to access this.")
        case .network:      return String(localized: "Check your internet connection and try again.")
        case .serverError:  return String(localized: "Please try again later.")
        case .rateLimited:  return String(localized: "Please wait a moment before retrying.")
        case .notFound:     return String(localized: "It may have been removed.")
        case .validation, .decoding, .badResponse: return nil
        }
    }

    /// SF Symbol name suitable for ContentUnavailableView.
    var systemImage: String {
        switch self {
        case .unauthorized: return "lock.shield"
        case .forbidden:    return "hand.raised.slash"
        case .network:      return "wifi.slash"
        case .serverError:  return "exclamationmark.icloud"
        case .rateLimited:  return "clock.badge.exclamationmark"
        case .notFound:     return "questionmark.folder"
        case .validation:   return "exclamationmark.triangle"
        case .decoding:     return "doc.badge.gearshape"
        case .badResponse:  return "exclamationmark.arrow.triangle.2.circlepath"
        }
    }
}
