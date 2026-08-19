import SwiftUI
import AppKit
import Network
import Combine
import CoreText
import Darwin
import UserNotifications
import CryptoKit

// MARK: - Font Registration
// Departure Mono is bundled next to the binary (SIL OFL, see
// departuremono.com). We register it at launch via CTFontManager so
// SwiftUI can use it by PostScript name without an Info.plist /
// Xcode project (we compile standalone with swiftc).
enum NotchFont {
    static let monospace = "DepartureMono-Regular"   // PostScript name

    // Custom monospaced font helper. Falls back to system mono if the
    // DepartureMono font failed to register (e.g. file missing).
    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        // Note: SwiftUI custom fonts ignore .weight() (that modifier only
        // works for system fonts and would make the font fall back to SF).
        // DepartureMono ships Regular only, so we vary size for hierarchy.
        .custom(monospace, size: size)
    }

    // Resolve a resource path: works both for standalone binary (next to
    // executable) and .app bundle (in Contents/Resources/).
    static func resourcePath(_ name: String) -> URL? {
        let candidates = [
            // .app bundle: Contents/Resources/<name>
            Bundle.main.resourceURL?.appendingPathComponent(name),
            // Standalone binary: next to executable
            Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent(name),
            // Fallback: relative to cwd
            URL(fileURLWithPath: name),
        ]
        return candidates.first { url in
            url != nil && FileManager.default.fileExists(atPath: url!.path)
        } ?? nil
    }

    static func registerIfNeeded() {
        // Look for the .otf in bundle Resources or next to the executable.
        let candidates = [
            Bundle.main.resourceURL?.appendingPathComponent("Fonts/DepartureMono-Regular.otf"),
            Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("DepartureMono-Regular.otf"),
            URL(fileURLWithPath: "DepartureMono-Regular.otf"),
        ].compactMap { $0 }
        for url in candidates {
            guard FileManager.default.fileExists(atPath: url.path) else { continue }
            var error: Unmanaged<CFError>?
            let ok = CTFontManagerRegisterFontsForURL(url as CFURL, .process, &error)
            if ok {
                print("DepartureMono registered from \(url.path)")
                return
            }
            // Registration failing — most likely "already registered" (harmless)
            // or the font file is corrupt. Either way, fall through to next
            // candidate or to the system-mono fallback in the font helper.
            if let err = error?.takeRetainedValue() {
                print("font registration failed: \(CFErrorGetCode(err))")
            }
        }
        print("DepartureMono not found; falling back to system monospaced")
    }
}

// MARK: - Provider Logo
// Loads a provider's logo PNG from the `logos/` dir next to the binary.
// Logos are sourced from the real app bundle (zai.png, claude.png, ...).
enum ProviderLogo {
    static func image(for provider: String, size: CGFloat) -> Image? {
        // Normalize "Z.ai" / "Codex" / "Claude" → filename.
        let name: String
        let lower = provider.lowercased()
        switch lower {
        case "z.ai", "zai", "zhipu", "bigmodel", "智谱", "智谱 glm", "glm": name = "zai"
        case "claude", "anthropic":              name = "claude"
        case "codex", "openai":                  name = "codex"
        case "gemini":                           name = "gemini"
        case "kimi":                             name = "kimi"
        default:
            // Fuzzy fallback so full names like "BigModel - Coding Plan"
            // still resolve to the right provider logo.
            if lower.contains("bigmodel") || lower.contains("zai") || lower.contains("智谱") || lower.contains("glm") {
                name = "zai"
            } else if lower.contains("claude") || lower.contains("anthropic") {
                name = "claude"
            } else if lower.contains("codex") || lower.contains("openai") {
                name = "codex"
            } else if lower.contains("gemini") {
                name = "gemini"
            } else if lower.contains("kimi") {
                name = "kimi"
            } else {
                name = lower
            }
        }
        let candidates = [
            // .app bundle
            Bundle.main.resourceURL?.appendingPathComponent("logos/\(name).png"),
            // standalone binary
            Bundle.main.bundleURL.deletingLastPathComponent()
                .appendingPathComponent("logos").appendingPathComponent("\(name).png"),
            URL(fileURLWithPath: "logos/\(name).png"),
        ].compactMap { $0 }
        for url in candidates {
            guard FileManager.default.fileExists(atPath: url.path),
                  let nsImage = NSImage(contentsOf: url) else { continue }
            let resized = NSImage(size: NSSize(width: size, height: size))
            resized.lockFocus()
            nsImage.draw(in: NSRect(x: 0, y: 0, width: size, height: size))
            resized.unlockFocus()
            return Image(nsImage: resized)
        }
        return nil
    }
}

// MARK: - File Watcher (real-time agent activity detection)
// Watches a file for write events via DispatchSource. When the agent's
// rollout/transcript file is being written to (agent is actively working),
// the handler fires immediately — no polling delay. This gives the notch
// real-time "running" status + latest content preview.
class FileWatcher {
    private var source: DispatchSourceFileSystemObject?
    private let fd: CInt
    let path: String
    let onChange: () -> Void

    init?(path: String, onChange: @escaping () -> Void) {
        self.path = path
        self.onChange = onChange
        fd = open(path, O_EVTONLY)
        guard fd >= 0 else { return nil }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .delete, .rename],
            queue: .global()
        )
        src.setEventHandler { [weak self] in
            self?.onChange()
        }
        src.setCancelHandler { [fd = self.fd] in
            close(fd)
        }
        src.resume()
        source = src
    }

    deinit { source?.cancel() }
}

// MARK: - Jump to App
// Activate the host terminal/IDE that owns a given agent session pid.
// Walks the parent-process chain until it finds a .app bundle, then
// brings that app to the foreground via NSWorkspace.
enum JumpToApp {
    enum Outcome: Equatable {
        case activatedApplication
        case openedWorkspace
        case unavailable

        var notice: String? {
            switch self {
            case .activatedApplication:
                return nil
            case .openedWorkspace:
                return "未找到原窗口，已打开项目目录"
            case .unavailable:
                return "无法定位这个 Agent 的工作现场"
            }
        }
    }

    // Return to the closest context we can prove: the host app owning the
    // live pid, then the recorded terminal/provider, and finally the exact
    // workspace directory. A stale pid no longer causes a silent no-op.
    @discardableResult
    static func jump(session: AgentSession) -> Outcome {
        if let pid = session.pid, pid > 0, jump(toPID: pid) {
            return .activatedApplication
        }
        if let application = fallbackApplication(terminal: session.terminal,
                                                 source: session.source),
           activate(application: application) {
            return .activatedApplication
        }
        if jump(toSessionID: session.id) {
            return .activatedApplication
        }
        if let cwd = session.cwd, !cwd.isEmpty,
           FileManager.default.fileExists(atPath: cwd),
           NSWorkspace.shared.open(URL(fileURLWithPath: cwd)) {
            return .openedWorkspace
        }
        return .unavailable
    }

    @discardableResult
    static func jump(toSessionID sessionID: String) -> Bool {
        // ZCode sessions use "sess_xxx" ids with no pid — activate ZCode.app directly.
        if sessionID.hasPrefix("sess_") || sessionID.hasPrefix("zcode") {
            return activate(application: "/Applications/ZCode.app")
        }
        // Codex sessions: activate ChatGPT.app (bundle id com.openai.codex)
        // since the codex process's parent chain doesn't lead to a .app.
        if sessionID.hasPrefix("codex-") {
            return activate(application: "ChatGPT")
        }
        // Gemini / DeepSeek / legacy Claude ids carry a trailing pid.
        let parts = sessionID.split(separator: "-")
        if let last = parts.last, let pid = Int32(last) {
            return jump(toPID: pid)
        }
        return false
    }

    // Pure mapping kept separate from activation so it can be regression
    // tested without launching apps.
    static func fallbackApplication(terminal: String?, source: String) -> String? {
        let terminalName = terminal?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased() ?? ""
        switch terminalName {
        case "warp": return "Warp"
        case "iterm", "iterm2", "iterm.app": return "iTerm"
        case "terminal", "terminal.app", "apple_terminal": return "Terminal"
        case "ghostty", "ghostty.app": return "Ghostty"
        case "zcode", "zcode.app": return "/Applications/ZCode.app"
        case "workbuddy", "workbuddy.app": return "WorkBuddy"
        case "vscode", "code", "visual studio code": return "Visual Studio Code"
        case "cursor", "cursor.app": return "Cursor"
        case "chatgpt", "codex": return "ChatGPT"
        default: break
        }
        switch source.lowercased() {
        case "codex": return "ChatGPT"
        case "zcode": return "/Applications/ZCode.app"
        case "workbuddy": return "WorkBuddy"
        default: return nil
        }
    }

    // Walk parent processes until we find a .app, then activate it.
    // Uses `ps` (user-space, not SIP-restricted like proc_pidpath) to read
    // the command + ppid of each process in the chain.
    @discardableResult
    static func jump(toPID pid: Int32) -> Bool {
        var current = pid
        var appPath: String?
        for _ in 0..<20 {
            guard let comm = psCommand(pid: current) else { break }
            if comm.contains(".app/") {
                // Extract the full path up to and including .app.
                // e.g. "/Applications/Warp.app/Contents/MacOS/stable" → "/Applications/Warp.app"
                if let range = comm.range(of: #".*\.app"#, options: .regularExpression) {
                    appPath = String(comm[range])
                }
                break
            }
            guard let ppid = psPPID(pid: current), ppid > 1 else { break }
            current = ppid
        }

        guard let path = appPath, FileManager.default.fileExists(atPath: path) else {
            NSLog("JumpToApp: no .app found for pid \(pid) (path=\(appPath ?? "nil"))")
            return false
        }
        return activate(application: path)
    }

    @discardableResult
    private static func activate(application: String) -> Bool {
        if application.hasPrefix("/"), !FileManager.default.fileExists(atPath: application) {
            return false
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        task.arguments = ["-a", application]
        do {
            try task.run()
            NSLog("JumpToApp: open -a \(application)")
            return true
        } catch {
            NSLog("JumpToApp: failed to open \(application): \(error.localizedDescription)")
            return false
        }
    }

    // Read a process's command via `ps` (not subject to SIP restrictions).
    private static func psCommand(pid: Int32) -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/ps")
        task.arguments = ["-p", String(pid), "-o", "command="]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle()
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // Read a process's ppid via `ps`.
    private static func psPPID(pid: Int32) -> Int32? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/ps")
        task.arguments = ["-p", String(pid), "-o", "ppid="]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle()
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let s = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              let ppid = Int32(s) else { return nil }
        return ppid
    }
}

// MARK: - Agent Session
// One live agent conversation tracked in the notch. Keyed by session_id
// in the ViewModel so SessionStart adds, SessionEnd removes, and
// UserPromptSubmit/PreToolUse/PostToolUse update the most recent task.
struct AgentSession: Identifiable {
    let id: String          // session_id from the hook event
    var source: String      // "claude" | "zcode" | "codex" | ...
    var task: String        // title: latest user prompt (first line)
    var preview: String?    // latest assistant reply / tool call (content line)
    var detail: String?     // display detail shown in UI
    var cwd: String?        // full cwd when scanner can resolve it
    var transcriptPath: String? // exact transcript/rollout path when known
    var terminal: String?   // host terminal: "warp" | "iterm" | "terminal" | "zcode" ...
    var lastUpdate: Date    // for ordering by recency + relative time display
    var isRunning: Bool     // true while agent is actively generating/executing
    var isScanManaged: Bool // true only for sessions owned by scan-agents.sh
    var hasLiveUpdates: Bool // once true, scanner reconciliation cannot delete it
    var endedAt: Date?      // set when the session ended and moved to history
    var pid: Int32?         // host process pid (for focusing its terminal)

    var providerName: String {
        switch source.lowercased() {
        case "zcode": return "ZCODE"
        case "deepseek": return "DEEPSEEK"
        default: return source.uppercased()
        }
    }

    var workspaceName: String? {
        if let cwd, !cwd.isEmpty { return (cwd as NSString).lastPathComponent }
        if let detail, !detail.isEmpty { return (detail as NSString).lastPathComponent }
        return nil
    }

    // Badge color by source — matches real app's per-agent coloring.
    var badgeColor: Color {
        switch source.lowercased() {
        case "claude":   return Color(red: 214/255, green: 140/255, blue: 89/255)   // warm orange
        case "zcode":    return Color(red: 96/255,  green: 165/255, blue: 250/255)  // blue
        case "codex":    return Color(red: 34/255,  green: 211/255, blue: 238/255)  // cyan
        case "gemini":   return Color(red: 139/255, green: 92/255,  blue: 246/255)  // purple
        case "deepseek": return Color(red: 96/255,  green: 165/255, blue: 250/255)  // blue (DeepSeek blue)
        case "kimi":     return Color(red: 236/255, green: 72/255,  blue: 153/255)  // pink
        case "grok":     return Color(red: 255/255, green: 255/255, blue: 255/255)  // white
        case "qwen":     return Color(red: 122/255, green: 110/255, blue: 245/255)  // indigo (Qwen Code)
        case "opencode": return Color(red: 245/255, green: 158/255, blue: 11/255)   // amber (OpenCode)
        case "workbuddy": return Color(red: 16/255, green: 185/255, blue: 129/255)  // emerald (WorkBuddy)
        default:         return Color(red: 156/255, green: 163/255, blue: 175/255)
        }
    }

    // Relative time like "2m" / "1h" / "3d" since lastUpdate.
    var relativeTime: String {
        let s = Int(Date().timeIntervalSince(lastUpdate))
        if s < 60 { return "now" }
        if s < 3600 { return "\(s/60)m" }
        if s < 86400 { return "\(s/3600)h" }
        return "\(s/86400)d"
    }

    // "刚刚" / "2m前" / "1h前" since the session ended (history rows).
    var endedRelativeTime: String {
        guard let endedAt else { return "" }
        let s = Int(Date().timeIntervalSince(endedAt))
        if s < 60 { return "刚刚" }
        if s < 3600 { return "\(s/60)m前" }
        if s < 86400 { return "\(s/3600)h前" }
        return "\(s/86400)d前"
    }
}

enum PendingRequestKind: Equatable {
    case approval
    case ask
}

struct AskOptionChoice: Identifiable {
    let id: String
    let label: String
    let description: String?
}

struct AskQuestionDraft: Identifiable {
    let id: String
    let header: String
    let question: String
    let options: [AskOptionChoice]
    let multiSelect: Bool
}

struct PendingRequest: Identifiable {
    let id: String
    let kind: PendingRequestKind
    let sessionID: String
    let source: String
    let agentName: String
    let taskName: String
    let targetFile: String
    let toolName: String?
    let command: String?
    let cwd: String?
    let reason: String?
    let diff: String?
    let questions: [AskQuestionDraft]
    let arrivedAt: Date
    let expiresAt: Date

    var riskAssessment: ApprovalRiskAssessment {
        ApprovalRiskAnalyzer.assess(self)
    }

    var remainingLabel: String {
        let seconds = max(0, Int(ceil(expiresAt.timeIntervalSinceNow)))
        if seconds >= 3600 { return "\(seconds / 3600)h" }
        if seconds >= 60 { return "\(Int(ceil(Double(seconds) / 60.0)))m" }
        return "\(seconds)s"
    }
}

enum ApprovalRisk: String, Codable, CaseIterable {
    case low
    case medium
    case high
    case critical

    var rank: Int {
        switch self {
        case .low: return 0
        case .medium: return 1
        case .high: return 2
        case .critical: return 3
        }
    }

    var label: String {
        switch self {
        case .low: return "低风险"
        case .medium: return "中风险"
        case .high: return "高风险"
        case .critical: return "严重风险"
        }
    }

    var allowsBatchApproval: Bool { rank <= ApprovalRisk.medium.rank }
}

struct ApprovalRiskAssessment: Equatable {
    let level: ApprovalRisk
    let reasons: [String]
}

