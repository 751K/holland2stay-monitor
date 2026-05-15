import SwiftUI

struct LoginModePicker: View {
    @Binding var mode: LoginMode
    @Namespace private var ns

    var body: some View {
        HStack(spacing: 0) {
            ForEach(LoginMode.allCases, id: \.self) { m in
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) { mode = m }
                } label: {
                    Text(m.label)
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background {
                            if mode == m {
                                RoundedRectangle(cornerRadius: 8)
                                    .fill(.ultraThinMaterial)
                                    .matchedGeometryEffect(id: "picker", in: ns)
                            }
                        }
                        .foregroundStyle(mode == m ? .primary : .secondary)
                }
            }
        }
        .padding(4)
        .background(.quaternary)
        .clipShape(RoundedRectangle(cornerRadius: 10))
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
}
