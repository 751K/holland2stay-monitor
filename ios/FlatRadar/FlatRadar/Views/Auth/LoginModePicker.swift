import SwiftUI

struct LoginModePicker: View {
    @Binding var mode: LoginMode
    @Namespace private var ns

    var body: some View {
        HStack(spacing: 2) {
            ForEach(LoginMode.allCases, id: \.self) { m in
                Button {
                    withAnimation(.spring(duration: 0.3)) { mode = m }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: m.icon)
                            .font(.caption.weight(.semibold))
                        Text(m.label)
                            .font(.subheadline.weight(.medium))
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .contentShape(Rectangle())
                    .background {
                        if mode == m {
                            RoundedRectangle(cornerRadius: 10)
                                .fill(.regularMaterial)
                                .shadow(color: .black.opacity(0.08), radius: 2, y: 1)
                                .matchedGeometryEffect(id: "picker", in: ns)
                        }
                    }
                    .foregroundStyle(mode == m ? .primary : .secondary)
                }
            }
        }
        .padding(3)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
    }
}

enum LoginMode: String, CaseIterable {
    case admin
    case user
    case guest

    var label: String {
        switch self {
        case .admin: return String(localized: "Admin")
        case .user:  return String(localized: "User")
        case .guest: return String(localized: "Guest")
        }
    }

    var icon: String {
        switch self {
        case .admin: return "shield.fill"
        case .user:  return "person.fill"
        case .guest: return "eye.fill"
        }
    }
}