enum ApprovalRiskAnalyzer {
    static func assess(_ request: PendingRequest) -> ApprovalRiskAssessment {
        guard request.kind == .approval else {
            return ApprovalRiskAssessment(level: .low, reasons: ["不执行工具操作"])
        }

        let tool = (request.toolName ?? "").lowercased()
        let command = (request.command ?? "").lowercased()
        var level: ApprovalRisk = .low
        var reasons: [String] = []

        func raise(_ candidate: ApprovalRisk, _ reason: String) {
            if candidate.rank > level.rank { level = candidate }
            if !reasons.contains(reason) { reasons.append(reason) }
        }

        if containsRegex(command, #"\b(git\s+reset\s+--hard|git\s+clean\s+-[^\s]*f[^\s]*d|git\s+clean\s+-[^\s]*d[^\s]*f)\b"#) {
            raise(.critical, "可能不可逆地丢弃版本控制内容")
        }
        if containsRegex(command, #"\b(curl|wget)\b[^\n|]*\|\s*(ba|z)?sh\b"#) {
            raise(.critical, "下载内容将直接交给 Shell 执行")
        }
        if containsRegex(command, #"\brm\s+-[^\s]*r[^\s]*f[^\s]*\s+(/|~|\$home)(\s|$)"#)
            || containsRegex(command, #"\brm\s+-[^\s]*f[^\s]*r[^\s]*\s+(/|~|\$home)(\s|$)"#) {
            raise(.critical, "可能删除系统或用户目录")
        }
        if command.contains("diskutil erase") || command.contains("mkfs")
            || containsRegex(command, #"\bdd\b[^\n]*\bof=/dev/"#) {
            raise(.critical, "可能覆盖磁盘或设备数据")
        }

        if containsRegex(command, #"\brm\s+-[^\s]*(r[^\s]*f|f[^\s]*r)"#) {
            raise(.high, "包含递归强制删除")
        }
        if containsRegex(command, #"(^|[;&|]\s*)sudo\s+"#) {
            raise(.high, "需要管理员权限")
        }
        if containsRegex(command, #"\bgit\s+push\b"#) {
            raise(.high, "会写入远端仓库")
        }
        if containsRegex(command, #"\b(chmod|chown)\s+-[^\s]*r"#)
            || command.contains("launchctl ") || command.contains("killall ") {
            raise(.high, "会批量修改系统状态")
        }

        if isWriteTool(tool), targetIsOutsideWorkspace(target: request.targetFile, cwd: request.cwd) {
            raise(.high, "写入位置在当前工作区之外")
        }
        if tool == "bash" || tool == "shell" || !command.isEmpty {
            raise(.medium, "将执行 Shell 命令")
        } else if isWriteTool(tool) || request.diff?.isEmpty == false {
            raise(.medium, "将修改文件内容")
        } else {
            raise(.medium, "工具会产生本机副作用")
        }

        return ApprovalRiskAssessment(level: level, reasons: reasons)
    }

    private static func containsRegex(_ value: String, _ pattern: String) -> Bool {
        value.range(of: pattern, options: .regularExpression) != nil
    }

    private static func isWriteTool(_ tool: String) -> Bool {
        ["edit", "write", "notebookedit"].contains(tool)
    }

    private static func targetIsOutsideWorkspace(target: String, cwd: String?) -> Bool {
        guard !target.isEmpty, let cwd, !cwd.isEmpty else { return false }
        let standardizedWorkspace = ((cwd as NSString).expandingTildeInPath as NSString).standardizingPath
        let expandedTarget = (target as NSString).expandingTildeInPath
        let resolvedTarget = expandedTarget.hasPrefix("/")
            ? expandedTarget
            : (standardizedWorkspace as NSString).appendingPathComponent(expandedTarget)
        let standardizedTarget = (resolvedTarget as NSString).standardizingPath
        return standardizedTarget != standardizedWorkspace
            && !standardizedTarget.hasPrefix(standardizedWorkspace + "/")
    }
}

struct ApprovalDecisionHistoryEntry: Codable, Identifiable, Equatable {
    let id: UUID
    let decidedAt: Date
    let provider: String
    let toolCategory: String
    let risk: ApprovalRisk
    let outcome: String
    let decisionSource: String

    static func sanitized(request: PendingRequest,
                          payload: [String: Any],
                          decidedAt: Date = Date()) -> ApprovalDecisionHistoryEntry {
        let rawSource = request.source.lowercased()
        let provider = ["claude", "codex", "zcode", "gemini", "deepseek", "kimi", "grok"]
            .contains(rawSource) ? rawSource : "other"
        let tool = (request.toolName ?? "").lowercased()
        let category: String
        switch tool {
        case "bash", "shell": category = "shell"
        case "edit", "write": category = "file"
        case "notebookedit": category = "notebook"
        default: category = "other"
        }
        let rawOutcome = (payload["action"] as? String)?.lowercased() ?? "deny"
        let outcome = rawOutcome == "allow" ? "allow" : "deny"
        let rawDecisionSource = (payload["decision_source"] as? String) ?? "other"
        let allowedSources = ["approval_button", "queue_button", "queue_allow_all", "timeout"]
        let decisionSource = allowedSources.contains(rawDecisionSource) ? rawDecisionSource : "other"
        return ApprovalDecisionHistoryEntry(
            id: UUID(), decidedAt: decidedAt, provider: provider,
            toolCategory: category, risk: request.riskAssessment.level,
            outcome: outcome, decisionSource: decisionSource
        )
    }

    var summaryLabel: String {
        let providerLabel = provider == "other" ? "AGENT" : provider.uppercased()
        let toolLabel: String
        switch toolCategory {
        case "shell": toolLabel = "Shell"
        case "file": toolLabel = "文件修改"
        case "notebook": toolLabel = "Notebook"
        default: toolLabel = "工具"
        }
        return "\(providerLabel) · \(toolLabel)"
    }

    var outcomeLabel: String { outcome == "allow" ? "已允许" : "已拒绝" }
}

struct DisplayChoice: Identifiable, Equatable {
    let id: String
    let label: String
}

enum SessionEventKind: String {
    case completed
    case failed
    case waiting

    var notificationTitle: String {
        switch self {
        case .completed: return "Agent 回合已完成"
        case .failed: return "Agent 执行失败"
        case .waiting: return "Agent 正在等待你"
        }
    }
}

struct SessionEvent {
    let kind: SessionEventKind
    let sessionID: String
    let source: String
}

struct RequestDecision {
    let requestID: String
    let payload: [String: Any]
}

enum ServiceHealthKind: String {
    case checking
    case ready
    case warning
    case failed
    case disabled
}

struct ServiceHealth: Equatable {
    let kind: ServiceHealthKind
    let title: String
    let detail: String

    static func checking(_ detail: String) -> ServiceHealth {
        ServiceHealth(kind: .checking, title: "检查中", detail: detail)
    }

    static func ready(_ detail: String) -> ServiceHealth {
        ServiceHealth(kind: .ready, title: "正常", detail: detail)
    }

    static func warning(_ detail: String) -> ServiceHealth {
        ServiceHealth(kind: .warning, title: "需要处理", detail: detail)
    }

    static func failed(_ detail: String) -> ServiceHealth {
        ServiceHealth(kind: .failed, title: "异常", detail: detail)
    }

    static func disabled(_ detail: String) -> ServiceHealth {
        ServiceHealth(kind: .disabled, title: "已关闭", detail: detail)
    }

    var color: Color {
        switch kind {
        case .checking: return Theme.blue
        case .ready: return Theme.green
        case .warning: return Theme.yellow
        case .failed: return Theme.red
        case .disabled: return Theme.textMuted
        }
    }

    var symbol: String {
        switch kind {
        case .checking: return "clock"
        case .ready: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .failed: return "xmark.octagon.fill"
        case .disabled: return "minus.circle"
        }
    }
}

enum IPCAuthenticationError: LocalizedError {
    case invalidToken
    case invalidPayload

    var errorDescription: String? {
        switch self {
        case .invalidToken: return "IPC 密钥格式无效"
        case .invalidPayload: return "IPC 消息无法签名"
        }
    }
}

final class IPCAuthenticator {
    private let key: SymmetricKey
    private var consumedNonces = Set<String>()
    private var nonceOrder: [String] = []
    private let maxRememberedNonces = 2_000

    init(token: Data) throws {
        guard token.count == 32 else { throw IPCAuthenticationError.invalidToken }
        key = SymmetricKey(data: token)
    }

    static func loadOrCreate() throws -> IPCAuthenticator {
        let environment = ProcessInfo.processInfo.environment
        let configuredPath = environment["VIBE_ISLAND_IPC_TOKEN_FILE"]
            ?? (NSHomeDirectory() + "/.vibe-island/run/ipc-token")
        let tokenURL = URL(fileURLWithPath: configuredPath)
        let directoryURL = tokenURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        chmod(directoryURL.path, 0o700)

        let token: Data
        if FileManager.default.fileExists(atPath: tokenURL.path) {
            let encoded = try String(contentsOf: tokenURL, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard let decoded = Data(hexString: encoded), decoded.count == 32 else {
                throw IPCAuthenticationError.invalidToken
            }
            token = decoded
        } else {
            let generated = SymmetricKey(size: .bits256)
            token = generated.withUnsafeBytes { Data($0) }
            try token.hexString.write(to: tokenURL, atomically: true, encoding: .utf8)
        }
        chmod(tokenURL.path, S_IRUSR | S_IWUSR)
        return try IPCAuthenticator(token: token)
    }

    func signedPayload(_ payload: [String: Any]) throws -> [String: Any] {
        var signed = payload
        signed["auth_nonce"] = UUID().uuidString.lowercased()
        let canonical = try canonicalData(for: signed)
        let code = HMAC<SHA256>.authenticationCode(for: canonical, using: key)
        signed["auth_signature"] = Data(code).hexString
        return signed
    }

    func verifyAndConsume(_ payload: [String: Any]) -> Bool {
        guard let nonce = payload["auth_nonce"] as? String, !nonce.isEmpty,
              let signature = payload["auth_signature"] as? String,
              let signatureData = Data(hexString: signature), signatureData.count == 32,
              !consumedNonces.contains(nonce),
              let canonical = try? canonicalData(for: payload),
              HMAC<SHA256>.isValidAuthenticationCode(
                signatureData,
                authenticating: canonical,
                using: key
              ) else {
            return false
        }

        consumedNonces.insert(nonce)
        nonceOrder.append(nonce)
        if nonceOrder.count > maxRememberedNonces {
            consumedNonces.remove(nonceOrder.removeFirst())
        }
        return true
    }

    private func canonicalData(for payload: [String: Any]) throws -> Data {
        var unsigned = payload
        unsigned.removeValue(forKey: "auth_signature")
        guard JSONSerialization.isValidJSONObject(unsigned) else {
            throw IPCAuthenticationError.invalidPayload
        }
        return try JSONSerialization.data(
            withJSONObject: unsigned,
            options: [.sortedKeys, .withoutEscapingSlashes]
        )
    }
}

private extension Data {
    init?(hexString: String) {
        guard hexString.count.isMultiple(of: 2) else { return nil }
        var bytes = [UInt8]()
        bytes.reserveCapacity(hexString.count / 2)
        var index = hexString.startIndex
        while index < hexString.endIndex {
            let next = hexString.index(index, offsetBy: 2)
            guard let byte = UInt8(hexString[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self = Data(bytes)
    }

    var hexString: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

final class NotificationCoordinator: NSObject, UNUserNotificationCenterDelegate {
    var onOpenRequest: ((String) -> Void)?
    var onOpenSession: ((String) -> Void)?

    func post(title: String,
              body: String,
              identifier: String? = nil,
              soundsEnabled: Bool,
              userInfo: [String: Any] = [:]) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.userInfo = userInfo
        content.sound = soundsEnabled ? .default : nil
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(
                identifier: identifier ?? UUID().uuidString,
                content: content,
                trigger: nil
            )
        )
    }

    func configure(enabled: Bool) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        guard enabled else { return }
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    func post(request: PendingRequest, soundsEnabled: Bool) {
        post(
            title: request.kind == .approval ? "有新的授权请求" : "Agent 需要你的回答",
            body: "\(request.agentName)：\(request.taskName)",
            identifier: "request-\(request.id)",
            soundsEnabled: soundsEnabled,
            userInfo: ["request_id": request.id]
        )
    }

    func post(event: SessionEvent, soundsEnabled: Bool) {
        let rawSource = event.source.lowercased()
        let provider = ["claude", "codex", "zcode", "gemini", "deepseek", "kimi", "grok"]
            .contains(rawSource) ? rawSource.capitalized : "Agent"
        post(
            title: event.kind.notificationTitle,
            body: "\(provider) · 点击返回工作现场",
            identifier: "session-\(event.kind.rawValue)-\(event.sessionID)",
            soundsEnabled: soundsEnabled,
            userInfo: ["session_id": event.sessionID]
        )
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        var options: UNNotificationPresentationOptions = [.banner]
        if notification.request.content.sound != nil {
            options.insert(.sound)
        }
        completionHandler(options)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let requestID = response.notification.request.content.userInfo["request_id"] as? String {
            onOpenRequest?(requestID)
        }
        if let sessionID = response.notification.request.content.userInfo["session_id"] as? String {
            onOpenSession?(sessionID)
        }
        completionHandler()
    }
}

// MARK: - View Model
class NotchViewModel: ObservableObject {
    private enum PreferenceKey {
        static let notificationsEnabled = "notificationsEnabled"
        static let notificationSoundsEnabled = "notificationSoundsEnabled"
        static let completionNotificationsEnabled = "completionNotificationsEnabled"
        static let failureNotificationsEnabled = "failureNotificationsEnabled"
        static let waitingNotificationsEnabled = "waitingNotificationsEnabled"
        static let autoStartUsage = "autoStartUsage"
        static let decisionHistoryEnabled = "decisionHistoryEnabled"
        static let approvalDecisionHistory = "approvalDecisionHistoryV1"
        static let displaySelection = "displaySelection"
    }

    private static let decisionHistoryLimit = 30

    private let defaults: UserDefaults

    // User interaction state.
    @Published var isHovered: Bool = false
    @Published var isPinned: Bool = false   // manualExpanded: click to lock open
    @Published private(set) var pendingRequests: [PendingRequest] = []
    @Published private(set) var askQuestionIndex = 0
    @Published private(set) var askSelections: [String: Set<String>] = [:]
    @Published var compactTaskName: String = "Idle"
    @Published var isScanningSessions = false
    @Published private(set) var transientNotice: String?
    // Live agent sessions (multi-agent list in overview).
    @Published var sessions: [String: AgentSession] = [:]
    // Per-provider usage snapshots (supports multi-platform rotation).
    // Empty = no usage daemon running yet.
    @Published var providers: [UsageSnapshot] = []
    // Currently-shown provider index (rotated by timer / tap).
    @Published var currentProviderIndex: Int = 0
    @Published private(set) var approvalDecisionHistory: [ApprovalDecisionHistoryEntry] = []
    @Published private(set) var availableDisplays: [DisplayChoice] = []
    @Published var notificationsEnabled: Bool = true {
        didSet {
            defaults.set(notificationsEnabled, forKey: PreferenceKey.notificationsEnabled)
            onNotificationSettingsChanged?()
        }
    }
    @Published var notificationSoundsEnabled: Bool = true {
        didSet {
            defaults.set(notificationSoundsEnabled, forKey: PreferenceKey.notificationSoundsEnabled)
            onNotificationSettingsChanged?()
        }
    }
    @Published var completionNotificationsEnabled: Bool = false {
        didSet {
            defaults.set(completionNotificationsEnabled,
                         forKey: PreferenceKey.completionNotificationsEnabled)
        }
    }
    @Published var failureNotificationsEnabled: Bool = true {
        didSet {
            defaults.set(failureNotificationsEnabled,
                         forKey: PreferenceKey.failureNotificationsEnabled)
        }
    }
    @Published var waitingNotificationsEnabled: Bool = true {
        didSet {
            defaults.set(waitingNotificationsEnabled,
                         forKey: PreferenceKey.waitingNotificationsEnabled)
        }
    }
    @Published var decisionHistoryEnabled: Bool = true {
        didSet {
            defaults.set(decisionHistoryEnabled, forKey: PreferenceKey.decisionHistoryEnabled)
            if !decisionHistoryEnabled { clearApprovalDecisionHistory() }
        }
    }
    @Published var displaySelection: String = "automatic" {
        didSet {
            guard displaySelection != oldValue else { return }
            defaults.set(displaySelection, forKey: PreferenceKey.displaySelection)
            onDisplaySelectionChanged?()
        }
    }
    @Published var autoStartUsage: Bool = true {
        didSet {
            defaults.set(autoStartUsage, forKey: PreferenceKey.autoStartUsage)
            onAutoUsageChanged?(autoStartUsage)
        }
    }
    @Published var ipcHealth = ServiceHealth.checking("正在启动本地 IPC")
    @Published var hookHealth = ServiceHealth.checking("正在检查 Claude Code Hook")
    @Published var usageHealth = ServiceHealth.checking("等待配额服务")

    var onDecision: ((RequestDecision) -> Void)?
    var onRequestEnqueued: ((PendingRequest) -> Void)?
    var onOpenSettings: (() -> Void)?
    var onRefreshSessions: (() -> Void)?
    var onInstallHook: (() -> Void)?
    var onRestartUsage: (() -> Void)?
    var onNotificationSettingsChanged: (() -> Void)?
    var onAutoUsageChanged: ((Bool) -> Void)?
    var onDisplaySelectionChanged: (() -> Void)?
    var onSessionEvent: ((SessionEvent) -> Void)?
    var onQuitRequested: (() -> Void)?

    private var collapseWorkItem: DispatchWorkItem?
    private var requestExpiryWorkItems: [String: DispatchWorkItem] = [:]
    private var noticeWorkItem: DispatchWorkItem?

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        // Remove the unsafe P1 prototype preference. Tool-wide grants (for
        // example every future Bash command) are intentionally unsupported.
        defaults.removeObject(forKey: "alwaysAllowedTools")
        if defaults.object(forKey: PreferenceKey.notificationsEnabled) != nil {
            notificationsEnabled = defaults.bool(forKey: PreferenceKey.notificationsEnabled)
        }
        if defaults.object(forKey: PreferenceKey.notificationSoundsEnabled) != nil {
            notificationSoundsEnabled = defaults.bool(forKey: PreferenceKey.notificationSoundsEnabled)
        }
        if defaults.object(forKey: PreferenceKey.completionNotificationsEnabled) != nil {
            completionNotificationsEnabled = defaults.bool(
                forKey: PreferenceKey.completionNotificationsEnabled
            )
        }
        if defaults.object(forKey: PreferenceKey.failureNotificationsEnabled) != nil {
            failureNotificationsEnabled = defaults.bool(
                forKey: PreferenceKey.failureNotificationsEnabled
            )
        }
        if defaults.object(forKey: PreferenceKey.waitingNotificationsEnabled) != nil {
            waitingNotificationsEnabled = defaults.bool(
                forKey: PreferenceKey.waitingNotificationsEnabled
            )
        }
        if defaults.object(forKey: PreferenceKey.autoStartUsage) != nil {
            autoStartUsage = defaults.bool(forKey: PreferenceKey.autoStartUsage)
        }
        if defaults.object(forKey: PreferenceKey.decisionHistoryEnabled) != nil {
            decisionHistoryEnabled = defaults.bool(forKey: PreferenceKey.decisionHistoryEnabled)
        }
        if let storedSelection = defaults.string(forKey: PreferenceKey.displaySelection),
           storedSelection == "automatic" || storedSelection == "main"
            || storedSelection == "pointer" || storedSelection.hasPrefix("display:") {
            displaySelection = storedSelection
        }
        if decisionHistoryEnabled,
           let historyData = defaults.data(forKey: PreferenceKey.approvalDecisionHistory),
           let decoded = try? JSONDecoder().decode(
               [ApprovalDecisionHistoryEntry].self, from: historyData
           ) {
            approvalDecisionHistory = Array(decoded.prefix(Self.decisionHistoryLimit))
        } else if defaults.data(forKey: PreferenceKey.approvalDecisionHistory) != nil {
            defaults.removeObject(forKey: PreferenceKey.approvalDecisionHistory)
        }
    }

    func updateAvailableDisplays(_ displays: [DisplayChoice]) {
        availableDisplays = displays
    }

    func clearApprovalDecisionHistory() {
        approvalDecisionHistory = []
        defaults.removeObject(forKey: PreferenceKey.approvalDecisionHistory)
    }

    func shouldNotify(for event: SessionEvent) -> Bool {
        guard notificationsEnabled else { return false }
        switch event.kind {
        case .completed: return completionNotificationsEnabled
        case .failed: return failureNotificationsEnabled
        case .waiting: return waitingNotificationsEnabled
        }
    }

    // Convenience: the currently displayed provider, if any.
    var currentUsage: UsageSnapshot? {
        guard !providers.isEmpty, currentProviderIndex < providers.count else { return nil }
        return providers[currentProviderIndex]
    }

    // Upsert a provider's snapshot (keyed by provider name).
    func upsertProvider(_ snap: UsageSnapshot) {
        if let i = providers.firstIndex(where: { $0.provider == snap.provider }) {
            providers[i] = snap
        } else {
            providers.append(snap)
        }
    }

    enum NotchState {
        case compact
        case overview
        case approval
        case ask
    }

    var currentRequest: PendingRequest? {
        pendingRequests.first
    }

    var requestState: NotchState? {
        guard let request = currentRequest else { return nil }
        return request.kind == .approval ? .approval : .ask
    }

    var currentAskQuestion: AskQuestionDraft? {
        guard let request = currentRequest, request.kind == .ask,
              request.questions.indices.contains(askQuestionIndex) else {
            return nil
        }
        return request.questions[askQuestionIndex]
    }

    // Sessions sorted by most-recently-updated first (for the list).
    var recentSessions: [AgentSession] {
        sessions.values.sorted { $0.lastUpdate > $1.lastUpdate }
    }

    var runningSessions: [AgentSession] {
        recentSessions.filter(\.isRunning)
    }

    var idleSessions: [AgentSession] {
        recentSessions.filter { !$0.isRunning }
    }

    // The session currently driving the compact display: only one that's
    // actively running (isRunning=true, set by live hook events).
    var runningSession: AgentSession? {
        recentSessions.first { $0.isRunning }
    }

    var activeSessionCount: Int {
        recentSessions.filter(\.isRunning).count
    }

    var compactOverflowCount: Int {
        max(0, activeSessionCount - 1)
    }

    var batchApprovableRequests: [PendingRequest] {
        pendingRequests.filter {
            $0.kind == .approval && $0.riskAssessment.level.allowsBatchApproval
        }
    }

    var blockedBatchApprovalCount: Int {
        pendingRequests.filter {
            $0.kind == .approval && !$0.riskAssessment.level.allowsBatchApproval
        }.count
    }

    var overviewSessionCountLabel: String {
        sessions.isEmpty ? "暂无会话" : "\(activeSessionCount) 运行中 · \(sessions.count) 个会话"
    }

    var statusSummary: String {
        [ipcHealth.title, hookHealth.title, usageHealth.title].joined(separator: " · ")
    }

    // Compact label: only show content when an agent is actually running.
    // Prefer the live status pushed by the IPC/hook channel (lowest latency),
    // then the file-polled preview, then the task title.
    var compactLabel: String {
        if let transientNotice { return transientNotice }
        if let active = runningSession {
            if let p = active.preview, !p.isEmpty {
                return p
            }
            return active.task.isEmpty ? active.source.capitalized : active.task
        }
        return "Idle"
    }

    // Pulse animation only plays while an agent is running.
    var compactIsActive: Bool {
        runningSession != nil
    }

    // Register/update a session from a hook event.
    // lastUpdate: if provided (from transcript timestamp), use it so
    // periodic rescans don't reset the relative-time display to "now".
    func upsertSession(id: String, source: String, task: String,
                       detail: String? = nil, preview: String? = nil,
                       cwd: String? = nil, transcriptPath: String? = nil,
                       terminal: String? = nil, lastUpdate: Date? = nil,
                       isRunning: Bool? = nil, isScanManaged: Bool? = nil,
                       pid: Int32? = nil,
                       isLiveUpdate: Bool = false,
                       touchTimestamp: Bool = true) {
        guard !id.isEmpty else { return }
        var s = sessions[id] ?? AgentSession(id: id, source: source, task: task,
                                             preview: preview, detail: detail,
                                             cwd: cwd, transcriptPath: transcriptPath,
                                             terminal: terminal,
                                             lastUpdate: lastUpdate ?? Date(),
                                             isRunning: isRunning ?? false,
                                             isScanManaged: isScanManaged ?? false,
                                             hasLiveUpdates: isLiveUpdate)
        s.source = source
        s.task = task
        if let detail = detail, !detail.isEmpty { s.detail = detail }
        if let preview = preview, !preview.isEmpty { s.preview = preview }
        if let cwd = cwd, !cwd.isEmpty { s.cwd = cwd }
        if let transcriptPath = transcriptPath, !transcriptPath.isEmpty { s.transcriptPath = transcriptPath }
        if let terminal = terminal, !terminal.isEmpty { s.terminal = terminal }
        if let ts = lastUpdate {
            s.lastUpdate = ts
        } else if touchTimestamp {
            s.lastUpdate = Date()
        }
        if let running = isRunning { s.isRunning = running }
        if isLiveUpdate {
            s.hasLiveUpdates = true
            s.isScanManaged = false
        } else if let scanManaged = isScanManaged, !s.hasLiveUpdates {
            s.isScanManaged = scanManaged
        }
        if let pid = pid { s.pid = pid }
        // A live update means the session is active again — drop any prior
        // history entry so it isn't shown twice.
        sessionHistory.removeAll { $0.id == id }
        sessions[id] = s
    }

    // Ended sessions are retained (capped) in history instead of vanishing,
    // so a user tabbing back can still see what just finished.
    @Published private(set) var sessionHistory: [AgentSession] = []

    func removeSession(id: String) {
        guard let sess = sessions.removeValue(forKey: id) else { return }
        var ended = sess
        ended.isRunning = false
        ended.endedAt = Date()
        sessionHistory.removeAll { $0.id == id }
        sessionHistory.insert(ended, at: 0)
        if sessionHistory.count > 5 {
            sessionHistory.removeLast()
        }
    }

    func reconcileScannedSessions(seenIDs: Set<String>) -> [String] {
        let staleIDs = sessions.values
            .filter { $0.isScanManaged && !seenIDs.contains($0.id) }
            .map(\.id)
        for id in staleIDs {
            removeSession(id: id)
        }
        return staleIDs
    }

    // Priority resolution (mirrors real app intents):
    // pending request (approval/ask) > pinned/hovered (expand) > compact.
    var activeState: NotchState {
        if let req = requestState { return req }
        if isPinned || isHovered { return .overview }
        return .compact
    }

    // Hover tracking with a 300ms grace before collapse, so quick edge
    // excursions don't make the notch flicker.
    func setHovered(_ hovered: Bool) {
        if hovered {
            collapseWorkItem?.cancel()
            isHovered = true
        } else {
            collapseWorkItem?.cancel()
            let work = DispatchWorkItem { [weak self] in self?.isHovered = false }
            collapseWorkItem = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: work)
        }
    }

    // Click the notch to pin/unpin the expanded view (no-op while a
    // request is pending, so approval/ask buttons keep working).
    func togglePin() {
        guard currentRequest == nil else { return }
        isPinned.toggle()
    }

    func enqueueRequest(_ request: PendingRequest) {
        requestExpiryWorkItems[request.id]?.cancel()
        let expiryWorkItem = DispatchWorkItem { [weak self] in
            self?.expireRequest(id: request.id)
        }
        requestExpiryWorkItems[request.id] = expiryWorkItem
        DispatchQueue.main.asyncAfter(
            deadline: .now() + max(0, request.expiresAt.timeIntervalSinceNow),
            execute: expiryWorkItem
        )

        if let index = pendingRequests.firstIndex(where: { $0.id == request.id }) {
            pendingRequests[index] = request
            if index == 0 {
                resetAskProgress()
            }
        } else {
            let wasEmpty = pendingRequests.isEmpty
            pendingRequests.append(request)
            onRequestEnqueued?(request)
            if wasEmpty {
                resetAskProgress()
            }
        }
    }

    func removeRequest(id: String) {
        requestExpiryWorkItems.removeValue(forKey: id)?.cancel()
        let removedCurrent = pendingRequests.first?.id == id
        pendingRequests.removeAll { $0.id == id }
        if removedCurrent {
            resetAskProgress()
        }
    }

    func removeRequests(sessionID: String) {
        guard !sessionID.isEmpty else { return }
        let previousCurrent = pendingRequests.first?.id
        let removedIDs = pendingRequests
            .filter { $0.sessionID == sessionID }
            .map(\.id)
        removedIDs.forEach { requestExpiryWorkItems.removeValue(forKey: $0)?.cancel() }
        pendingRequests.removeAll { $0.sessionID == sessionID }
        if previousCurrent != pendingRequests.first?.id {
            resetAskProgress()
        }
    }

    func removeRequests(ids: Set<String>) {
        guard !ids.isEmpty else { return }
        let previousCurrent = pendingRequests.first?.id
        ids.forEach { requestExpiryWorkItems.removeValue(forKey: $0)?.cancel() }
        pendingRequests.removeAll { ids.contains($0.id) }
        if previousCurrent != pendingRequests.first?.id {
            resetAskProgress()
        }
    }

    func respondToApproval(action: String) {
        guard currentRequest?.kind == .approval,
              action == "allow" || action == "deny" else { return }
        playInteractionSound()
        respondToCurrent(payload: ["action": action, "decision_source": "approval_button"])
    }

    // Decide a specific request by id — used by the queue list's per-row
    // buttons, where each request is acted on independently rather than only
    // the front one. Keeps the panel pinned while more remain.
    func respondToRequest(id: String, action: String) {
        guard let req = pendingRequests.first(where: { $0.id == id }),
              req.kind == .approval,
              action == "allow" || action == "deny" else { return }
        playInteractionSound()
        emitDecision(for: req,
                     payload: ["action": action, "decision_source": "queue_button"])
        removeRequest(id: id)
        if pendingRequests.isEmpty { isPinned = false }
    }

    // Batch approval deliberately excludes high/critical risk requests. They
    // remain visible until the user makes an explicit per-request decision.
    func allowAllPending() {
        let requests = batchApprovableRequests
        guard !requests.isEmpty else { return }
        playInteractionSound()
        for request in requests {
            emitDecision(
                for: request,
                payload: ["action": "allow", "decision_source": "queue_allow_all"]
            )
        }
        removeRequests(ids: Set(requests.map(\.id)))
        if pendingRequests.isEmpty { isPinned = false }
    }

    func chooseAskOption(_ option: AskOptionChoice) {
        guard let question = currentAskQuestion else { return }
        var selected = askSelections[question.id] ?? []
        if question.multiSelect {
            if selected.contains(option.label) {
                selected.remove(option.label)
            } else {
                selected.insert(option.label)
            }
            askSelections[question.id] = selected
            playInteractionSound()
            return
        }

        askSelections[question.id] = [option.label]
        playInteractionSound()
        advanceOrSubmitAsk()
    }

    func isAskOptionSelected(_ option: AskOptionChoice) -> Bool {
        guard let question = currentAskQuestion else { return false }
        return askSelections[question.id]?.contains(option.label) ?? false
    }

    func submitMultiSelectAnswer() {
        guard let question = currentAskQuestion, question.multiSelect,
              !(askSelections[question.id] ?? []).isEmpty else { return }
        playInteractionSound()
        advanceOrSubmitAsk()
    }

    func previousAskQuestion() {
        guard askQuestionIndex > 0 else { return }
        askQuestionIndex -= 1
    }

    func denyCurrentRequest() {
        guard let request = currentRequest else {
            isPinned = false
            return
        }
        playInteractionSound()
        if request.kind == .ask {
            respondToCurrent(payload: ["action": "cancel", "reason": "user_denied"])
        } else {
            respondToCurrent(payload: [
                "action": "deny",
                "reason": "user_denied",
                "decision_source": "approval_button",
            ])
        }
    }

    func playInteractionSound() {
        guard notificationSoundsEnabled else { return }
        NSSound(named: NSSound.Name("Pop"))?.play()
    }

    func openSettings() {
        onOpenSettings?()
    }

    func refreshSessions() {
        onRefreshSessions?()
    }

    func installHook() {
        onInstallHook?()
    }

    func restartUsage() {
        onRestartUsage?()
    }

    func quitApp() {
        onQuitRequested?()
    }

    func returnToSession(_ session: AgentSession) {
        if let notice = JumpToApp.jump(session: session).notice {
            showTransientNotice(notice)
        }
    }

    func returnToSession(id: String) {
        guard let session = sessions[id]
                ?? sessionHistory.first(where: { $0.id == id }) else {
            showTransientNotice("会话已结束，无法定位工作现场")
            return
        }
        returnToSession(session)
    }

    func focusRequest(id: String) {
        guard let index = pendingRequests.firstIndex(where: { $0.id == id }) else { return }
        if index != 0 {
            let request = pendingRequests.remove(at: index)
            pendingRequests.insert(request, at: 0)
            resetAskProgress()
        }
        isPinned = true
    }

    // Cycle through the queued requests (approval/ask). Each focused request
    // is pulled to the front, so the user can step through a backlog one at
    // a time.
    func focusNextRequest() {
        guard let current = currentRequest,
              let idx = pendingRequests.firstIndex(where: { $0.id == current.id }),
              pendingRequests.count > 1 else { return }
        let next = pendingRequests[(idx + 1) % pendingRequests.count]
        focusRequest(id: next.id)
    }

    func focusPreviousRequest() {
        guard let current = currentRequest,
              let idx = pendingRequests.firstIndex(where: { $0.id == current.id }),
              pendingRequests.count > 1 else { return }
        let prev = pendingRequests[(idx - 1 + pendingRequests.count) % pendingRequests.count]
        focusRequest(id: prev.id)
    }

    private func advanceOrSubmitAsk() {
        guard let request = currentRequest, request.kind == .ask else { return }
        if askQuestionIndex + 1 < request.questions.count {
            askQuestionIndex += 1
            return
        }

        var answers: [String: Any] = [:]
        for question in request.questions {
            let labels = Array(askSelections[question.id] ?? []).sorted()
            guard !labels.isEmpty else { return }
            answers[question.id] = question.multiSelect ? labels : labels[0]
        }
        respondToCurrent(payload: ["answers": answers])
    }

    private func respondToCurrent(payload: [String: Any]) {
        guard let currentRequest else { return }
        emitDecision(for: currentRequest, payload: payload)
        removeRequest(id: currentRequest.id)
        isPinned = false
    }

    private func expireRequest(id: String) {
        guard let request = pendingRequests.first(where: { $0.id == id }) else { return }
        let payload: [String: Any] = request.kind == .ask
            ? ["action": "cancel", "reason": "request_timeout", "decision_source": "timeout"]
            : ["action": "deny", "reason": "request_timeout", "decision_source": "timeout"]
        emitDecision(for: request, payload: payload)
        removeRequest(id: id)
        showTransientNotice("请求已超时并自动拒绝")
    }

    private func emitDecision(for request: PendingRequest, payload: [String: Any]) {
        if request.kind == .approval, decisionHistoryEnabled {
            approvalDecisionHistory.insert(
                .sanitized(request: request, payload: payload), at: 0
            )
            if approvalDecisionHistory.count > Self.decisionHistoryLimit {
                approvalDecisionHistory.removeLast(
                    approvalDecisionHistory.count - Self.decisionHistoryLimit
                )
            }
            if let encoded = try? JSONEncoder().encode(approvalDecisionHistory) {
                defaults.set(encoded, forKey: PreferenceKey.approvalDecisionHistory)
            }
        }
        onDecision?(RequestDecision(requestID: request.id, payload: payload))
    }

    private func showTransientNotice(_ message: String) {
        noticeWorkItem?.cancel()
        transientNotice = message
        let workItem = DispatchWorkItem { [weak self] in
            self?.transientNotice = nil
        }
        noticeWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: workItem)
    }

    private func resetAskProgress() {
        askQuestionIndex = 0
        askSelections = [:]
    }
}

// MARK: - Local Server (IPC)
class LocalServer {
    private var listener: NWListener
    let viewModel: NotchViewModel
    private let authenticator: IPCAuthenticator
    private let listeningPort: UInt16
    private let ioQueue = DispatchQueue(label: "vibe-island.local-server")
    private var startAttempts = 0
    private let maxStartAttempts = 6

    private final class ClientContext {
        let id: UUID
        let connection: NWConnection
        var buffer = Data()
        var requestIDs = Set<String>()

        init(id: UUID, connection: NWConnection) {
            self.id = id
            self.connection = connection
        }
    }

    private var clients: [UUID: ClientContext] = [:]
    private var requestToClient: [String: UUID] = [:]
    private var requestToSession: [String: String] = [:]

    init(viewModel: NotchViewModel, port: UInt16,
         authenticator: IPCAuthenticator) throws {
        self.viewModel = viewModel
        self.listeningPort = port
        self.authenticator = authenticator
        self.listener = try Self.makeListener(port: port)
        self.viewModel.onDecision = { [weak self] decision in
            self?.sendDecision(decision)
        }
    }

    private static func makeListener(port: UInt16) throws -> NWListener {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(
            host: NWEndpoint.Host("127.0.0.1"),
            port: NWEndpoint.Port(rawValue: port)!
        )
        return try NWListener(using: parameters)
    }

    func start() {
        attachAndStart()
    }

    private func attachAndStart() {
        listener.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                self.startAttempts = 0
                DispatchQueue.main.async {
                    self.viewModel.ipcHealth = .ready("127.0.0.1:\(self.listeningPort)")
                }
                NSLog("Vibe Island IPC ready on 127.0.0.1:\(self.listeningPort)")
            case .failed(let error):
                self.scheduleRestart(reason: error.localizedDescription)
            case .waiting(let error):
                DispatchQueue.main.async {
                    self.viewModel.ipcHealth = .warning("IPC 等待中：\(error.localizedDescription)")
                }
            default:
                break
            }
        }
        listener.newConnectionHandler = { [weak self] connection in
            self?.handleConnection(connection)
        }
        listener.start(queue: ioQueue)
    }

