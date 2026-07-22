import SwiftUI
import AppKit

// MARK: - View Model
class NotchViewModel: ObservableObject {
    @Published var activeState: NotchState = .compact
    
    enum NotchState {
        case compact
        case overview
        case approval
        case ask
        case kanban
    }
}
