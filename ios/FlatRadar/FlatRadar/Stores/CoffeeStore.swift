import StoreKit
import SwiftUI

/// Buy me a coffee — StoreKit 2 捐赠。
///
/// 产品在 App Store Connect 创建为 consumable：
/// - coffee.espresso   → Espresso  ☕   €0.99
/// - coffee.latte      → Latte     ☕☕  €2.99
/// - coffee.flatwhite  → Flat White ☕☕☕ €5.99
///
/// 购买成功后弹出感谢 alert，不绑定任何功能。
@MainActor
@Observable
final class CoffeeStore {
    private let productIDs = ["coffee.espresso", "coffee.latte", "coffee.flatwhite"]

    var products: [Product] = []
    var isLoading = false
    var purchaseError: String?
    var showThanks = false
    var thanksMessage = ""

    /// Fetch product metadata from App Store.
    func loadProducts() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let fetched = try await Product.products(for: productIDs)
            products = fetched.sorted { $0.price < $1.price }
        } catch {
            purchaseError = error.localizedDescription
            #if DEBUG
            print("[CoffeeStore] loadProducts error: \(error)")
            #endif
        }
    }

    /// Purchase a product and handle the transaction.
    func purchase(_ product: Product) async {
        purchaseError = nil
        do {
            let result = try await product.purchase()
            switch result {
            case .success(let verification):
                switch verification {
                case .verified(let tx):
                    await tx.finish()
                    thanksMessage = product.displayName
                    showThanks = true
                case .unverified:
                    purchaseError = String(localized: "Transaction verification failed.")
                }
            case .userCancelled:
                break
            case .pending:
                purchaseError = String(localized: "Payment is pending approval.")
            @unknown default:
                break
            }
        } catch {
            purchaseError = error.localizedDescription
            #if DEBUG
            print("[CoffeeStore] purchase error: \(error)")
            #endif
        }
    }

    /// Listen for transactions arriving outside the purchase() flow
    /// (e.g. parental approval, network delay).
    func listenForTransactions() {
        Task.detached {
            for await verification in Transaction.updates {
                guard case .verified(let tx) = verification else { continue }
                await tx.finish()
            }
        }
    }
}