    // NWListener can't be restarted once failed, so on failure (e.g. the port
    // was briefly held at launch) we recreate it and retry a few times instead
    // of giving up and leaving approvals nowhere to land.
    private func scheduleRestart(reason: String) {
        guard startAttempts < maxStartAttempts else {
            DispatchQueue.main.async {
                self.viewModel.ipcHealth = .failed("端口 \(self.listeningPort) 启动失败：\(reason)")
            }
            return
        }
        startAttempts += 1
        let attempt = startAttempts
        DispatchQueue.main.async {
            self.viewModel.ipcHealth = .warning("IPC 启动失败，重试中（\(attempt)/\(self.maxStartAttempts)）：\(reason)")
        }
        listener.stateUpdateHandler = nil
        listener.newConnectionHandler = nil
        listener.cancel()
        guard let next = try? Self.makeListener(port: listeningPort) else {
            DispatchQueue.main.async {
                self.viewModel.ipcHealth = .failed("端口 \(self.listeningPort) 无法创建监听器")
            }
            return
        }
        listener = next
        ioQueue.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.attachAndStart()
        }
    }

    private func handleConnection(_ connection: NWConnection) {
        let clientID = UUID()
        clients[clientID] = ClientContext(id: clientID, connection: connection)
        connection.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .failed, .cancelled:
                self.cleanupClient(clientID)
            default:
                break
            }
        }
        connection.start(queue: ioQueue)
        receiveData(for: clientID)
    }

    private func receiveData(for clientID: UUID) {
        guard let context = clients[clientID] else { return }
        let connection = context.connection
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, context, isComplete, error in
            guard let self, let client = self.clients[clientID] else { return }
            if let data, !data.isEmpty {
                client.buffer.append(data)
                self.processBufferedLines(for: clientID)
            }
            if isComplete || error != nil {
                if !client.buffer.isEmpty {
                    self.processLine(client.buffer, from: clientID)
                    client.buffer.removeAll(keepingCapacity: false)
                }
                self.cleanupClient(clientID)
            } else {
                self.receiveData(for: clientID)
            }
        }
    }

    private func processBufferedLines(for clientID: UUID) {
        guard let client = clients[clientID] else { return }
        while let newline = client.buffer.firstIndex(of: 0x0A) {
            let line = Data(client.buffer[..<newline])
            client.buffer.removeSubrange(...newline)
            if !line.isEmpty {
                processLine(line, from: clientID)
            }
        }
    }

    private func sendDecision(_ decision: RequestDecision) {
        ioQueue.async { [weak self] in
            guard let self,
                  let clientID = self.requestToClient[decision.requestID],
                  let client = self.clients[clientID] else {
                return
            }
            var payload = decision.payload
            payload["request_id"] = decision.requestID
            guard let signedPayload = try? self.authenticator.signedPayload(payload),
                  JSONSerialization.isValidJSONObject(signedPayload),
                  let data = try? JSONSerialization.data(withJSONObject: signedPayload) else {
                self.cleanupClient(clientID)
                return
            }
            var framed = data
            framed.append(0x0A)
            client.connection.send(content: framed, completion: .contentProcessed { [weak self] _ in
                guard let self else { return }
                self.ioQueue.async {
                    self.requestToClient.removeValue(forKey: decision.requestID)
                    self.requestToSession.removeValue(forKey: decision.requestID)
                    client.requestIDs.remove(decision.requestID)
                    self.cleanupClient(clientID)
                }
            })
        }
    }

    private func processLine(_ data: Data, from clientID: UUID) {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            NSLog("LocalServer: invalid JSON frame; closing client")
            cleanupClient(clientID)
            return
        }
        guard authenticator.verifyAndConsume(json) else {
            NSLog("LocalServer: rejected unauthenticated or replayed IPC frame")
            cleanupClient(clientID)
            return
        }

        // Usage updates come from usage-daemon.py with a distinct type so
        // they don't interfere with state transitions.
        if (json["type"] as? String) == "usage" {
            if let u = json["usage"] as? [String: Any] {
                let provider = (u["provider"] as? String) ?? "Z.ai"
                let snap = UsageSnapshot(
                    id: provider,
                    provider: provider,
                    fiveHour: u["five_hour"] as? Int,
                    fiveHourReset: u["five_hour_reset"] as? String,
                    sevenDay: u["seven_day"] as? Int,
                    sevenDayReset: u["seven_day_reset"] as? String,
                    monthly:  u["monthly"] as? Int,
                    monthlyReset: u["monthly_reset"] as? String,
                    level:    u["level"] as? String,
                    plan:     u["plan"] as? String,
                    credits:  u["credits"] as? String
                )
                DispatchQueue.main.async {
                    self.viewModel.upsertProvider(snap)
                    self.viewModel.usageHealth = .ready("配额数据已更新")
                    // If current index is out of range (provider removed), reset.
                    if self.viewModel.currentProviderIndex >= self.viewModel.providers.count {
                        self.viewModel.currentProviderIndex = 0
                    }
                }
            }
            return
        }

        if (json["type"] as? String) == "usage_status" {
            let status = (json["status"] as? String) ?? "fetch_error"
            let detail = (json["detail"] as? String) ?? status
            DispatchQueue.main.async {
                switch status {
                case "starting":
                    self.viewModel.usageHealth = .checking("正在启动配额服务")
                case "ready":
                    self.viewModel.usageHealth = .ready("配额服务已连接")
                case "unconfigured":
                    self.viewModel.usageHealth = .warning("未找到 Z.ai API 配置")
                case "already_running":
                    self.viewModel.usageHealth = .warning("已有用量服务实例正在运行")
                default:
                    self.viewModel.usageHealth = .failed("配额刷新失败：\(detail)")
                }
            }
            return
        }

        let state = (json["state"] as? String)
                    ?? ((json["type"] as? String) == "request"
                        ? json["request_kind"] as? String : nil)
        if state == "approval" || state == "ask" {
            enqueueInteractiveRequest(json, state: state!, from: clientID)
            return
        }

        if let action = json["session"] as? String {
            let sessionID = (json["session_id"] as? String) ?? ""
            let source = (json["source"] as? String) ?? "claude"
            let sessionEvent = (json["event_kind"] as? String)
                .flatMap(SessionEventKind.init(rawValue:))
            if action == "end" {
                cancelRequests(sessionID: sessionID)
            }
            DispatchQueue.main.async {
                switch action {
                case "end":
                    self.viewModel.removeSession(id: sessionID)
                    self.viewModel.removeRequests(sessionID: sessionID)
                default:
                    self.viewModel.upsertSession(
                        id: sessionID,
                        source: source,
                        task: (json["task"] as? String) ?? "",
                        detail: json["detail"] as? String,
                        cwd: json["cwd"] as? String,
                        transcriptPath: json["transcript_path"] as? String,
                        isRunning: json["running"] as? Bool,
                        isLiveUpdate: true
                    )
                }
                if let kind = sessionEvent, !sessionID.isEmpty {
                    self.viewModel.onSessionEvent?(
                        SessionEvent(kind: kind, sessionID: sessionID, source: source)
                    )
                }
            }
            return
        }

        DispatchQueue.main.async {
            if let task = json["task"] as? String {
                self.viewModel.compactTaskName = task
            }
            // Compact/status messages deliberately do not dismiss pending
            // requests. Only a response, disconnect, or session end can.
        }
    }

    private func enqueueInteractiveRequest(_ json: [String: Any],
                                           state: String,
                                           from clientID: UUID) {
        guard let client = clients[clientID] else { return }
        let requestID = ((json["request_id"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines))
            .flatMap { $0.isEmpty ? nil : $0 } ?? UUID().uuidString

        if let owner = requestToClient[requestID], owner != clientID {
            NSLog("LocalServer: duplicate request_id \(requestID); closing newer client")
            cleanupClient(clientID)
            return
        }

        let kind: PendingRequestKind = state == "approval" ? .approval : .ask
        var questions = Self.parseQuestions(json["questions"])
        if kind == .ask, questions.isEmpty {
            let labels = json["options"] as? [String] ?? []
            let choices = labels.enumerated().map {
                AskOptionChoice(id: "legacy-\($0.offset)", label: $0.element,
                                description: nil)
            }
            let prompt = (json["task"] as? String) ?? "Question"
            questions = [AskQuestionDraft(id: "question-1", header: "Question",
                                          question: prompt, options: choices,
                                          multiSelect: false)]
        }
        if kind == .ask, questions.isEmpty || questions.contains(where: { $0.options.isEmpty }) {
            NSLog("LocalServer: Ask request \(requestID) has no selectable options")
            cleanupClient(clientID)
            return
        }

        requestToClient[requestID] = clientID
        let sessionID = (json["session_id"] as? String) ?? ""
        requestToSession[requestID] = sessionID
        client.requestIDs.insert(requestID)

        let target = (json["targetFile"] as? String)
                     ?? (json["target_file"] as? String) ?? ""
        let requestedTimeout = (json["timeout_seconds"] as? NSNumber)?.doubleValue ?? 300
        let timeoutSeconds = min(max(requestedTimeout, 15), 600)
        let arrivedAt = Date()
        let request = PendingRequest(
            id: requestID,
            kind: kind,
            sessionID: sessionID,
            source: (json["source"] as? String) ?? "claude",
            agentName: (json["agent"] as? String) ?? "Claude Code",
            taskName: (json["task"] as? String) ?? (kind == .approval ? "Permission request" : "Question"),
            targetFile: target,
            toolName: (json["tool_name"] as? String) ?? (json["toolName"] as? String),
            command: (json["command"] as? String) ?? (kind == .approval ? target : nil),
            cwd: json["cwd"] as? String,
            reason: json["reason"] as? String,
            diff: json["diff"] as? String,
            questions: questions,
            arrivedAt: arrivedAt,
            expiresAt: arrivedAt.addingTimeInterval(timeoutSeconds)
        )
        DispatchQueue.main.async {
            self.viewModel.enqueueRequest(request)
        }
    }

    private func cancelRequests(sessionID: String) {
        guard !sessionID.isEmpty else { return }
        let clientIDs = Set(requestToSession.compactMap { requestID, mappedSession in
            mappedSession == sessionID ? requestToClient[requestID] : nil
        })
        for clientID in clientIDs {
            cleanupClient(clientID)
        }
    }

    private func cleanupClient(_ clientID: UUID) {
        guard let client = clients.removeValue(forKey: clientID) else { return }
        let requestIDs = client.requestIDs
        for requestID in requestIDs {
            requestToClient.removeValue(forKey: requestID)
            requestToSession.removeValue(forKey: requestID)
        }
        client.connection.stateUpdateHandler = nil
        client.connection.cancel()
        if !requestIDs.isEmpty {
            DispatchQueue.main.async {
                self.viewModel.removeRequests(ids: requestIDs)
            }
        }
    }

    private static func parseQuestions(_ rawQuestions: Any?) -> [AskQuestionDraft] {
        guard let questionMaps = rawQuestions as? [[String: Any]] else { return [] }
        return questionMaps.enumerated().map { index, raw in
            let header = (raw["header"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
                ?? "question_\(index + 1)"
            let question = (raw["question"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
                ?? header
            let rawOptions = raw["options"] as? [[String: Any]] ?? []
            let options = rawOptions.enumerated().map { optionIndex, option in
                let label = (option["label"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
                    ?? "Option \(optionIndex + 1)"
                let id = (option["id"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
                    ?? label
                let description = (option["description"] as? String)?
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                return AskOptionChoice(id: id, label: label, description: description)
            }
            return AskQuestionDraft(id: (raw["id"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? header,
                                    header: header,
                                    question: question,
                                    options: options,
                                    multiSelect: (raw["multiSelect"] as? Bool)
                                        ?? (raw["multi_select"] as? Bool) ?? false)
        }
    }
}

// MARK: - Unix Socket Server (vibe-island.sock bridge protocol)
// Listens on ~/.vibe-island/run/vibe-island.sock — the same path the real
// app uses. Agent CLIs (ZCode/Claude Code via bridge) push NDJSON events
// here, one JSON object per line. We take over this socket so events flow
// into our notch app instead of the real one. Requires the real app to be
// stopped (only one listener per socket file).
//
// We use POSIX sockets + GCD rather than Network.framework because
// NWListener with .unix endpoint silently fails to create the socket file
// on current macOS. POSIX is the reliable path for AF_UNIX SOCK_STREAM.
//
// Captured field reference (from sniffing the bridge → socket traffic):
//   _source: "zcode" | "claude" | "codex" — agent CLI identity
//   sessionId / session_id: unique per conversation (camelCase + snake_case)
//   hook_event_name / hookEventName: PreToolUse|PostToolUse|Stop|UserPromptSubmit|...
//   tool_name: Bash|Edit|Read|...
//   tool_input: {file_path|command|...}
//   tool_response / responseText: result data (PostToolUse/Stop only)
//   cwd, _git_branch, _repo_name, timestamp, _env
class UnixSocketServer {
    let viewModel: NotchViewModel
    let socketPath: String
    private var listenFD: Int32 = -1
    private var source: DispatchSourceRead?

    private static func hasActiveListener(at path: String) -> Bool {
        let probeFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard probeFD >= 0 else { return false }
        defer { close(probeFD) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = path.utf8CString
        let pathCapacity = MemoryLayout.size(ofValue: addr.sun_path)
        guard pathBytes.count <= pathCapacity else {
            return false
        }
        _ = withUnsafeMutablePointer(to: &addr.sun_path) {
            $0.withMemoryRebound(to: CChar.self, capacity: pathCapacity) { destination in
                pathBytes.withUnsafeBufferPointer { source in
                    memcpy(destination, source.baseAddress, source.count)
                }
            }
        }
        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(probeFD, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        return result == 0
    }

    init?(viewModel: NotchViewModel, socketPath: String) {
        self.viewModel = viewModel
        self.socketPath = socketPath

        // Create parent directory.
        let dir = (socketPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir,
                                                 withIntermediateDirectories: true)
        // Never steal an active socket from another Vibe Island instance.
        // Only remove a stale socket node after proving no listener owns it.
        var fileInfo = stat()
        if lstat(socketPath, &fileInfo) == 0 {
            let fileType = fileInfo.st_mode & mode_t(S_IFMT)
            guard fileType == mode_t(S_IFSOCK) else {
                NSLog("UnixSocketServer: refusing to replace non-socket path \(socketPath)")
                return nil
            }
            guard !Self.hasActiveListener(at: socketPath) else {
                NSLog("UnixSocketServer: active listener already owns \(socketPath)")
                return nil
            }
            guard unlink(socketPath) == 0 else {
                NSLog("UnixSocketServer: failed to remove stale socket errno=\(errno)")
                return nil
            }
        }

        // Create AF_UNIX SOCK_STREAM listening socket.
        listenFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listenFD >= 0 else {
            NSLog("UnixSocketServer: socket() failed errno=\(errno)")
            return nil
        }
        // Bind to the path.
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = socketPath.utf8CString
        // 104 is the historical max for sun_path (Linux); Darwin's
        // sockaddr_un.sun_path is 104 chars too.
        let maxPath = 104
        _ = withUnsafeMutablePointer(to: &addr.sun_path) {
            $0.withMemoryRebound(to: CChar.self, capacity: maxPath) { dst in
                pathBytes.withUnsafeBufferPointer { src in
                    memcpy(dst, src.baseAddress, min(src.count, maxPath))
                }
            }
        }
        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(listenFD, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bindResult == 0 else {
            NSLog("UnixSocketServer: bind() failed errno=\(errno) path=\(socketPath)")
            close(listenFD); listenFD = -1
            return nil
        }
        chmod(socketPath, 0o600)

        // Listen for incoming connections.
        guard listen(listenFD, 8) == 0 else {
            NSLog("UnixSocketServer: listen() failed errno=\(errno)")
            close(listenFD); listenFD = -1
            return nil
        }
        // Non-blocking accept via DispatchSource.
        let flags = fcntl(listenFD, F_GETFL, 0)
        _ = fcntl(listenFD, F_SETFL, flags | O_NONBLOCK)
    }

    func start() {
        guard listenFD >= 0 else { return }
        // Schedule accept() readiness on a global queue.
        let src = DispatchSource.makeReadSource(fileDescriptor: listenFD,
                                                queue: .global())
        src.setEventHandler { [weak self] in
            self?.acceptOnce()
        }
        src.resume()
        source = src
        NSLog("UnixSocketServer listening on \(socketPath)")
    }

    private func acceptOnce() {
        while true {
            var addr = sockaddr_un()
            var len = socklen_t(MemoryLayout<sockaddr_un>.size)
            let clientFD = withUnsafeMutablePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                    accept(listenFD, sa, &len)
                }
            }
            if clientFD < 0 {
                // EAGAIN / EWOULDBLOCK = no more pending connections.
                break
            }
            handleClient(clientFD)
        }
    }

    private func handleClient(_ fd: Int32) {
        // Read until EOF or a newline, line-by-line.
        DispatchQueue.global().async { [weak self] in
            var buf = [UInt8](repeating: 0, count: 65536)
            var pending = Data()
            while true {
                let n = read(fd, &buf, buf.count)
                if n <= 0 { break }
                pending.append(contentsOf: buf[0..<n])
                // Process complete lines.
                while let nlIdx = pending.firstIndex(of: 0x0A) {
                    let lineData = pending[0..<nlIdx]
                    pending.removeFirst(nlIdx + 1)
                    if let s = self,
                       let text = String(data: lineData, encoding: .utf8) {
                        s.processLine(text)
                    }
                }
            }
            close(fd)
        }
    }

    // Map bridge NDJSON → ViewModel updates.
    private func processLine(_ line: String) {
        guard let data = line.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            NSLog("UnixSocket RX parse failed")
            return
        }

        // Source + session identity (accept camelCase or snake_case).
        let source = (json["_source"] as? String) ?? "claude"
        let sessionId = (json["session_id"] as? String)
                        ?? (json["sessionId"] as? String) ?? ""
        let event = (json["hook_event_name"] as? String)
                    ?? (json["hookEventName"] as? String) ?? ""
        let cwd = (json["cwd"] as? String) ?? ""
        let shortCwd = (cwd as NSString).lastPathComponent
        NSLog("UnixSocket RX event=\(event) source=\(source) session=\(sessionId.prefix(12))")

        // Tool details.
        let toolName = (json["tool_name"] as? String)
                       ?? (json["toolName"] as? String) ?? ""
        let toolInput = json["tool_input"] as? [String: Any]
                        ?? json["toolInput"] as? [String: Any] ?? [:]
        let prompt = (json["prompt"] as? String) ?? ""
        let responseText = (json["responseText"] as? String)
                           ?? (json["last_assistant_message"] as? String) ?? ""

        DispatchQueue.main.async {
            switch event {
            case "SessionStart":
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: "Session start", detail: shortCwd,
                                             cwd: cwd, isRunning: false,
                                             isLiveUpdate: true)
                self.viewModel.compactTaskName = "\(source): start"
            case "UserPromptSubmit":
                let snippet = String(prompt.prefix(50))
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: snippet.isEmpty ? "Thinking…" : snippet,
                                             detail: shortCwd, cwd: cwd, isRunning: true,
                                             isLiveUpdate: true)
                self.viewModel.compactTaskName = snippet.isEmpty ? "Thinking…" : snippet
            case "PreToolUse":
                let (task, detail) = Self.describe(tool: toolName, input: toolInput)
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: task, detail: detail, cwd: cwd,
                                             isRunning: true, isLiveUpdate: true)
                self.viewModel.compactTaskName = task
            case "PostToolUse":
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: "Done: \(toolName)", detail: shortCwd,
                                             cwd: cwd, isRunning: true,
                                             isLiveUpdate: true)
                self.viewModel.compactTaskName = "Done: \(toolName)"
            case "PostToolUseFailure":
                let error = (json["error"] as? String)
                    ?? (json["responseText"] as? String)
                    ?? "Tool failure"
                let preview = String(error.prefix(60))
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: "Failed: \(toolName)", detail: preview,
                                             cwd: cwd, isRunning: true,
                                             isLiveUpdate: true)
                self.viewModel.compactTaskName = "Failed: \(toolName)"
            case "StopFailure":
                let error = (json["error"] as? String)
                    ?? (json["responseText"] as? String)
                    ?? "Session failure"
                let preview = String(error.prefix(60))
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: "Failed: \(preview)", detail: shortCwd,
                                             cwd: cwd, isRunning: false,
                                             isLiveUpdate: true)
                self.viewModel.compactTaskName = "Failed: \(preview)"
            case "Stop":
                let preview = String(responseText.prefix(40))
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: preview.isEmpty ? "Idle" : preview,
                                             detail: shortCwd, cwd: cwd, isRunning: false,
                                             isLiveUpdate: true)
                self.viewModel.compactTaskName = "Idle"
            case "Notification":
                let notificationType = (json["notification_type"] as? String)
                    ?? (json["notificationType"] as? String)
                    ?? "notification"
                let message = (json["message"] as? String)
                    ?? (json["text"] as? String)
                    ?? responseText
                let preview = String((message.isEmpty ? notificationType : message).prefix(70))
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: preview, detail: shortCwd,
                                             cwd: cwd, isRunning: false,
                                             isLiveUpdate: true)
                self.viewModel.compactTaskName = preview
            case "SessionEnd":
                self.viewModel.removeSession(id: sessionId)
            default:
                break
            }
        }
    }

    // Build a short human description for a tool call.
    private static func describe(tool: String, input: [String: Any]) -> (task: String, detail: String?) {
        switch tool {
        case "Bash":
            let cmd = (input["command"] as? String) ?? ""
            return ("Run command", String(cmd.prefix(60)))
        case "Edit", "Write":
            let path = (input["file_path"] as? String) ?? ""
            let kind = tool == "Edit" ? "Edit file" : "Write file"
            return (kind, path)
        case "Read":
            return ("Read", (input["file_path"] as? String))
        case "Grep", "Glob":
            return ("Search", (input["pattern"] as? String))
        default:
            return (tool, nil)
        }
    }
}

// MARK: - Usage Snapshot
// One provider's usage snapshot. Mirrors the JSON pushed by usage-daemon.py:
//   {type:"usage", provider:"Z.ai", usage:{five_hour:6, five_hour_reset:"53m",
//                         seven_day:5, seven_day_reset:"6d10h",
//                         monthly:19, monthly_reset:"16d10h",
//                         level:"max", plan:"GLM Coding Max"}}
struct UsageSnapshot: Identifiable, Equatable {
    let id: String          // provider name (unique key)
    let provider: String    // "Z.ai" / "Codex" / "Claude" — shown in the UI
    let fiveHour: Int?
    let fiveHourReset: String?
    let sevenDay: Int?
    let sevenDayReset: String?
    let monthly: Int?
    let monthlyReset: String?
    let level: String?
    let plan: String?
    // Remaining plan credits ("1,240") for wallet-style providers like the
    // Google One AI credits on a Gemini paid plan. nil when not applicable.
    let credits: String?

    // Color by usage level: green < 50%, yellow < 80%, red otherwise.
    func color(for window: Window) -> Color {
        let pct = value(for: window) ?? 0
        if pct >= 80 { return Color(red: 248/255, green: 113/255, blue: 113/255) }
        if pct >= 50 { return Color(red: 250/255, green: 204/255, blue: 21/255) }
        return Color(red: 74/255, green: 222/255, blue: 128/255)
    }
    func value(for window: Window) -> Int? {
        switch window {
        case .fiveHour: return fiveHour
        case .sevenDay: return sevenDay
        case .monthly:  return monthly
        }
    }
    func reset(for window: Window) -> String? {
        switch window {
        case .fiveHour: return fiveHourReset
        case .sevenDay: return sevenDayReset
        case .monthly:  return monthlyReset
        }
    }

    enum Window: String, CaseIterable {
        case fiveHour = "5h"
        case sevenDay = "7d"
        case monthly  = "mo"
    }

    static func == (lhs: UsageSnapshot, rhs: UsageSnapshot) -> Bool { lhs.id == rhs.id }
}

// MARK: - UI Constants & Colors
struct Theme {
    // The shell stays almost black so it visually joins the physical notch;
    // the inner surfaces use a cool instrument-panel hierarchy.
    static let bg = Color(red: 5/255, green: 7/255, blue: 10/255)
    static let cardBg = Color(red: 17/255, green: 22/255, blue: 29/255)
    static let cardHover = Color(red: 24/255, green: 31/255, blue: 40/255)
    static let codeBg = Color(red: 12/255, green: 16/255, blue: 22/255)
    static let buttonGray = Color(red: 31/255, green: 39/255, blue: 50/255)
    static let textMain = Color(red: 241/255, green: 245/255, blue: 249/255)
    static let textMuted = Color(red: 140/255, green: 151/255, blue: 165/255)
    static let green = Color(red: 102/255, green: 227/255, blue: 196/255)
    static let blue = Color(red: 122/255, green: 167/255, blue: 255/255)
    static let yellow = Color(red: 247/255, green: 198/255, blue: 107/255)
    static let cyan = Color(red: 95/255, green: 214/255, blue: 208/255)
    static let red = Color(red: 255/255, green: 122/255, blue: 144/255)
    static let border = Color.white.opacity(0.08)
    static let divider = Color.white.opacity(0.07)
}

extension ApprovalRisk {
    var color: Color {
        switch self {
        case .low: return Theme.green
        case .medium: return Theme.yellow
        case .high, .critical: return Theme.red
        }
    }

    var symbolName: String {
        switch self {
        case .low: return "checkmark.shield.fill"
        case .medium: return "exclamationmark.shield.fill"
        case .high: return "exclamationmark.triangle.fill"
        case .critical: return "xmark.shield.fill"
        }
    }
}

struct PixelPet: View {
    var color: Color
    var body: some View {
        VStack(spacing: 1) {
            HStack(spacing: 1) {
                Color.clear.frame(width: 2, height: 2)
                color.frame(width: 2, height: 2)
                Color.clear.frame(width: 4, height: 2)
                color.frame(width: 2, height: 2)
                Color.clear.frame(width: 2, height: 2)
            }
            HStack(spacing: 1) { Color(white: 0.15).frame(width: 12, height: 2) }
            HStack(spacing: 1) {
                Color.clear.frame(width: 2, height: 2)
                Color(white: 0.15).frame(width: 4, height: 2)
                Color.clear.frame(width: 4, height: 2)
            }
        }
        .frame(width: 14, height: 8)
    }
}

// Animated equalizer-style pulse bars shown in compact mode while an
// agent is active. Uses TimelineView so the animation runs smoothly
// without manual timers; each bar's height follows a sine wave with a
// phase offset so they ripple.
struct PulseBars: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var color: Color = Color(red: 74/255, green: 222/255, blue: 128/255)
    var barCount: Int = 4
    var maxHeight: CGFloat = 14

    var body: some View {
        Group {
            if reduceMotion {
                HStack(alignment: .center, spacing: 2) {
                    ForEach(0..<barCount, id: \.self) { index in
                        Capsule()
                            .fill(color)
                            .frame(width: 2.5,
                                   height: maxHeight * (index.isMultiple(of: 2) ? 0.55 : 0.85))
                    }
                }
            } else {
                TimelineView(.animation) { context in
                    let t = context.date.timeIntervalSinceReferenceDate
                    HStack(spacing: 2) {
                        ForEach(0..<barCount, id: \.self) { index in
                            let phase = Double(index) * 0.6
                            let wave = (sin(t * 4 + phase) + 1) / 2
                            let height = maxHeight * (0.25 + 0.75 * wave)
                            Capsule()
                                .fill(color)
                                .frame(width: 2.5, height: max(2, height))
                        }
                    }
                }
            }
        }
        .frame(height: maxHeight)
    }
}

private enum NotchLayout {
    static let compactWidth: CGFloat = 224
    static let compactHeight: CGFloat = 32
    static let overviewWidth: CGFloat = 560
    static let approvalWidth: CGFloat = 468
    static let askWidth: CGFloat = 468

    static func overviewHeight(sessionCount: Int) -> CGFloat {
        guard sessionCount > 0 else { return 224 }
        return min(516, 124 + CGFloat(min(sessionCount, 6)) * 62)
    }

    static func askHeight(optionCount: Int) -> CGFloat {
        min(526, 256 + CGFloat(min(max(optionCount, 1), 4)) * 64)
    }

    static func width(for state: NotchViewModel.NotchState) -> CGFloat {
        switch state {
        case .compact: return compactWidth
        case .overview: return overviewWidth
        case .approval: return approvalWidth
        case .ask: return askWidth
        }
    }

    static func height(for state: NotchViewModel.NotchState,
                       sessionCount: Int,
                       askOptionCount: Int) -> CGFloat {
        switch state {
        case .compact: return compactHeight
        case .overview: return overviewHeight(sessionCount: sessionCount)
        case .approval: return 548
        case .ask: return askHeight(optionCount: askOptionCount)
        }
    }
}

// MARK: - Notch View
struct NotchView: View {
    @ObservedObject var viewModel: NotchViewModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack(alignment: .top) {
            // Top corners stay square (flush with screen top edge, like a
            // menu-bar dropdown pulled down); only bottom corners round.
            // This matches the real app's "pulled-down panel" silhouette.
            UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: cornerRadius,
                                   bottomTrailingRadius: cornerRadius, topTrailingRadius: 0,
                                   style: .continuous)
                .fill(
                    LinearGradient(colors: [.black, Theme.bg],
                                   startPoint: .top, endPoint: .bottom)
                )
                .frame(width: notchWidth, height: notchHeight)
                // Subtle inner border instead of a drop shadow: .shadow on
                // UnevenRoundedRectangle leaks the bounding-box corners
                // (visible尖角 outside the rounded bottom), so we avoid it.
                .overlay(
                    UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: cornerRadius,
                                           bottomTrailingRadius: cornerRadius, topTrailingRadius: 0,
                                           style: .continuous)
                        .stroke(Theme.border, lineWidth: 0.5)
                )
                // A soft outer glow only below the notch, clipped to the
                // rounded shape so it can't leak尖角.
                .overlay(
                    UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: cornerRadius,
                                           bottomTrailingRadius: cornerRadius, topTrailingRadius: 0,
                                           style: .continuous)
                        .stroke(Color.black.opacity(0.5), lineWidth: 3)
                        .blur(radius: 6)
                        .offset(y: 3)
                        .mask(
                            VStack(spacing: 0) {
                                Color.clear
                                Rectangle().frame(height: cornerRadius + 6)
                            }
                        )
                )
                .contentShape(UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: cornerRadius,
                                                     bottomTrailingRadius: cornerRadius, topTrailingRadius: 0,
                                                     style: .continuous))

            Group {
                switch viewModel.activeState {
                case .compact: CompactView(vm: viewModel).frame(width: notchWidth, height: notchHeight)
                case .overview: OverviewView(vm: viewModel).frame(width: notchWidth, height: notchHeight)
                case .approval:
                    // Multiple pending approvals → triage list (several visible
                    // at once, scroll for more). Single → full detail card.
                    if viewModel.pendingRequests.count > 1 {
                        PendingQueueView(vm: viewModel).frame(width: notchWidth, height: notchHeight)
                    } else {
                        ApprovalView(viewModel: viewModel).frame(width: notchWidth, height: notchHeight)
                    }
                case .ask: AskView(viewModel: viewModel).frame(width: notchWidth, height: notchHeight)
                }
            }
            .transition(.opacity)
        }
        // Hover gives a temporary preview; the explicit pin control in the
        // overview keeps it open. There is no competing outer tap gesture.
        // Clicks pass through to child views (AgentRow jump, buttons).
        // Snappy spring with a touch of overshoot — matches the real
        // app's "expand decisively, settle gently" feel.
        .animation(
            reduceMotion ? nil : .spring(response: 0.42, dampingFraction: 0.78, blendDuration: 0.1),
            value: viewModel.activeState
        )
        // Native AppKit hover tracking: reliable across dynamic size changes,
        // where SwiftUI .onHover drops events during layout rebuilds.
        .background(HoverTrackingArea(onEnter: { viewModel.setHovered(true) },
                                      onExit:  { viewModel.setHovered(false) }))
    }

    var notchWidth: CGFloat {
        NotchLayout.width(for: viewModel.activeState)
    }

    var notchHeight: CGFloat {
        NotchLayout.height(
            for: viewModel.activeState,
            sessionCount: viewModel.recentSessions.count,
            askOptionCount: viewModel.currentAskQuestion?.options.count ?? 0
        )
    }
    var cornerRadius: CGFloat { viewModel.activeState == .compact ? 18 : 28 }
}

// MARK: - Native Hover Tracking (reliable across dynamic size changes)
// SwiftUI .onHover drops events when the tracked view resizes (compact ->
// overview), leaving the notch stuck open. NSTrackingArea tracks at the
// window level so it survives SwiftUI layout rebuilds.
struct HoverTrackingArea: NSViewRepresentable {
    let onEnter: () -> Void
    let onExit:  () -> Void

    func makeNSView(context: Context) -> HoverView {
        let v = HoverView()
        v.onEnter = onEnter
        v.onExit  = onExit
        return v
    }
    func updateNSView(_ nsView: HoverView, context: Context) {
        nsView.onEnter = onEnter
        nsView.onExit  = onExit
    }

    final class HoverView: NSView {
        var onEnter: (() -> Void)?
        var onExit:  (() -> Void)?

        override func updateTrackingAreas() {
            super.updateTrackingAreas()
            // Remove any tracking area we previously installed so we don't
            // accumulate duplicates as the view resizes.
            trackingAreas.forEach { removeTrackingArea($0) }
            let area = NSTrackingArea(
                rect: bounds,
                options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                owner: self,
                userInfo: nil
            )
            addTrackingArea(area)
        }
        override func mouseEntered(with event: NSEvent) { onEnter?() }
        override func mouseExited(with event: NSEvent)  { onExit?() }
    }
}

struct CompactView: View {
    @ObservedObject var vm: NotchViewModel

    var body: some View {
        Button(action: { vm.togglePin() }) {
            HStack(spacing: 8) {
                if let active = vm.runningSession {
                    PulseBars(color: active.badgeColor, maxHeight: 12)
                    Text(active.providerName)
                        .font(NotchFont.mono(9))
                        .foregroundColor(active.badgeColor)
                    Rectangle()
                        .fill(Theme.divider)
                        .frame(width: 1, height: 11)
                } else {
                    PixelPet(color: vm.transientNotice == nil ? Theme.green : Theme.red)
                        .offset(y: 1)
                }
                Text(vm.compactLabel)
                    .font(NotchFont.mono(11))
                    .foregroundColor(vm.transientNotice == nil ? Theme.textMain : Theme.red)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 0)
                if vm.compactOverflowCount > 0 {
                    Text("+\(vm.compactOverflowCount)")
                        .font(NotchFont.mono(9))
                        .foregroundColor(Theme.textMain)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Theme.buttonGray)
                        .clipShape(Capsule())
                }
            }
            .padding(.horizontal, 13)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .overlay(alignment: .bottom) {
            Capsule()
                .fill(vm.transientNotice != nil
                      ? Theme.red
                      : (vm.runningSession?.badgeColor ?? Theme.green.opacity(0.7)))
                .frame(width: vm.runningSession == nil ? 20 : 44, height: 1)
                .padding(.bottom, 1)
        }
        .help("单击固定会话中心")
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            vm.activeSessionCount > 0
                ? "\(vm.activeSessionCount) 个 Agent 正在工作，\(vm.compactLabel)"
                : "没有 Agent 正在工作"
        )
        .accessibilityHint("单击固定会话中心")
    }
}

struct OverviewView: View {
    @ObservedObject var vm: NotchViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 14) {
                VStack(alignment: .leading, spacing: 7) {
                    HStack(spacing: 7) {
                        Text("VIBE CENTER")
                            .font(NotchFont.mono(9))
                            .foregroundColor(Theme.textMain)
                            .tracking(0.8)
                        Circle()
                            .fill(vm.ipcHealth.color)
                            .frame(width: 6, height: 6)
                        Text(vm.overviewSessionCountLabel)
                            .font(NotchFont.mono(10))
                            .foregroundColor(Theme.textMuted)
                    }
                    UsageRotator(vm: vm)
                }
                .help(vm.statusSummary)
                Spacer(minLength: 8)
                HStack(spacing: 6) {
                    IslandIconButton(
                        icon: vm.notificationSoundsEnabled ? "speaker.wave.2.fill" : "speaker.slash.fill",
                        label: vm.notificationSoundsEnabled ? "静音提醒" : "开启提醒声音"
                    ) {
                        vm.notificationSoundsEnabled.toggle()
                    }
                    IslandIconButton(icon: "arrow.clockwise", label: "刷新会话",
                                     isBusy: vm.isScanningSessions) {
                        vm.refreshSessions()
                    }
                    IslandIconButton(icon: vm.isPinned ? "pin.fill" : "pin",
                                     label: vm.isPinned ? "取消固定" : "固定面板",
                                     isActive: vm.isPinned) {
                        vm.togglePin()
                    }
                    IslandIconButton(icon: "gearshape", label: "打开设置") {
                        vm.openSettings()
                    }
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 14)
            .padding(.bottom, 12)

            Divider().overlay(Theme.divider)

            if vm.recentSessions.isEmpty && vm.sessionHistory.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if !vm.runningSessions.isEmpty {
                            sessionSection(title: "LIVE", sessions: vm.runningSessions,
                                           color: Theme.green) { AgentRow(vm: vm, session: $0) }
                        }
                        if !vm.idleSessions.isEmpty {
                            sessionSection(title: "RECENT", sessions: vm.idleSessions,
                                           color: Theme.textMuted) { AgentRow(vm: vm, session: $0) }
                        }
                        if !vm.sessionHistory.isEmpty {
                            sessionSection(title: "完成", sessions: vm.sessionHistory,
                                           color: Theme.textMuted) { HistoryRow(vm: vm, session: $0) }
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 12)
                }
                .scrollIndicators(.visible)
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            if vm.isScanningSessions {
                ProgressView()
                    .controlSize(.small)
                Text("正在发现 Agent 会话…")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(Theme.textMain)
            } else {
                Image(systemName: "waveform.path.ecg.rectangle")
                    .font(.system(size: 20, weight: .light))
                    .foregroundColor(Theme.green)
                Text("等待第一个信号")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(Theme.textMain)
                Text("启动 Claude Code、Codex 或 ZCode；这里会自动出现。")
                    .font(NotchFont.mono(10))
                    .foregroundColor(Theme.textMuted)
                HStack(spacing: 8) {
                    Button("重新扫描") { vm.refreshSessions() }
                        .buttonStyle(NotchBtnStyle(bg: Theme.buttonGray, fg: Theme.textMain))
                    Button("连接设置") { vm.openSettings() }
                        .buttonStyle(NotchBtnStyle(bg: Theme.textMain, fg: Theme.bg))
                }
                .frame(width: 236)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.bottom, 10)
    }

    private func sessionSection<Row: View>(title: String, sessions: [AgentSession],
                                            color: Color,
                                            @ViewBuilder rowContent: @escaping (AgentSession) -> Row) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 7) {
                Capsule().fill(color).frame(width: 18, height: 2)
                Text(title)
                    .font(NotchFont.mono(9))
                    .foregroundColor(Theme.textMuted)
                    .tracking(0.7)
                Text("\(sessions.count)")
                    .font(NotchFont.mono(9))
                    .foregroundColor(color)
            }
            .padding(.horizontal, 6)

            ForEach(sessions) { session in
                rowContent(session)
            }
        }
    }
}

struct IslandIconButton: View {
    let icon: String
    let label: String
    var isActive = false
    var isBusy = false
    let action: () -> Void
    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(isActive ? Theme.green : Theme.textMuted)
                .frame(width: 30, height: 30)
                .background(isActive ? Theme.green.opacity(0.12) :
                            (isHovered ? Theme.cardHover : Color.white.opacity(0.035)))
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                .rotationEffect(isBusy ? .degrees(360) : .zero)
                .animation(isBusy ? .linear(duration: 0.8).repeatForever(autoreverses: false) : .default,
                           value: isBusy)
        }
        .buttonStyle(.plain)
        .disabled(isBusy)
        .onHover { isHovered = $0 }
        .help(label)
        .accessibilityLabel(label)
    }
}

// A usage strip that auto-rotates through every provider (Codex, 智谱 GLM,
// …) every few seconds so all quotas are visible without clicking.
struct UsageRotator: View {
    @ObservedObject var vm: NotchViewModel
    private let rotationInterval: TimeInterval = 5.0

    var body: some View {
        let providers = vm.providers
        Group {
            if providers.isEmpty {
                Button(action: { vm.restartUsage() }) {
                    HStack(spacing: 6) {
                        Image(systemName: vm.usageHealth.symbol)
                            .font(.system(size: 10, weight: .medium))
                            .foregroundColor(vm.usageHealth.color)
                        Text(usageHealthLabel)
                            .font(NotchFont.mono(9))
                            .foregroundColor(Theme.textMuted)
                    }
                }
                .buttonStyle(.plain)
                .help("\(vm.usageHealth.detail) · 点击重试")
                .accessibilityLabel("用量状态：\(usageHealthLabel)，点击重试")
            } else {
                HStack(spacing: 8) {
                    Button(action: { advance(count: providers.count) }) {
                        HStack(spacing: 5) {
                            if let img = ProviderLogo.image(for: vm.currentUsage?.provider ?? "",
                                                            size: 15) {
                                img.resizable().scaledToFit().frame(width: 15, height: 15)
                            } else {
                                Text(String(vm.currentUsage?.provider.prefix(1) ?? "—"))
                                    .font(NotchFont.mono(10))
                                    .foregroundColor(Theme.textMain)
                                    .frame(width: 15, height: 15)
                            }
                            Text(vm.currentUsage?.provider.uppercased() ?? "USAGE")
                                .font(NotchFont.mono(9))
                                .foregroundColor(Theme.textMuted)
                            if providers.count > 1 {
                                HStack(spacing: 2) {
                                    ForEach(0..<providers.count, id: \.self) { i in
                                        Circle()
                                            .fill(i == vm.currentProviderIndex
                                                  ? Theme.textMuted
                                                  : Theme.textMuted.opacity(0.3))
                                            .frame(width: 3, height: 3)
                                    }
                                }
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(providers.count < 2)
                    .accessibilityLabel(providers.count > 1
                        ? "当前用量提供方 \(vm.currentUsage?.provider ?? "")，切换下一个"
                        : "用量提供方 \(vm.currentUsage?.provider ?? "")")

                    if let usage = vm.currentUsage {
                        HStack(spacing: 8) {
                            if let credits = usage.credits {
                                // Wallet-style provider (Google One AI credits):
                                // show the remaining balance instead of % bars.
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(usage.plan?.uppercased() ?? "PLAN")
                                        .font(NotchFont.mono(8))
                                        .foregroundColor(Theme.textMuted)
                                    Text(credits)
                                        .font(NotchFont.mono(12))
                                        .foregroundColor(Theme.green)
                                    Text("积分剩余")
                                        .font(NotchFont.mono(8))
                                        .foregroundColor(Theme.textMuted)
                                }
                                .frame(width: 74, alignment: .leading)
                            } else {
                                windowBar(label: "5h",
                                          pct: usage.fiveHour,
                                          reset: usage.fiveHourReset,
                                          color: usage.color(for: .fiveHour))
                                Divider().frame(height: 26).overlay(Theme.divider)
                                windowBar(label: "7d",
                                          pct: usage.sevenDay,
                                          reset: usage.sevenDayReset,
                                          color: usage.color(for: .sevenDay))
                                if let mo = usage.monthly {
                                    Divider().frame(height: 26).overlay(Theme.divider)
                                    windowBar(label: "mo",
                                              pct: mo,
                                              reset: usage.monthlyReset,
                                              color: usage.color(for: .monthly))
                                }
                            }
                        }
                    }
                }
                .id(vm.currentProviderIndex)
                .animation(.easeInOut(duration: 0.25), value: vm.currentProviderIndex)
                .onReceive(Timer.publish(every: rotationInterval, on: .main, in: .common).autoconnect()) { _ in
                    if vm.providers.count > 1 {
                        advance(count: vm.providers.count)
                    }
                }
            }
        }
    }

    private var usageHealthLabel: String {
        switch vm.usageHealth.kind {
        case .checking: return "正在读取用量"
        case .ready: return "等待用量快照"
        case .warning: return "Z.ai 尚未配置"
        case .failed: return "用量服务异常"
        case .disabled: return "用量监测已关闭"
        }
    }

    private func advance(count: Int) {
        guard count > 0 else { return }
        vm.currentProviderIndex = (vm.currentProviderIndex + 1) % count
    }

    // One quota window as a mini progress bar: label + colored % on top, a
    // thin track filled to pct, and the reset time beneath. Scannable at a
    // glance without reading digits.
    private func windowBar(label: String, pct: Int?, reset: String?, color: Color) -> some View {
        let barWidth: CGFloat = 46
        let clamped = min(max(pct ?? 0, 0), 100)
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 3) {
                Text(label)
                    .font(NotchFont.mono(8))
                    .foregroundColor(Theme.textMuted)
                if let pct {
                    Text("\(pct)%")
                        .font(NotchFont.mono(9))
                        .foregroundColor(color)
                }
                Spacer(minLength: 0)
            }
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.08)).frame(width: barWidth, height: 3)
                Capsule().fill(color)
                    .frame(width: max(2, barWidth * CGFloat(clamped) / 100.0), height: 3)
            }
            Text((reset ?? "").isEmpty ? " " : reset!)
                .font(NotchFont.mono(8))
                .foregroundColor(Theme.textMuted)
        }
        .frame(width: barWidth)
    }
}

// One agent session row. The source badge on the right is colored per
// agent (claude=orange, zcode=blue, codex=cyan, ...).
struct AgentRow: View {
    @ObservedObject var vm: NotchViewModel
    let session: AgentSession
    @State private var isHovered = false

    var body: some View {
        Button(action: { vm.returnToSession(session) }) {
            HStack(alignment: .center, spacing: 11) {
                ZStack(alignment: .top) {
                    Capsule()
                        .fill(session.badgeColor.opacity(0.18))
                    Capsule()
                        .fill(session.badgeColor)
                        .frame(height: session.isRunning ? 28 : 8)
                }
                .frame(width: 3, height: 38)

                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 7) {
                        Text(session.task.isEmpty ? "等待新任务" : session.task)
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(Theme.textMain)
                            .lineLimit(1)
                        Spacer(minLength: 6)
                        Text(session.relativeTime)
                            .font(NotchFont.mono(9))
                            .foregroundColor(Theme.textMuted)
                    }

                    HStack(spacing: 6) {
                        badge(session.providerName, color: session.badgeColor)
                        if let workspace = session.workspaceName, !workspace.isEmpty {
                            Text(workspace)
                                .font(NotchFont.mono(9))
                                .foregroundColor(Theme.textMuted)
                                .lineLimit(1)
                        }
                        if let terminal = session.terminal, !terminal.isEmpty {
                            Text("· \(terminal.uppercased())")
                                .font(NotchFont.mono(8))
                                .foregroundColor(Theme.textMuted.opacity(0.8))
                        }
                        Spacer(minLength: 4)
                        if session.isRunning {
                            PulseBars(color: session.badgeColor, barCount: 3, maxHeight: 10)
                        }
                    }

                    if let preview = session.preview, !preview.isEmpty {
                        Text(preview)
                            .font(NotchFont.mono(9))
                            .foregroundColor(Theme.textMuted)
                            .lineLimit(1)
                    }
                }
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, minHeight: 54, alignment: .leading)
            .background(isHovered ? Theme.cardHover : Theme.cardBg)
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(session.isRunning ? session.badgeColor.opacity(0.16) : Theme.border,
                            lineWidth: 0.5)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
        .help("返回 \(session.providerName) 工作现场")
        .accessibilityLabel(
            "\(session.providerName)，\(session.isRunning ? "运行中" : "最近活动")，\(session.task)"
        )
        .accessibilityHint("打开对应应用")
        .contextMenu {
            Button("复制会话 ID") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(session.id, forType: .string)
            }
            if let cwd = session.cwd, !cwd.isEmpty {
                Button("在 Finder 中显示项目") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: cwd)])
                }
                Button("复制项目路径") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(cwd, forType: .string)
                }
            }
            if let transcript = session.transcriptPath, !transcript.isEmpty {
                Button("在 Finder 中显示会话记录") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: transcript)])
                }
            }
        }
    }

    private func badge(_ text: String, color: Color) -> some View {
        Text(text)
            .font(NotchFont.mono(8, .bold))
            .foregroundColor(color)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(color.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
    }
}

// A finished session can still return to its provider or workspace even when
// the original child process has exited.
struct HistoryRow: View {
    @ObservedObject var vm: NotchViewModel
    let session: AgentSession

    var body: some View {
        Button(action: { vm.returnToSession(session) }) {
            HStack(alignment: .center, spacing: 11) {
                Capsule()
                    .fill(Theme.textMuted.opacity(0.35))
                    .frame(width: 3, height: 38)
                VStack(alignment: .leading, spacing: 5) {
                    Text(session.task.isEmpty ? "会话结束" : session.task)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(Theme.textMuted)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    HStack(spacing: 6) {
                        Text(session.providerName)
                            .font(NotchFont.mono(8, .bold))
                            .foregroundColor(Theme.textMuted)
                        if let workspace = session.workspaceName, !workspace.isEmpty {
                            Text(workspace)
                                .font(NotchFont.mono(9))
                                .foregroundColor(Theme.textMuted.opacity(0.8))
                                .lineLimit(1)
                        }
                        Spacer(minLength: 4)
                        Text("完成 \(session.endedRelativeTime)")
                            .font(NotchFont.mono(9))
                            .foregroundColor(Theme.textMuted)
                    }
                }
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, minHeight: 54, alignment: .leading)
            .background(Theme.cardBg.opacity(0.5))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(Theme.border, lineWidth: 0.5)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(session.providerName) 已完成，\(session.task)")
        .accessibilityHint("返回应用或打开项目目录")
    }
}

private enum DiffRole { case header, removed, added, context }

private func diffRoleColor(_ role: DiffRole) -> Color {
    switch role {
    case .header: return Theme.textMuted
    case .removed: return Theme.red
    case .added: return Theme.green
    case .context: return Theme.textMain
    }
}

// Parses the relay's Edit diff format ("--- before\n{old}\n+++ after\n{new}")
// into per-line roles so old_string reads as removed (red) and new_string as
// added (green), like a real unified diff.
private func parseDiffLines(_ raw: String) -> [(String, DiffRole)] {
    var rows: [(String, DiffRole)] = []
    var seenPlusMarker = false
    for chunk in raw.split(separator: "\n", omittingEmptySubsequences: false) {
        let line = String(chunk)
        if line.hasPrefix("---") {
            rows.append((line, .header))
        } else if line.hasPrefix("+++") {
            seenPlusMarker = true
            rows.append((line, .header))
        } else {
            rows.append((line, seenPlusMarker ? .added : .removed))
        }
    }
    return rows
}

// Header for the approval context box: a +added -removed summary when we have
// a real diff, else COMMAND / TARGET.
private func diffSummaryLabel(isDiff: Bool, diff: String?, request: PendingRequest?) -> String {
    if isDiff, let diff {
        let rows = parseDiffLines(diff)
        let added = rows.filter { $0.1 == .added && !$0.0.isEmpty }.count
        let removed = rows.filter { $0.1 == .removed && !$0.0.isEmpty }.count
        return "DIFF  +\(added) -\(removed)"
    }
    return (request?.command?.isEmpty == false) ? "COMMAND" : "TARGET"
}

struct DiffTextView: View {
    let text: String

    var body: some View {
        let rows = parseDiffLines(text)
        LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                Text(row.0.isEmpty ? " " : row.0)
                    .font(NotchFont.mono(11))
                    .foregroundColor(diffRoleColor(row.1))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .frame(height: 15)
            }
        }
        .textSelection(.enabled)
    }
}

// Queue pager for the approval/ask headers: when more than one request is
// stacked, cycle through them. Each focused request is pulled to the front.
struct QueueNav: View {
    @ObservedObject var vm: NotchViewModel
    var color: Color = Theme.textMuted

    var body: some View {
        if vm.pendingRequests.count > 1 {
            HStack(spacing: 3) {
                Button(action: { vm.focusPreviousRequest() }) {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(color)
                        .frame(width: 18, height: 18)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("上一个请求")
                Text("\(vm.pendingRequests.count)")
                    .font(NotchFont.mono(9))
                    .foregroundColor(color)
                Button(action: { vm.focusNextRequest() }) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(color)
                        .frame(width: 18, height: 18)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("下一个请求")
            }
        }
    }
}

struct RequestCountdown: View {
    let request: PendingRequest
    var color: Color = Theme.textMuted

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            HStack(spacing: 4) {
                Image(systemName: "timer")
                    .font(.system(size: 9, weight: .medium))
                Text(request.remainingLabel)
                    .font(NotchFont.mono(9))
            }
            .foregroundColor(color)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("剩余 \(request.remainingLabel) 后自动拒绝")
        }
    }
}

// Triage list shown when several approvals are queued at once: each pending
// request is its own compact row with inline 拒/允, several visible at a time
// and scroll for the rest. Ask rows focus into the detail card. Replaces the
// one-at-a-time pager so the user can see the backlog at a glance.
struct PendingQueueView: View {
    @ObservedObject var vm: NotchViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Circle().fill(Theme.yellow).frame(width: 8, height: 8)
                Text("请求队列")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(Theme.textMain)
                Spacer()
                Text("\(vm.pendingRequests.count) 待处理")
                    .font(NotchFont.mono(9))
                    .foregroundColor(Theme.yellow)
                if !vm.batchApprovableRequests.isEmpty {
                    Button("批量允许 \(vm.batchApprovableRequests.count)") { vm.allowAllPending() }
                        .buttonStyle(MiniBtnStyle(bg: Theme.green.opacity(0.16), fg: Theme.green))
                        .accessibilityLabel("批量允许低风险和中风险审批")
                }
                if vm.blockedBatchApprovalCount > 0 {
                    Text("\(vm.blockedBatchApprovalCount) 项需单独确认")
                        .font(NotchFont.mono(8))
                        .foregroundColor(Theme.red)
                        .help("高风险和严重风险请求不会包含在批量允许中")
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 18)
            .padding(.bottom, 12)

            Divider().overlay(Theme.divider)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(vm.pendingRequests) { request in
                        PendingRow(vm: vm, request: request)
                    }
                }
                .padding(14)
            }
        }
    }
}

struct PendingRow: View {
    @ObservedObject var vm: NotchViewModel
    let request: PendingRequest

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 7) {
                Text(badgeText)
                    .font(NotchFont.mono(8, .bold))
                    .foregroundColor(badgeColor)
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .background(badgeColor.opacity(0.12))
                    .cornerRadius(4)
                if request.kind == .approval {
                    RiskBadge(assessment: request.riskAssessment, compact: true)
                }
                Text(title)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(Theme.textMain)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 4)
                RequestCountdown(request: request, color: badgeColor)
            }
            if let subtitle {
                Text(subtitle)
                    .font(NotchFont.mono(9))
                    .foregroundColor(Theme.textMuted)
                    .lineLimit(2)
            }
            HStack(spacing: 8) {
                Spacer()
                if request.kind == .approval {
                    Button("拒绝") { vm.respondToRequest(id: request.id, action: "deny") }
                        .buttonStyle(MiniBtnStyle(bg: Theme.buttonGray, fg: Theme.textMain))
                    Button("允许") { vm.respondToRequest(id: request.id, action: "allow") }
                        .buttonStyle(MiniBtnStyle(bg: Theme.textMain, fg: Theme.bg))
                } else {
                    Button("查看") { vm.focusRequest(id: request.id) }
                        .buttonStyle(MiniBtnStyle(bg: Theme.textMain, fg: Theme.bg))
                }
            }
        }
        .padding(10)
        .background(Theme.cardBg)
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous)
                    .stroke(Theme.border, lineWidth: 0.5))
        .clipShape(RoundedRectangle(cornerRadius: 11, style: .continuous))
        .accessibilityLabel("\(badgeText)，\(title)")
    }

    private var badgeText: String {
        request.kind == .ask ? "提问" : (request.toolName ?? "TOOL").uppercased()
    }
    private var badgeColor: Color {
        request.kind == .ask ? Theme.cyan : Theme.yellow
    }
    private var title: String {
        if request.kind == .ask { return request.taskName }
        return request.targetFile.isEmpty
            ? request.taskName
            : (request.targetFile as NSString).lastPathComponent
    }
    // One-line signal of what changes: "+X -Y" for edits, the command for Bash.
    private var subtitle: String? {
        if request.kind == .ask { return nil }
        if let diff = request.diff, !diff.isEmpty {
            let rows = parseDiffLines(diff)
            let added = rows.filter { $0.1 == .added && !$0.0.isEmpty }.count
            let removed = rows.filter { $0.1 == .removed && !$0.0.isEmpty }.count
            return "改 \(added + removed) 行  +\(added) -\(removed)"
        }
        if let cmd = request.command, !cmd.isEmpty {
            return "$ " + String(cmd.prefix(70))
        }
        return request.reason
    }
}

// Compact button for the dense queue rows.
struct MiniBtnStyle: ButtonStyle {
    var bg: Color; var fg: Color
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11, weight: .semibold))
            .padding(.vertical, 6).padding(.horizontal, 12)
            .background(configuration.isPressed ? bg.opacity(0.8) : bg)
            .foregroundColor(fg)
            .cornerRadius(8)
    }
}

struct RiskBadge: View {
    let assessment: ApprovalRiskAssessment
    var compact = false

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: assessment.level.symbolName)
            Text(assessment.level.label)
        }
        .font(NotchFont.mono(compact ? 8 : 9, .bold))
        .foregroundColor(assessment.level.color)
        .padding(.horizontal, compact ? 5 : 7)
        .padding(.vertical, compact ? 2 : 4)
        .background(assessment.level.color.opacity(0.12))
        .clipShape(Capsule())
        .accessibilityLabel(assessment.level.label)
    }
}

struct ApprovalView: View {
    @ObservedObject var viewModel: NotchViewModel
    var body: some View {
        let request = viewModel.currentRequest
        let context = request?.diff ?? request?.command
        let assessment = request?.riskAssessment
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Circle().fill(assessment?.level.color ?? Theme.yellow).frame(width: 8, height: 8)
                Text("\(request?.agentName ?? "Agent") 请求授权")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(Theme.textMain)
                Spacer()
                if let request, let assessment {
                    RiskBadge(assessment: assessment)
                    RequestCountdown(request: request, color: assessment.level.color)
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 18)

            HStack(alignment: .top, spacing: 6) {
                Image(systemName: assessment?.level.symbolName ?? "exclamationmark.triangle.fill")
                    .foregroundColor(assessment?.level.color ?? Theme.yellow)
                    .font(.system(size: 11))
                VStack(alignment: .leading, spacing: 4) {
                    Text(request?.taskName ?? "Permission request")
                        .foregroundColor(Theme.textMuted)
                    HStack(spacing: 6) {
                        if let tool = request?.toolName, !tool.isEmpty {
                            Text(tool.uppercased())
                                .foregroundColor(Theme.yellow)
                        }
                        if let cwd = request?.cwd, !cwd.isEmpty {
                            Text((cwd as NSString).lastPathComponent)
                                .foregroundColor(Theme.textMuted)
                        }
                    }
                    .font(NotchFont.mono(9))
                    if let target = request?.targetFile, !target.isEmpty {
                        Text(target)
                            .foregroundColor(Theme.textMain)
                            .lineLimit(2)
                            .textSelection(.enabled)
                    }
                    if let reason = request?.reason, !reason.isEmpty {
                        Text(reason)
                            .foregroundColor(Theme.yellow)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let assessment {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(assessment.reasons, id: \.self) { reason in
                                Text("• \(reason)")
                                    .foregroundColor(assessment.level.color)
                            }
                        }
                        .font(NotchFont.mono(9, .medium))
                        .padding(.top, 2)
                    }
                }
                Spacer(minLength: 0)
            }
            .font(NotchFont.mono(12, .medium))
            .padding(.horizontal, 18)

            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    let diffRaw = request?.diff
                    let isDiff = diffRaw?.isEmpty == false
                    Text(diffSummaryLabel(isDiff: isDiff, diff: diffRaw, request: request))
                        .font(NotchFont.mono(9))
                        .foregroundColor(Theme.textMuted)
                    if let context, !context.isEmpty {
                        if isDiff, let diffRaw {
                            DiffTextView(text: diffRaw)
                        } else {
                            Text(context)
                                .foregroundColor(Theme.textMain)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    } else if let target = request?.targetFile, !target.isEmpty {
                        Text(target)
                            .foregroundColor(Theme.textMain)
                            .textSelection(.enabled)
                    } else {
                        Text("Hook 未提供更多上下文；不会展示推测或示例内容。")
                            .foregroundColor(Theme.textMuted)
                    }
                }
                .font(NotchFont.mono(11, .regular))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
            }
            .frame(maxHeight: 240)
            .background(Theme.codeBg)
            .cornerRadius(10)
            .padding(.horizontal, 16)

            VStack(spacing: 8) {
                HStack(spacing: 12) {
                    Button(action: {
                        viewModel.respondToApproval(action: "deny")
                    }) {
                        Text("拒绝  ⌘N").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(NotchBtnStyle(bg: Theme.buttonGray, fg: Theme.textMain))
                    .keyboardShortcut("n", modifiers: [.command])
                    .accessibilityLabel("拒绝请求")

                    Button(action: {
                        viewModel.respondToApproval(action: "allow")
                    }) {
                        Text((assessment?.level.rank ?? 0) >= ApprovalRisk.high.rank
                             ? "仍允许本次  ⌘Y" : "仅允许本次  ⌘Y")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(NotchBtnStyle(bg: Theme.textMain, fg: Theme.bg))
                    .keyboardShortcut("y", modifiers: [.command])
                    .accessibilityLabel("仅允许本次请求")
                }

            }
            .padding(.horizontal, 16)
            .padding(.bottom, 16)
        }
    }
}

struct AskView: View {
    @ObservedObject var viewModel: NotchViewModel

    var body: some View {
        let request = viewModel.currentRequest
        let question = viewModel.currentAskQuestion
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "bubble.left.fill").foregroundColor(Theme.cyan).font(.system(size: 13))
                Text("\(request?.agentName ?? "Agent") 需要你的回答")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(Theme.textMain)
                Spacer()
                if let request {
                    RequestCountdown(request: request, color: Theme.cyan)
                }
                if let request, request.questions.count > 1 {
                    Text("\(viewModel.askQuestionIndex + 1)/\(request.questions.count)")
                        .font(NotchFont.mono(10))
                        .foregroundColor(Theme.cyan)
                }
                if viewModel.pendingRequests.count > 1 {
                    QueueNav(vm: viewModel)
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 18)

            if let question {
                VStack(alignment: .leading, spacing: 5) {
                    Text(question.header.uppercased())
                        .font(NotchFont.mono(9))
                        .foregroundColor(Theme.cyan)
                    Text(question.question)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(Theme.textMain)
                        .fixedSize(horizontal: false, vertical: true)
                    if question.multiSelect {
                        Text("可多选")
                            .font(.system(size: 10))
                            .foregroundColor(Theme.textMuted)
                    }
                }
                .padding(.horizontal, 18)
            }

            ScrollView {
                VStack(spacing: 8) {
                    if let question {
                        ForEach(Array(question.options.enumerated()), id: \.element.id) { index, option in
                            AskSelectableOption(
                                key: index < 9 ? "⌘\(index + 1)" : "\(index + 1)",
                                text: option.label,
                                description: option.description,
                                isSelected: viewModel.isAskOptionSelected(option),
                                isMultiSelect: question.multiSelect,
                                shortcut: index < 9 ? Character(String(index + 1)) : nil
                            ) {
                                viewModel.chooseAskOption(option)
                            }
                        }
                    } else {
                        Text("问题格式无效，已禁止静默继续。")
                            .font(.system(size: 12))
                            .foregroundColor(Theme.yellow)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 4)
            }

            HStack(spacing: 10) {
                Button(action: { viewModel.denyCurrentRequest() }) {
                    Text("拒绝回答").frame(maxWidth: .infinity)
                }
                .buttonStyle(NotchBtnStyle(bg: Theme.buttonGray, fg: Theme.textMain))
                .keyboardShortcut(.escape, modifiers: [])
                .accessibilityHint("阻止当前提问继续")

                if viewModel.askQuestionIndex > 0 {
                    Button(action: { viewModel.previousAskQuestion() }) {
                        Text("上一步").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(NotchBtnStyle(bg: Theme.buttonGray, fg: Theme.textMain))
                }

                if let question, question.multiSelect {
                    let hasSelection = !(viewModel.askSelections[question.id] ?? []).isEmpty
                    Button(action: {
                        viewModel.submitMultiSelectAnswer()
                    }) {
                        Text(viewModel.askQuestionIndex + 1 < (request?.questions.count ?? 0)
                             ? "继续" : "提交")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(NotchBtnStyle(bg: Theme.textMain, fg: Theme.bg))
                    .disabled(!hasSelection)
                    .opacity(hasSelection ? 1 : 0.45)
                }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 16)
        }
    }
}

struct AskOption: View {
    var key: String
    var text: String
    var action: () -> Void
    @State private var isHovered = false
    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                Text(key).font(NotchFont.mono(11, .bold)).foregroundColor(Theme.cyan).padding(.horizontal, 6).padding(.vertical, 3).background(Theme.cyan.opacity(0.15)).cornerRadius(6)
                Text(text)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(Theme.textMain)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
            }.padding(10).background(isHovered ? Theme.buttonGray : Theme.cardBg).cornerRadius(11)
        }.buttonStyle(PlainButtonStyle()).onHover { h in isHovered = h }
    }
}

struct AskSelectableOption: View {
    var key: String
    var text: String
    var description: String?
    var isSelected: Bool
    var isMultiSelect: Bool
    var shortcut: Character?
    var action: () -> Void
    @State private var isHovered = false

    var body: some View {
        Group {
            if let shortcut {
                optionButton.keyboardShortcut(KeyEquivalent(shortcut), modifiers: [.command])
            } else {
                optionButton
            }
        }
    }

    private var optionButton: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                Text(key)
                    .font(NotchFont.mono(11, .bold))
                    .foregroundColor(Theme.cyan)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .background(Theme.cyan.opacity(0.15))
                    .cornerRadius(6)
                VStack(alignment: .leading, spacing: 3) {
                    Text(text)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(Theme.textMain)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    if let description, !description.isEmpty {
                        Text(description)
                            .font(NotchFont.mono(10))
                            .foregroundColor(Theme.textMuted)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer()
                Image(systemName: isMultiSelect
                      ? (isSelected ? "checkmark.square.fill" : "square")
                      : (isSelected ? "largecircle.fill.circle" : "circle"))
                    .foregroundColor(isSelected ? Theme.cyan : Theme.textMuted)
            }
            .padding(10)
            .background(isSelected ? Theme.buttonGray : (isHovered ? Theme.buttonGray : Theme.cardBg))
            .cornerRadius(11)
        }
        .buttonStyle(PlainButtonStyle())
        .onHover { hovered in isHovered = hovered }
        .accessibilityLabel(text)
        .accessibilityHint(isMultiSelect ? "切换此选项" : "选择此选项并继续")
    }
}

struct NotchBtnStyle: ButtonStyle {
    var bg: Color; var fg: Color
    func makeBody(configuration: Configuration) -> some View {
                configuration.label.font(.system(size: 13, weight: .semibold)).padding(.vertical, 11).background(configuration.isPressed ? bg.opacity(0.8) : bg).foregroundColor(fg).cornerRadius(11)
    }
}

// MARK: - Controller Window
class ControllerWindowDelegate: NSObject, NSWindowDelegate { func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return true } }
struct ControllerView: View {
    @ObservedObject var viewModel: NotchViewModel
    var body: some View {
        VStack(spacing: 20) {
            Text("Vibe Center Controls").font(.system(size: 16, weight: .semibold))
            HStack(spacing: 12) {
                CtrlBtn(title: "Dismiss", icon: "minus", color: Theme.green) { viewModel.denyCurrentRequest() }
                CtrlBtn(title: "Overview", icon: "square.grid.2x2", color: .white) { viewModel.isPinned = true }
            }
        }.padding(24).frame(width: 320, height: 130).background(VisualEffectView(material: .hudWindow, blendingMode: .behindWindow))
    }
}
struct CtrlBtn: View {
    var title: String; var icon: String; var color: Color; var action: () -> Void
    var body: some View { Button(action: action) { VStack(spacing: 8) { Image(systemName: icon).font(.system(size: 18)).foregroundColor(color); Text(title).font(.system(size: 12, weight: .medium)) }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(.vertical, 14).background(Color.white.opacity(0.1)).cornerRadius(14) }.buttonStyle(PlainButtonStyle()) }
}
struct VisualEffectView: NSViewRepresentable {
    var material: NSVisualEffectView.Material; var blendingMode: NSVisualEffectView.BlendingMode
    func makeNSView(context: Context) -> NSVisualEffectView { let view = NSVisualEffectView(); view.material = material; view.blendingMode = blendingMode; view.state = .active; return view }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

// MARK: - Settings Window
struct SettingsView: View {
    @ObservedObject var vm: NotchViewModel
    let onUninstallHook: () -> Void
    let onClose: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Vibe Center").font(.system(size: 16, weight: .semibold))

                GroupBox("运行状态") {
                    VStack(alignment: .leading, spacing: 10) {
                        healthRow(health: vm.ipcHealth)
                        HStack(spacing: 8) {
                            Image(systemName: vm.isScanningSessions ? "arrow.triangle.2.circlepath" : "rectangle.stack")
                                .foregroundColor(vm.isScanningSessions ? Theme.blue : Theme.textMuted)
                            Text(vm.isScanningSessions ? "正在扫描 Agent 会话" : vm.overviewSessionCountLabel)
                                .font(NotchFont.mono(10))
                                .foregroundColor(Theme.textMuted)
                            Spacer()
                            Button("刷新") { vm.refreshSessions() }
                                .disabled(vm.isScanningSessions)
                        }
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                GroupBox("通知") {
                    VStack(alignment: .leading, spacing: 10) {
                        Toggle("系统通知", isOn: $vm.notificationsEnabled)
                            .toggleStyle(.switch)
                        Toggle("提醒声音", isOn: $vm.notificationSoundsEnabled)
                            .toggleStyle(.switch)
                            .disabled(!vm.notificationsEnabled)
                        Divider()
                        Toggle("Agent 等待输入", isOn: $vm.waitingNotificationsEnabled)
                            .toggleStyle(.switch)
                            .disabled(!vm.notificationsEnabled)
                        Toggle("Agent 执行失败", isOn: $vm.failureNotificationsEnabled)
                            .toggleStyle(.switch)
                            .disabled(!vm.notificationsEnabled)
                        Toggle("Agent 回合完成", isOn: $vm.completionNotificationsEnabled)
                            .toggleStyle(.switch)
                            .disabled(!vm.notificationsEnabled)
                        Toggle("自动启动用量监测", isOn: $vm.autoStartUsage)
                            .toggleStyle(.switch)
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                GroupBox("显示位置") {
                    VStack(alignment: .leading, spacing: 10) {
                        Picker("岛所在显示器", selection: $vm.displaySelection) {
                            Text("自动（优先刘海屏）").tag("automatic")
                            Text("主显示器").tag("main")
                            Text("跟随鼠标所在显示器").tag("pointer")
                            ForEach(vm.availableDisplays) { display in
                                Text(display.label).tag("display:\(display.id)")
                            }
                        }
                        .pickerStyle(.menu)
                        Text("指定显示器断开后会临时回退，重新连接后自动恢复。")
                            .font(.system(size: 10))
                            .foregroundColor(Theme.textMuted)
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                GroupBox("审批隐私") {
                    VStack(alignment: .leading, spacing: 10) {
                        Toggle("保留本机决策历史", isOn: $vm.decisionHistoryEnabled)
                            .toggleStyle(.switch)
                        Text("仅保存 Agent 类别、工具类别、风险、结果和时间；不保存命令、diff、路径、问题或回答。")
                            .font(.system(size: 10))
                            .foregroundColor(Theme.textMuted)
                        if vm.decisionHistoryEnabled {
                            if vm.approvalDecisionHistory.isEmpty {
                                Text("暂无审批记录")
                                    .font(NotchFont.mono(10))
                                    .foregroundColor(Theme.textMuted)
                            } else {
                                ForEach(Array(vm.approvalDecisionHistory.prefix(5))) { entry in
                                    DecisionHistoryRow(entry: entry)
                                }
                                Button("清除历史") { vm.clearApprovalDecisionHistory() }
                            }
                        }
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                GroupBox("Claude Code Hook") {
                    VStack(alignment: .leading, spacing: 10) {
                        healthRow(health: vm.hookHealth)
                        HStack(spacing: 10) {
                            Button("安装 / 修复 Hook") { vm.installHook() }
                            Button("卸载 Hook") { onUninstallHook() }
                        }
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                GroupBox("Z.ai 用量") {
                    VStack(alignment: .leading, spacing: 10) {
                        healthRow(health: vm.usageHealth)
                        Button("重启用量服务") { vm.restartUsage() }
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                HStack {
                    Spacer()
                    Button("完成") { onClose() }
                        .keyboardShortcut(.return, modifiers: [])
                }
            }
            .padding(20)
        }
        .frame(width: 390, height: 640)
    }

    private func healthRow(health: ServiceHealth) -> some View {
        HStack(spacing: 8) {
            Circle().fill(health.color).frame(width: 8, height: 8)
            Text(health.title).font(.system(size: 12, weight: .medium))
            Spacer(minLength: 4)
            Text(health.detail)
                .font(NotchFont.mono(10))
                .foregroundColor(Theme.textMuted)
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }
}

struct DecisionHistoryRow: View {
    let entry: ApprovalDecisionHistoryEntry

    var body: some View {
        HStack(spacing: 7) {
            Circle().fill(entry.risk.color).frame(width: 6, height: 6)
            Text(entry.summaryLabel)
                .font(NotchFont.mono(9))
                .foregroundColor(Theme.textMain)
            Text(entry.risk.label)
                .font(NotchFont.mono(8, .bold))
                .foregroundColor(entry.risk.color)
            Spacer(minLength: 4)
            Text(entry.outcomeLabel)
                .font(.system(size: 10, weight: .medium))
                .foregroundColor(entry.outcome == "allow" ? Theme.green : Theme.textMuted)
            Text(entry.decidedAt, style: .relative)
                .font(NotchFont.mono(8))
                .foregroundColor(Theme.textMuted)
        }
        .padding(.vertical, 2)
    }
}

// Key-capable, non-activating panel for the notch. A plain borderless
// NSWindow returns canBecomeKey == false, so SwiftUI keyboardShortcut
// bindings (⌘Y/⌘N/⌘1-9/Esc in the approval/ask cards) never fire. Using an
// NSPanel with .nonactivatingPanel means we can take key focus to receive
// those shortcuts without yanking app activation away from the terminal the
// user is working in.
final class NotchPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

// MARK: - Entry
#if !VIBE_ISLAND_UNIT_TESTS
@main struct VibeIslandApp: App { @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate; var body: some Scene { Settings { EmptyView() } } }
#endif

class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    #if VIBE_ISLAND_TESTING
    static weak var testingInstance: AppDelegate?
    #endif

    var notchWindow: NSWindow!; var controlWindow: NSWindow!; let viewModel = NotchViewModel(); let controlDelegate = ControllerWindowDelegate(); var server: LocalServer!
    var unixServer: UnixSocketServer?
    var scanTimer: Timer?
    // Real-time file watchers for agent rollout/transcript files.
    // When an agent writes to its file (actively working), we update
    // running=true immediately — no 10s poll delay.
    private var fileWatchers: [String: FileWatcher] = [:]
    private var runningTimeouts: [String: Timer] = [:]  // per-session timeout
    private var stateCancellable: AnyCancellable?
    private var scanInFlight = false

    // App-level objects that the view-model callbacks drive.
    private var notifier: NotificationCoordinator?
    private var statusItem: NSStatusItem?
    private var settingsWindow: NSWindow?
    private var usageProcess: Process?
    private var resolvedAutomaticDisplayID: CGDirectDisplayID?
    private var recentSessionNotifications: [String: Date] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        #if VIBE_ISLAND_TESTING
        Self.testingInstance = self
        #endif

        // Accessory (background) policy: no Dock icon, no stealing focus from
        // other apps. The notch is an overlay, not a regular app.
        NSApp.setActivationPolicy(.accessory)

        // Load Departure Mono (bundled next to the executable).
        NotchFont.registerIfNeeded()

        refreshAvailableDisplays()
        let screen = preferredScreen(); let screenRect = screen.frame

        // Window sized to the notch body only (NOT full screen width), so it
        // doesn't cover the menu bar / other apps' window controls. Position
        // is top-centered; size tracks the active notch state.
        let initialSize = notchSize(for: viewModel.activeState)
        // NotchPanel (NSPanel + .nonactivatingPanel) so it can become key for
        // keyboard shortcuts without stealing app activation from the terminal.
        let panel = NotchPanel(
            contentRect: NSRect(x: 0, y: 0, width: initialSize.width, height: initialSize.height),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        // NSPanel defaults to hiding when its app deactivates. The island is
        // a persistent status surface, so it must remain visible after an
        // approval closes and focus returns to the user's terminal/editor.
        panel.hidesOnDeactivate = false
        notchWindow = panel
        notchWindow.isOpaque = false
        notchWindow.backgroundColor = NSColor.clear
        notchWindow.hasShadow = false
        // .screenSaver is above the menu bar, so the notch can render on
        // top of it (like the real app). Safe now because the window is
        // sized to the notch body only — it can't cover other apps'
        // window controls the way the old full-width window did.
        notchWindow.level = .screenSaver
        notchWindow.ignoresMouseEvents = false
        notchWindow.collectionBehavior = [.canJoinAllSpaces, .stationary]
        centerAtTop(notchWindow, size: initialSize, screenRect: screenRect)
        notchWindow.contentView = NSHostingView(rootView: NotchView(viewModel: viewModel))
        // Show without taking key focus at launch; key is acquired on demand
        // when an approval/ask prompt appears (see updateNotchFrame).
        notchWindow.orderFrontRegardless()

        // Keep the window frame pinned to the notch body as the state
        // changes (compact ↔ overview ↔ approval). This is what prevents
        // the window from covering other apps' controls when collapsed.
        stateCancellable = viewModel.objectWillChange.sink { [weak self] in
            DispatchQueue.main.async { self?.updateNotchFrame() }
        }

        // (Control panel window removed — it was for early testing and its
        // close button would terminate the whole app. The notch is now
        // fully driven by hover + IPC.)

        do {
            let authenticator = try IPCAuthenticator.loadOrCreate()
            server = try LocalServer(
                viewModel: viewModel,
                port: 14321,
                authenticator: authenticator
            )
            server.start()
        } catch {
            NSLog("LocalServer failed to start: \(error)")
            viewModel.ipcHealth = .failed("端口 14321 启动失败：\(error.localizedDescription)")
        }

        // Try to take over the real app's Unix socket. Succeeds only if the
        // real app is NOT running (only one listener per socket file). If it
        // fails, we just keep running on TCP — the user can quit the real
        // app and restart us to enable the live bridge feed.
        let socketPath = NSHomeDirectory() + "/.vibe-island/run/vibe-island.sock"
        if let srv = UnixSocketServer(viewModel: viewModel, socketPath: socketPath) {
            srv.start()
            unixServer = srv
        } else {
            NSLog("UnixSocketServer not started (real app may own the socket)")
        }

        // Populate the agent list immediately by scanning running agent
        // processes (claude / zcode / codex). This delivers the "open the
        // app and existing sessions show up" experience without needing
        // the bridge handshake protocol.
        scanRunningAgents()

        // Re-scan every 5s so closed agents disappear quickly and running
        // state stays responsive.
        scanTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.scanRunningAgents()
        }

        // Re-pin the notch when the display layout changes (plug/unplug
        // external monitor, resolution change).
        NotificationCenter.default.addObserver(
            self, selector: #selector(handleScreenChange),
            name: NSApplication.didChangeScreenParametersNotification, object: nil
        )

        // App-level wiring: callbacks, menu-bar item, notifications, hook
        // health, and the optional usage daemon. Previously the view-model's
        // action callbacks were all nil, so every overview button (refresh /
        // settings / retry) silently did nothing.
        wireCallbacks()
        setupStatusItem()

        notifier = NotificationCoordinator()
        notifier?.configure(enabled: viewModel.notificationsEnabled)
        notifier?.onOpenRequest = { [weak self] requestID in
            DispatchQueue.main.async { self?.viewModel.focusRequest(id: requestID) }
        }
        notifier?.onOpenSession = { [weak self] sessionID in
            DispatchQueue.main.async { self?.viewModel.returnToSession(id: sessionID) }
        }

        refreshHookHealth()
        if viewModel.autoStartUsage {
            startUsageDaemon()
        }
    }

    // Route view-model actions to app-level handlers. onDecision stays owned
    // by LocalServer (set in its init); the rest are wired here.
    private func wireCallbacks() {
        viewModel.onRefreshSessions = { [weak self] in
            guard let self else { return }
            self.scanRunningAgents()
        }
        viewModel.onOpenSettings = { [weak self] in self?.openSettingsWindow() }
        viewModel.onRestartUsage = { [weak self] in self?.restartUsageDaemon() }
        viewModel.onInstallHook = { [weak self] in self?.installHook() }
        viewModel.onQuitRequested = { NSApp.terminate(nil) }
        viewModel.onNotificationSettingsChanged = { [weak self] in
            self?.notifier?.configure(enabled: self?.viewModel.notificationsEnabled ?? false)
        }
        viewModel.onAutoUsageChanged = { [weak self] enabled in
            if enabled { self?.startUsageDaemon() } else { self?.stopUsageDaemon() }
        }
        viewModel.onDisplaySelectionChanged = { [weak self] in
            guard let self else { return }
            self.resolvedAutomaticDisplayID = nil
            self.updateNotchFrame()
        }
        viewModel.onSessionEvent = { [weak self] event in
            self?.notifySessionEventIfNeeded(event)
        }
        viewModel.onRequestEnqueued = { [weak self] request in
            guard let self, self.viewModel.notificationsEnabled,
                  !self.viewModel.isHovered, !self.viewModel.isPinned else { return }
            self.notifier?.post(request: request,
                                soundsEnabled: self.viewModel.notificationSoundsEnabled)
        }
    }

    @objc private func handleScreenChange() {
        DispatchQueue.main.async {
            self.refreshAvailableDisplays()
            let connectedIDs = Set(NSScreen.screens.compactMap(Self.displayID))
            if let resolved = self.resolvedAutomaticDisplayID,
               !connectedIDs.contains(resolved) {
                self.resolvedAutomaticDisplayID = nil
            }
            self.updateNotchFrame()
        }
    }

    private func notifySessionEventIfNeeded(_ event: SessionEvent) {
        guard viewModel.shouldNotify(for: event),
              !viewModel.isHovered, !viewModel.isPinned else { return }
        let now = Date()
        recentSessionNotifications = recentSessionNotifications.filter {
            now.timeIntervalSince($0.value) < 60
        }
        let key = "\(event.kind.rawValue):\(event.sessionID)"
        if let previous = recentSessionNotifications[key],
           now.timeIntervalSince(previous) < 10 {
            return
        }
        recentSessionNotifications[key] = now
        notifier?.post(event: event, soundsEnabled: viewModel.notificationSoundsEnabled)
    }

    // Tear down child processes so quitting the app doesn't orphan the usage
    // daemon (whose single-instance lock would then block relaunch).
    func applicationWillTerminate(_ notification: Notification) {
        usageProcess?.terminate()
        usageProcess = nil
    }

    // MARK: Menu bar item

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            let symbol = NSImage(systemSymbolName: "waveform",
                                 accessibilityDescription: "Vibe Center")
            button.image = symbol
            button.image?.isTemplate = true
        }
        let menu = NSMenu()
        menu.delegate = self
        populateStatusMenu(menu)
        item.menu = menu
        statusItem = item
    }

    private func populateStatusMenu(_ menu: NSMenu) {
        menu.addItem(withTitle: "显示面板", action: #selector(showPanelFromMenu),
                     keyEquivalent: "").target = self
        menu.addItem(withTitle: "刷新会话", action: #selector(refreshFromMenu),
                     keyEquivalent: "r").target = self
        menu.addItem(withTitle: "设置…", action: #selector(openSettingsFromMenu),
                     keyEquivalent: ",").target = self
        menu.addItem(.separator())
        let usageItem = menu.addItem(withTitle: "自动监测用量",
                                     action: #selector(toggleAutoUsageFromMenu),
                                     keyEquivalent: "")
        usageItem.target = self
        usageItem.state = viewModel.autoStartUsage ? .on : .off
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 Vibe Center", action: #selector(quitFromMenu),
                     keyEquivalent: "q").target = self
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        populateStatusMenu(menu)
    }

    @objc private func refreshFromMenu() { viewModel.refreshSessions() }
    @objc private func showPanelFromMenu() {
        viewModel.isPinned = true
        notchWindow.orderFrontRegardless()
    }
    @objc private func openSettingsFromMenu() { viewModel.openSettings() }
    @objc private func toggleAutoUsageFromMenu() { viewModel.autoStartUsage.toggle() }
    @objc private func quitFromMenu() { viewModel.quitApp() }

    // MARK: Settings window

    private func openSettingsWindow() {
        if let win = settingsWindow {
            win.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 430, height: 680),
                           styleMask: [.titled, .closable], backing: .buffered, defer: false)
        win.title = "Vibe Center"
        win.titlebarAppearsTransparent = false
        win.isReleasedWhenClosed = false
        win.contentView = NSHostingView(rootView:
            SettingsView(vm: viewModel,
                         onUninstallHook: { [weak self] in self?.uninstallHook() },
                         onClose: { [weak self] in self?.settingsWindow?.close() }))
        win.center()
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow = win
    }

    // MARK: Usage daemon lifecycle

    private func usageDaemonURL() -> URL? {
        bundledScript("usage-daemon.py")
    }

    private func startUsageDaemon() {
        guard usageProcess == nil else { return }
        guard let script = usageDaemonURL() else {
            viewModel.usageHealth = .failed("未找到 usage-daemon.py")
            return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3", script.path]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        task.terminationHandler = { [weak self] finishedTask in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.usageProcess === finishedTask {
                    self.usageProcess = nil
                }
                if self.viewModel.autoStartUsage,
                   self.viewModel.usageHealth.kind == .checking {
                    self.viewModel.usageHealth = .failed(
                        "用量服务已退出（状态 \(finishedTask.terminationStatus)）"
                    )
                }
            }
        }
        do {
            try task.run()
            usageProcess = task
            viewModel.usageHealth = .checking("正在启动配额服务")
        } catch {
            viewModel.usageHealth = .failed("无法启动配额服务：\(error.localizedDescription)")
        }
    }

    private func stopUsageDaemon() {
        usageProcess?.terminate()
        usageProcess = nil
        viewModel.usageHealth = .disabled("用量监测已关闭")
    }

    private func restartUsageDaemon() {
        if !viewModel.autoStartUsage {
            viewModel.autoStartUsage = true
            return
        }
        stopUsageDaemon()
        // Give the process a moment to die so its single-instance file lock
        // releases before we relaunch.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            self?.startUsageDaemon()
        }
    }

    // MARK: Hook install / health

    private func installHookURL() -> URL? { bundledScript("install-hook.sh") }

    private func installHook() { runHookScript(arguments: [], installing: true) }
    private func uninstallHook() { runHookScript(arguments: ["--uninstall"], installing: false) }

    private func runHookScript(arguments: [String], installing: Bool) {
        guard let script = installHookURL() else {
            viewModel.hookHealth = .failed("未找到 install-hook.sh")
            return
        }
        viewModel.hookHealth = .checking(installing ? "正在安装 Hook" : "正在卸载 Hook")
        DispatchQueue.global().async { [weak self] in
            let task = Process()
            task.executableURL = script
            task.arguments = arguments
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            do {
                try task.run()
                task.waitUntilExit()
            } catch {
                DispatchQueue.main.async {
                    self?.viewModel.hookHealth = .failed("Hook 操作失败：\(error.localizedDescription)")
                }
                return
            }
            guard task.terminationStatus == 0 else {
                DispatchQueue.main.async {
                    self?.viewModel.hookHealth = .failed(
                        "Hook 操作失败（退出码 \(task.terminationStatus)）"
                    )
                }
                return
            }
            DispatchQueue.main.async { self?.refreshHookHealth() }
        }
    }

    private func refreshHookHealth() {
        DispatchQueue.global().async { [weak self] in
            let coverage = Self.hookCoverage()
            DispatchQueue.main.async {
                if coverage.configured == coverage.required, coverage.authenticated {
                    self?.viewModel.hookHealth = .ready("Claude Code Hook 已就绪")
                } else if coverage.configured == coverage.required {
                    self?.viewModel.hookHealth = .warning("Hook 版本过旧，请安装 / 修复")
                } else if coverage.configured == 0 {
                    self?.viewModel.hookHealth = .warning("Hook 未安装")
                } else {
                    self?.viewModel.hookHealth = .warning(
                        "Hook 不完整（\(coverage.configured)/\(coverage.required)）"
                    )
                }
            }
        }
    }

    // Mirrors install-hook.sh's is_relay_entry detection: any hook command
    // whose lowercased path contains "vibe-island" and ends in "/relay.py".
    private static func hookCoverage() -> (configured: Int, required: Int, authenticated: Bool) {
        let requiredEvents = [
            "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "PostToolUseFailure", "Stop", "StopFailure", "Notification",
            "PermissionRequest", "SessionEnd",
        ]
        let env = ProcessInfo.processInfo.environment
        let path = env["CLAUDE_SETTINGS"] ?? (NSHomeDirectory() + "/.claude/settings.json")
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let hooks = cfg["hooks"] as? [String: Any] else {
            return (0, requiredEvents.count, false)
        }
        var configured = 0
        var authenticated = false
        for event in requiredEvents {
            guard let value = hooks[event] else { continue }
            guard let entries = value as? [[String: Any]] else { continue }
            var eventHasRelay = false
            for entry in entries {
                guard let hookList = entry["hooks"] as? [[String: Any]] else { continue }
                for h in hookList {
                    let originalCommand = (h["command"] as? String) ?? ""
                    let normalizedCommand = originalCommand.lowercased()
                        .replacingOccurrences(of: "\\", with: "/")
                    if normalizedCommand.contains("vibe-island"),
                       normalizedCommand.hasSuffix("/relay.py") {
                        eventHasRelay = true
                        authenticated = authenticated
                            || relaySupportsAuthenticatedIPC(command: originalCommand)
                        break
                    }
                }
                if eventHasRelay { break }
            }
            if eventHasRelay { configured += 1 }
        }
        return (configured, requiredEvents.count, authenticated)
    }

    private static func relaySupportsAuthenticatedIPC(command: String) -> Bool {
        guard let rawPath = command.split(whereSeparator: { $0.isWhitespace }).last else {
            return false
        }
        let trimmed = String(rawPath).trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        let path = (trimmed as NSString).expandingTildeInPath
        guard let source = try? String(contentsOfFile: path, encoding: .utf8) else {
            return false
        }
        return source.contains("VIBE_ISLAND_IPC_TOKEN_FILE")
            && source.contains("auth_signature")
    }

    // Find a helper script bundled in Resources/bin or next to the binary.
    private func bundledScript(_ name: String) -> URL? {
        let candidates = [
            Bundle.main.resourceURL?.appendingPathComponent("bin/\(name)"),
            Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent(name),
            URL(fileURLWithPath: name),
        ].compactMap { $0 }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0.path) }
            ?? candidates.first { FileManager.default.fileExists(atPath: $0.path) }
    }

    // Set up real-time file watchers for each session's data file.
    // Called after each process scan. Only creates new watchers (idempotent).
    private func setupFileWatchers() {
        for (id, sess) in viewModel.sessions {
            guard fileWatchers[id] == nil else { continue }
            // Determine the file to watch for this session.
            let watchPath: String?
            if let transcriptPath = sess.transcriptPath, !transcriptPath.isEmpty {
                watchPath = transcriptPath
            } else if sess.source == "zcode" {
                watchPath = NSHomeDirectory() + "/.zcode/cli/rollout/model-io-\(id).jsonl"
            } else if sess.source == "claude", let cwd = sess.cwd, !cwd.isEmpty {
                let encoded = cwd.replacingOccurrences(of: "/", with: "-")
                let projDir = NSHomeDirectory() + "/.claude/projects/" + encoded
                watchPath = mostRecentFile(in: projDir, matching: "*.jsonl")
            } else {
                watchPath = nil
            }
            guard let path = watchPath, FileManager.default.fileExists(atPath: path) else { continue }

            let watcher = FileWatcher(path: path) { [weak self] in
                self?.onAgentFileChanged(sessionID: id, source: sess.source)
            }
            if let w = watcher {
                fileWatchers[id] = w
                NSLog("FileWatcher: watching \(path) for session \(id)")
            }
        }
    }

    // Find the most recently modified file in a directory matching a glob.
    private func mostRecentFile(in dir: String, matching pattern: String) -> String? {
        guard FileManager.default.fileExists(atPath: dir) else { return nil }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/find")
        task.arguments = [dir, "-name", pattern, "-type", "f",
                          "-exec", "stat", "-f", "%m %N", "{}", ";"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle()
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let output = String(data: data, encoding: .utf8) else { return nil }
        // Parse "epoch path" lines, pick the highest epoch.
        var bestEpoch: TimeInterval = 0
        var bestPath: String?
        for line in output.split(separator: "\n") {
            let parts = line.split(separator: " ", maxSplits: 1)
            if parts.count == 2, let epoch = TimeInterval(parts[0]) {
                if epoch > bestEpoch {
                    bestEpoch = epoch
                    bestPath = String(parts[1]).trimmingCharacters(in: .whitespaces)
                }
            }
        }
        return bestPath
    }

    private func removeFileWatcher(for id: String) {
        fileWatchers.removeValue(forKey: id)
        runningTimeouts[id]?.invalidate()
        runningTimeouts.removeValue(forKey: id)
    }

    // Called immediately when an agent's file is written to.
    private func onAgentFileChanged(sessionID: String, source: String) {
        // Mark as running RIGHT NOW (file is being written = agent working).
        DispatchQueue.main.async {
            // Update running state.
            if var sess = self.viewModel.sessions[sessionID] {
                sess.isRunning = true
                self.viewModel.sessions[sessionID] = sess
            }
            // (Re)start the 20s idle timer: if no file write for 20s,
            // the agent finished its turn → set running=false.
            self.runningTimeouts[sessionID]?.invalidate()
            let t = Timer.scheduledTimer(withTimeInterval: 20, repeats: false) { [weak self] _ in
                DispatchQueue.main.async {
                    if var sess = self?.viewModel.sessions[sessionID] {
                        sess.isRunning = false
                        self?.viewModel.sessions[sessionID] = sess
                    }
                    self?.runningTimeouts.removeValue(forKey: sessionID)
                }
            }
            self.runningTimeouts[sessionID] = t
        }

        // Also update the preview content in background (read latest from file).
        DispatchQueue.global().async { [weak self] in
            guard let self = self else { return }
            let preview: String?
            switch source {
            case "zcode":
                preview = self.readZCodeRolloutPreview(sessionID: sessionID)
            case "codex":
                preview = self.readCodexPreview(sessionID: sessionID)
            case "claude":
                preview = self.readClaudePreview(sessionID: sessionID)
            case "gemini", "qwen":
                preview = self.readGeminiChatPreview(sessionID: sessionID)
            default:
                preview = nil
            }
            if let p = preview, !p.isEmpty {
                DispatchQueue.main.async {
                    if var sess = self.viewModel.sessions[sessionID] {
                        sess.preview = p
                        self.viewModel.sessions[sessionID] = sess
                    }
                }
            }
        }
    }

    // Read latest assistant reply from Codex's most recent rollout.
    private func readCodexPreview(sessionID: String) -> String? {
        guard let sess = viewModel.sessions[sessionID] else { return nil }
        let path = sess.transcriptPath
            ?? mostRecentFile(in: NSHomeDirectory() + "/.codex/sessions",
                              matching: "rollout-*.jsonl")
        guard let path else { return nil }
        guard let data = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        let lines = data.split(separator: "\n", omittingEmptySubsequences: true)
        guard let lastLine = lines.last else { return nil }
        guard let lineData = lastLine.data(using: .utf8),
              let d = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any] else { return nil }
        // Codex event_msg with agent_message.
        if let p = d["payload"] as? [String: Any],
           let ev = p["type"] as? String, ev == "agent_message" {
            let msg = (p["message"] as? String) ?? ""
            return String(msg.trimmingCharacters(in: .whitespacesAndNewlines).prefix(70))
        }
        return nil
    }

    // Read latest assistant reply from Claude's transcript.
    private func readClaudePreview(sessionID: String) -> String? {
        guard let sess = viewModel.sessions[sessionID] else { return nil }
        let path: String?
        if let transcriptPath = sess.transcriptPath, !transcriptPath.isEmpty {
            path = transcriptPath
        } else if let cwd = sess.cwd, !cwd.isEmpty {
            let encoded = cwd.replacingOccurrences(of: "/", with: "-")
            let projDir = NSHomeDirectory() + "/.claude/projects/" + encoded
            path = mostRecentFile(in: projDir, matching: "*.jsonl")
        } else {
            path = nil
        }
        guard let path else { return nil }
        guard let data = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        // Find the last assistant text message.
        var lastText = ""
        for line in data.split(separator: "\n") {
            guard let lineData = line.data(using: .utf8),
                  let d = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any] else { continue }
            guard let msg = d["message"] as? [String: Any],
                  (msg["role"] as? String) == "assistant" else { continue }
            if let content = msg["content"] as? [[String: Any]] {
                for c in content {
                    if (c["type"] as? String) == "text", let t = c["text"] as? String, !t.isEmpty {
                        lastText = t
                    }
                }
            }
        }
        return lastText.isEmpty ? nil : String(lastText.trimmingCharacters(in: .whitespacesAndNewlines).prefix(70))
    }

    // Read the latest model reply from a native Gemini CLI / Qwen Code chat
    // file (JSONL records with type "gemini"/"model"). Antigravity log files
    // fail JSON parsing and return nil.
    private func readGeminiChatPreview(sessionID: String) -> String? {
        guard let sess = viewModel.sessions[sessionID] else { return nil }
        guard let path = sess.transcriptPath, !path.isEmpty else { return nil }
        guard let data = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        var preview = ""
        for line in data.split(separator: "\n") {
            guard let lineData = line.data(using: .utf8),
                  let d = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any] else { continue }
            let type = (d["type"] as? String) ?? ""
            guard type == "gemini" || type == "model" || type == "assistant" else { continue }
            if let content = d["content"] as? String, !content.isEmpty {
                preview = content
            } else if let tcs = d["toolCalls"] as? [[String: Any]], let tc = tcs.first {
                let fn = tc["name"] as? String
                    ?? (tc["function"] as? [String: Any])?["name"] as? String ?? ""
                if !fn.isEmpty { preview = "tool: \(fn)" }
            }
        }
        return preview.isEmpty
            ? nil
            : String(preview.trimmingCharacters(in: .whitespacesAndNewlines).prefix(70))
    }

    // Read the latest assistant reply from ZCode's rollout file.
    private func readZCodeRolloutPreview(sessionID: String) -> String? {
        let path = NSHomeDirectory() + "/.zcode/cli/rollout/model-io-\(sessionID).jsonl"
        guard let data = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        // Last non-empty line.
        let lines = data.split(separator: "\n", omittingEmptySubsequences: true)
        guard let lastLine = lines.last else { return nil }
        guard let lineData = lastLine.data(using: .utf8),
              let d = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any] else { return nil }
        let resp = d["response"] as? [String: Any] ?? [:]
        let text = (resp["text"] as? String) ?? ""
        if !text.isEmpty {
            return String(text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(70))
        }
        // Fall back to tool calls.
        if let tcs = resp["toolCalls"] as? [[String: Any]], let tc = tcs.first {
            let name = tc["toolName"] as? String ?? tc["name"] as? String ?? ""
            if !name.isEmpty { return "tool: \(name)" }
        }
        return nil
    }

    // Scan running agent CLIs (claude / zcode / codex) and reconcile the
    // sessions list: add new, update existing, remove gone. Run in
    // background so it doesn't block the UI.
    private func scanRunningAgents() {
        guard !scanInFlight else { return }
        scanInFlight = true
        viewModel.isScanningSessions = true
        DispatchQueue.global().async { [weak self] in
            guard let self = self else { return }
            // Find scan-agents.sh: bundle Resources or next to the binary.
            let candidates = [
                Bundle.main.resourceURL?.appendingPathComponent("scan-agents.sh"),
                Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("scan-agents.sh"),
                URL(fileURLWithPath: "scan-agents.sh"),
            ].compactMap { $0 }
            guard let scriptURL = candidates.first(where: {
                FileManager.default.isExecutableFile(atPath: $0.path)
            }) else {
                self.finishSessionScan()
                return
            }

            let task = Process()
            task.executableURL = scriptURL
            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = FileHandle()  // discard
            do {
                try task.run()
            } catch {
                NSLog("scan-agents.sh failed: \(error)")
                self.finishSessionScan()
                return
            }
            task.waitUntilExit()
            guard task.terminationStatus == 0 else {
                NSLog("scan-agents.sh exited with status \(task.terminationStatus)")
                self.finishSessionScan()
                return
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            guard let text = String(data: data, encoding: .utf8) else {
                self.finishSessionScan()
                return
            }

            // Collect the IDs from this scan pass.
            var seenIDs = Set<String>()
            for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
                if let id = self.parseScanLine(String(line)) {
                    seenIDs.insert(id)
                }
            }
            NSLog("scan-agents: discovered \(seenIDs.count) session(s)")

            // Reconcile on the main thread: upsert scanned sessions, then
            // remove only sessions that this scanner owns (isScanManaged) and
            // that didn't appear in this pass. Hook/socket-registered sessions
            // (isScanManaged=false) are preserved so active conversations
            // don't vanish mid-turn.
            DispatchQueue.main.async {
                for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
                    self.applyScanLine(String(line))
                }
                let removed = self.viewModel.reconcileScannedSessions(seenIDs: seenIDs)
                for id in removed {
                    self.removeFileWatcher(for: id)
                }
                // Set up real-time file watchers for the currently active
                // agent's data files (rollout for zcode, transcript for claude).
                self.setupFileWatchers()
                self.scanInFlight = false
                self.viewModel.isScanningSessions = false
            }
        }
    }

    private func finishSessionScan() {
        DispatchQueue.main.async { [weak self] in
            self?.scanInFlight = false
            self?.viewModel.isScanningSessions = false
        }
    }

    // Parse a scan line just to get its session_id (for reconciliation).
    private func parseScanLine(_ line: String) -> String? {
        guard let data = line.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (json["session"] as? String) == "start" else { return nil }
        return json["session_id"] as? String
    }

    // Apply one scan-agents.sh output line to the viewModel.
    private func applyScanLine(_ line: String) {
        guard let data = line.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (json["session"] as? String) == "start" else {
            NSLog("applyScanLine: skip non-start line: \(line.prefix(80))")
            return
        }
        let id = (json["session_id"] as? String) ?? ""
        let source = (json["source"] as? String) ?? "claude"
        let task = (json["task"] as? String) ?? "Session"
        let detail = json["detail"] as? String
        let displayDetail = json["display_detail"] as? String
        let cwd = json["cwd"] as? String
        let preview = json["preview"] as? String
        let terminal = json["terminal"] as? String
        let transcriptPath = json["transcript_path"] as? String
        let lastTs = json["last_ts"] as? String
        let running = json["running"] as? Bool
        // pid is emitted as a string by scan-agents.sh.
        let pidValue = (json["pid"] as? String).flatMap { Int32($0) }
            ?? (json["pid"] as? NSNumber).map { Int32(truncating: $0) }
        // Parse ISO 8601 timestamp so relative-time shows the real last
        // activity, not the rescan time.
        var lastUpdate: Date? = nil
        if let ts = lastTs, !ts.isEmpty {
            let df = ISO8601DateFormatter()
            df.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            lastUpdate = df.date(from: ts) ?? ISO8601DateFormatter().date(from: ts)
        }
        self.viewModel.upsertSession(id: id, source: source,
                                     task: task, detail: displayDetail ?? detail,
                                     preview: preview, cwd: cwd,
                                     transcriptPath: transcriptPath,
                                     terminal: terminal,
                                     lastUpdate: lastUpdate,
                                     isRunning: running,
                                     isScanManaged: true,
                                     pid: pidValue,
                                     touchTimestamp: false)
    }

    // Notch body size for a given state. Delegates to NotchLayout so the
    // window frame and the SwiftUI view can never drift apart (that drift
    // was clipping compact text and approval buttons).
    private func notchSize(for state: NotchViewModel.NotchState) -> CGSize {
        CGSize(
            width: NotchLayout.width(for: state),
            height: NotchLayout.height(
                for: state,
                sessionCount: viewModel.recentSessions.count,
                askOptionCount: viewModel.currentAskQuestion?.options.count ?? 0
            )
        )
    }

    private static func displayID(_ screen: NSScreen) -> CGDirectDisplayID? {
        (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?
            .uint32Value
    }

    private func refreshAvailableDisplays() {
        var choices = NSScreen.screens.compactMap { screen -> DisplayChoice? in
            guard let id = Self.displayID(screen) else { return nil }
            var traits: [String] = []
            if CGDisplayIsBuiltin(id) != 0 { traits.append("内建") }
            if screen.safeAreaInsets.top > 0 { traits.append("刘海") }
            let suffix = traits.isEmpty ? "" : "（\(traits.joined(separator: " · "))）"
            return DisplayChoice(id: String(id), label: screen.localizedName + suffix)
        }
        if viewModel.displaySelection.hasPrefix("display:") {
            let selectedID = String(viewModel.displaySelection.dropFirst("display:".count))
            if !choices.contains(where: { $0.id == selectedID }) {
                choices.append(DisplayChoice(id: selectedID, label: "已断开显示器 \(selectedID)"))
            }
        }
        viewModel.updateAvailableDisplays(choices)
    }

    private func automaticScreen(from screens: [NSScreen], fallback: NSScreen) -> NSScreen {
        if let resolvedAutomaticDisplayID,
           let resolved = screens.first(where: { Self.displayID($0) == resolvedAutomaticDisplayID }) {
            return resolved
        }
        let selected = screens.first(where: { $0.safeAreaInsets.top > 0 }) ?? fallback
        resolvedAutomaticDisplayID = Self.displayID(selected)
        return selected
    }

    // Respect a persisted user choice. Automatic mode resolves once and stays
    // stable across compact/expanded state changes; it re-resolves only after
    // the chosen screen is disconnected or the user changes the setting.
    private func preferredScreen() -> NSScreen {
        let screens = NSScreen.screens
        guard let fallback = NSScreen.main ?? screens.first else {
            fatalError("Vibe Island requires an attached display")
        }
        switch viewModel.displaySelection {
        case "main":
            return NSScreen.main ?? fallback
        case "pointer":
            let mouse = NSEvent.mouseLocation
            return screens.first(where: { $0.frame.contains(mouse) }) ?? fallback
        case let selection where selection.hasPrefix("display:"):
            let rawID = String(selection.dropFirst("display:".count))
            if let requestedID = CGDirectDisplayID(rawID),
               let requested = screens.first(where: { Self.displayID($0) == requestedID }) {
                return requested
            }
            return automaticScreen(from: screens, fallback: fallback)
        default:
            return automaticScreen(from: screens, fallback: fallback)
        }
    }

    private func updateNotchFrame() {
        let size = notchSize(for: viewModel.activeState)
        let screen = preferredScreen()
        centerAtTop(notchWindow, size: size, screenRect: screen.frame)
        // While an approval/ask prompt is up, take key focus so the bound
        // shortcuts (⌘Y/⌘N/⌘1-9/Esc) fire. Non-activating panel, so the
        // terminal keeps app activation. No explicit resign — clicking any
        // other window transfers key naturally.
        if viewModel.currentRequest != nil, notchWindow.isKeyWindow == false {
            notchWindow.makeKey()
        }
    }

    // Center horizontally at the very top of the screen.
    private func centerAtTop(_ window: NSWindow, size: CGSize, screenRect: NSRect) {
        let x = screenRect.midX - size.width / 2
        let y = screenRect.maxY - size.height   // top edge aligned with screen top
        window.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }
}
