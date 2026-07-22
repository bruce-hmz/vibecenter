import SwiftUI
import AppKit
import Network
import Combine
import CoreText
import Darwin

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
        switch provider.lowercased() {
        case "z.ai", "zai", "zhipu", "bigmodel": name = "zai"
        case "claude", "anthropic":              name = "claude"
        case "codex", "openai":                  name = "codex"
        case "gemini":                           name = "gemini"
        case "kimi":                             name = "kimi"
        default:                                 name = provider.lowercased()
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
    static func jump(toSessionID sessionID: String) {
        // ZCode sessions use "sess_xxx" ids with no pid — activate ZCode.app directly.
        if sessionID.hasPrefix("sess_") || sessionID.hasPrefix("zcode") {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            task.arguments = ["-a", "/Applications/ZCode.app"]
            try? task.run()
            NSLog("JumpToApp: open -a ZCode.app for session \(sessionID)")
            return
        }
        // claude/codex sessions: "claude-16762" → extract trailing pid.
        let parts = sessionID.split(separator: "-")
        guard let last = parts.last, let pid = Int32(last) else { return }
        jump(toPID: pid)
    }

    // Walk parent processes until we find a .app, then activate it.
    // Uses `ps` (user-space, not SIP-restricted like proc_pidpath) to read
    // the command + ppid of each process in the chain.
    static func jump(toPID pid: Int32) {
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

        if let path = appPath, FileManager.default.fileExists(atPath: path) {
            // Use `open -a` shell command — the most reliable way to bring
            // an app to foreground on macOS 26, bypassing accessory-app
            // activation restrictions that block NSRunningApplication.activate()
            // and AppleScript from background processes.
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            task.arguments = ["-a", path]
            try? task.run()
            NSLog("JumpToApp: open -a \(path) for session pid \(pid)")
        } else {
            NSLog("JumpToApp: no .app found for pid \(pid) (path=\(appPath ?? "nil"))")
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
    var detail: String?     // file path / command snippet (kept for compatibility)
    var terminal: String?   // host terminal: "warp" | "iterm" | "terminal" | "zcode" ...
    var lastUpdate: Date    // for ordering by recency + relative time display
    var isRunning: Bool     // true while agent is actively generating/executing

    // Badge color by source — matches real app's per-agent coloring.
    var badgeColor: Color {
        switch source.lowercased() {
        case "claude": return Color(red: 214/255, green: 140/255, blue: 89/255)   // warm orange
        case "zcode":  return Color(red: 96/255,  green: 165/255, blue: 250/255)  // blue
        case "codex":  return Color(red: 34/255,  green: 211/255, blue: 238/255)  // cyan
        case "gemini": return Color(red: 139/255, green: 92/255,  blue: 246/255)  // purple
        default:       return Color(red: 156/255, green: 163/255, blue: 175/255)
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
}

// MARK: - View Model
class NotchViewModel: ObservableObject {
    // Agent-requested state from IPC. nil = no pending request (notch free).
    @Published var requestState: NotchState? = nil
    // User interaction state.
    @Published var isHovered: Bool = false
    @Published var isPinned: Bool = false   // manualExpanded: click to lock open
    // Display data for the current approval/ask request.
    @Published var agentName: String = "Claude Code"
    @Published var taskName: String = "Command Execution"
    @Published var targetFile: String = "npm run build"
    @Published var compactTaskName: String = "Idle"
    // Live agent sessions (multi-agent list in overview).
    @Published var sessions: [String: AgentSession] = [:]
    // Per-provider usage snapshots (supports multi-platform rotation).
    // Empty = no usage daemon running yet.
    @Published var providers: [UsageSnapshot] = []
    // Currently-shown provider index (rotated by timer / tap).
    @Published var currentProviderIndex: Int = 0

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

    // Sessions sorted by most-recently-updated first (for the list).
    var recentSessions: [AgentSession] {
        sessions.values.sorted { $0.lastUpdate > $1.lastUpdate }
    }

    // The session currently driving the compact display: only one that's
    // actively running (isRunning=true, set by live hook events).
    var runningSession: AgentSession? {
        recentSessions.first { $0.isRunning }
    }

    // Compact label: only show content when an agent is actually running.
    // Shows the latest activity (preview), not the session title.
    var compactLabel: String {
        if let active = runningSession {
            // Prefer the latest preview (what's happening NOW), fall back
            // to the task title if no preview exists.
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
                       terminal: String? = nil, lastUpdate: Date? = nil,
                       isRunning: Bool? = nil) {
        guard !id.isEmpty else { return }
        var s = sessions[id] ?? AgentSession(id: id, source: source, task: task,
                                             preview: preview, detail: detail,
                                             terminal: terminal,
                                             lastUpdate: lastUpdate ?? Date(),
                                             isRunning: isRunning ?? false)
        s.source = source
        s.task = task
        if let detail = detail, !detail.isEmpty { s.detail = detail }
        if let preview = preview, !preview.isEmpty { s.preview = preview }
        if let terminal = terminal, !terminal.isEmpty { s.terminal = terminal }
        if let ts = lastUpdate { s.lastUpdate = ts }
        if let running = isRunning { s.isRunning = running }
        sessions[id] = s
    }

    func removeSession(id: String) {
        sessions.removeValue(forKey: id)
    }

    // Priority resolution (mirrors real app intents):
    // pending request (approval/ask) > pinned/hovered (expand) > compact.
    var activeState: NotchState {
        if let req = requestState { return req }
        if isPinned || isHovered { return .overview }
        return .compact
    }

    var onResponse: ((String) -> Void)?
    private var collapseWorkItem: DispatchWorkItem?

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
        guard requestState == nil else { return }
        isPinned.toggle()
    }

    func respond(action: String) {
        onResponse?("{\"action\": \"\(action)\"}")
        requestState = nil
        isPinned = false
    }
}

// MARK: - Local Server (IPC)
class LocalServer {
    let listener: NWListener
    let viewModel: NotchViewModel
    var activeConnection: NWConnection?

    init(viewModel: NotchViewModel, port: UInt16) {
        self.viewModel = viewModel
        self.listener = try! NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: port)!)
    }

    func start() {
        listener.newConnectionHandler = { [weak self] connection in
            self?.handleConnection(connection)
        }
        listener.start(queue: .global())
        print("Vibe Island IPC listening on port \(listener.port?.rawValue ?? 0)")
    }

    private func handleConnection(_ connection: NWConnection) {
        activeConnection = connection
        connection.start(queue: .global())
        
        viewModel.onResponse = { [weak self] response in
            let data = (response + "\n").data(using: .utf8)!
            self?.activeConnection?.send(content: data, completion: .contentProcessed({ _ in
                // Keep connection open or close depending on protocol, here we close after single response
            }))
        }
        
        receiveData(on: connection)
    }

    private func receiveData(on connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, context, isComplete, error in
            if let data = data, let message = String(data: data, encoding: .utf8) {
                self?.processMessage(message)
            }
            if isComplete || error != nil {
                connection.cancel()
            } else {
                self?.receiveData(on: connection)
            }
        }
    }

    private func processMessage(_ message: String) {
        guard let data = message.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
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
                    plan:     u["plan"] as? String
                )
                DispatchQueue.main.async {
                    self.viewModel.upsertProvider(snap)
                    // If current index is out of range (provider removed), reset.
                    if self.viewModel.currentProviderIndex >= self.viewModel.providers.count {
                        self.viewModel.currentProviderIndex = 0
                    }
                }
            }
            return
        }

        DispatchQueue.main.async {
            // Session lifecycle events (sent by relay.py with a distinct
            // "session" action field).
            if let action = json["session"] as? String {
                let id = (json["session_id"] as? String) ?? ""
                switch action {
                case "end":
                    self.viewModel.removeSession(id: id)
                default:  // "start" | "update"
                    let source = (json["source"] as? String) ?? "claude"
                    let task = (json["task"] as? String) ?? ""
                    let detail = json["detail"] as? String
                    let running = json["running"] as? Bool
                    self.viewModel.upsertSession(id: id, source: source,
                                                 task: task, detail: detail,
                                                 isRunning: running)
                }
                return
            }

            if let state = json["state"] as? String {
                switch state {
                case "approval": self.viewModel.requestState = .approval
                case "ask":      self.viewModel.requestState = .ask
                default:         self.viewModel.requestState = nil  // "compact" = dismiss
                }
            }
            if let agent = json["agent"] as? String { self.viewModel.agentName = agent }
            if let task = json["task"] as? String {
                self.viewModel.taskName = task
                self.viewModel.compactTaskName = task
            }
            if let targetFile = json["targetFile"] as? String { self.viewModel.targetFile = targetFile }
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

    init?(viewModel: NotchViewModel, socketPath: String) {
        self.viewModel = viewModel
        self.socketPath = socketPath

        // Create parent directory.
        let dir = (socketPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir,
                                                 withIntermediateDirectories: true)
        // Remove stale socket file from a previous run.
        unlink(socketPath)

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
        withUnsafeMutablePointer(to: &addr.sun_path) {
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
        NSLog("UnixSocket RX: \(line.prefix(200))")
        guard let data = line.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            NSLog("UnixSocket RX parse failed: \(line.prefix(100))")
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
                                             isRunning: false)
                self.viewModel.compactTaskName = "\(source): start"
            case "UserPromptSubmit":
                let snippet = String(prompt.prefix(50))
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: snippet.isEmpty ? "Thinking…" : snippet,
                                             detail: shortCwd, isRunning: true)
                self.viewModel.compactTaskName = snippet.isEmpty ? "Thinking…" : snippet
            case "PreToolUse":
                let (task, detail) = Self.describe(tool: toolName, input: toolInput)
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: task, detail: detail, isRunning: true)
                self.viewModel.compactTaskName = task
                // Write tools → trigger approval card (like relay.py does).
                if ["Bash", "Edit", "Write", "NotebookEdit"].contains(toolName) {
                    self.viewModel.targetFile = detail ?? ""
                    self.viewModel.taskName = task
                    self.viewModel.requestState = .approval
                }
            case "PostToolUse":
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: "Done: \(toolName)", detail: shortCwd,
                                             isRunning: true)
                self.viewModel.compactTaskName = "Done: \(toolName)"
            case "Stop":
                let preview = String(responseText.prefix(40))
                self.viewModel.upsertSession(id: sessionId, source: source,
                                             task: preview.isEmpty ? "Idle" : preview,
                                             detail: shortCwd, isRunning: false)
                self.viewModel.compactTaskName = "Idle"
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
    static let bg = Color.black
    static let cardBg = Color(red: 22/255, green: 23/255, blue: 29/255)
    static let codeBg = Color(red: 31/255, green: 32/255, blue: 40/255)
    static let buttonGray = Color(red: 46/255, green: 48/255, blue: 58/255)
    static let textMain = Color.white
    static let textMuted = Color(red: 156/255, green: 163/255, blue: 175/255)
    static let green = Color(red: 74/255, green: 222/255, blue: 128/255)
    static let blue = Color(red: 96/255, green: 165/255, blue: 250/255)
    static let yellow = Color(red: 250/255, green: 204/255, blue: 21/255)
    static let cyan = Color(red: 34/255, green: 211/255, blue: 238/255)
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
    var color: Color = Color(red: 74/255, green: 222/255, blue: 128/255)
    var barCount: Int = 4
    var maxHeight: CGFloat = 14

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 2) {
                ForEach(0..<barCount, id: \.self) { i in
                    let phase = Double(i) * 0.6
                    // each bar oscillates between 25% and 100% of maxHeight
                    let wave = (sin(t * 4 + phase) + 1) / 2  // 0...1
                    let h = maxHeight * (0.25 + 0.75 * wave)
                    Capsule()
                        .fill(color)
                        .frame(width: 2.5, height: max(2, h))
                        .animation(.linear(duration: 0.05), value: h)
                }
            }
        }
        .frame(height: maxHeight)
    }
}

// MARK: - Notch View
struct NotchView: View {
    @ObservedObject var viewModel: NotchViewModel

    var body: some View {
        ZStack(alignment: .top) {
            // Top corners stay square (flush with screen top edge, like a
            // menu-bar dropdown pulled down); only bottom corners round.
            // This matches the real app's "pulled-down panel" silhouette.
            UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: cornerRadius,
                                   bottomTrailingRadius: cornerRadius, topTrailingRadius: 0,
                                   style: .continuous)
                .fill(Theme.bg)
                .frame(width: notchWidth, height: notchHeight)
                // Subtle inner border instead of a drop shadow: .shadow on
                // UnevenRoundedRectangle leaks the bounding-box corners
                // (visible尖角 outside the rounded bottom), so we avoid it.
                .overlay(
                    UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: cornerRadius,
                                           bottomTrailingRadius: cornerRadius, topTrailingRadius: 0,
                                           style: .continuous)
                        .stroke(Color.white.opacity(0.06), lineWidth: 0.5)
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
                case .approval: ApprovalView(viewModel: viewModel).frame(width: notchWidth, height: notchHeight)
                case .ask: AskView(viewModel: viewModel).frame(width: notchWidth, height: notchHeight)
                }
            }
            .transition(.asymmetric(insertion: .opacity.animation(.easeInOut(duration: 0.2).delay(0.1)), removal: .opacity.animation(.easeOut(duration: 0.1))))
        }
        // No outer tap gesture here — it would steal clicks from AgentRows
        // and approval buttons. Pin/unpin is driven by hover instead:
        // hover = expanded, leave = collapse (300ms grace).
        // Clicks pass through to child views (AgentRow jump, buttons).
        // Snappy spring with a touch of overshoot — matches the real
        // app's "expand decisively, settle gently" feel.
        .animation(.spring(response: 0.42, dampingFraction: 0.78, blendDuration: 0.1),
                   value: viewModel.activeState)
        // Native AppKit hover tracking: reliable across dynamic size changes,
        // where SwiftUI .onHover drops events during layout rebuilds.
        .background(HoverTrackingArea(onEnter: { viewModel.setHovered(true) },
                                      onExit:  { viewModel.setHovered(false) }))
    }

    var notchWidth: CGFloat {
        switch viewModel.activeState {
        case .compact: return 135
        case .overview: return 600
        case .approval: return 380
        case .ask: return 340
        }
    }

    var notchHeight: CGFloat {
        switch viewModel.activeState {
        case .compact: return 30
        case .overview: return 280
        case .approval: return 240
        case .ask: return 200
        }
    }
    var cornerRadius: CGFloat { viewModel.activeState == .compact ? 19 : 36 }
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
        HStack(spacing: 8) {
            if vm.compactIsActive {
                // Active: animated pulse bars (agent is working).
                PulseBars(color: Theme.green, maxHeight: 12)
                    .padding(.leading, 2)
            } else {
                // Idle: static pixel pet (low visual energy).
                PixelPet(color: Theme.green).offset(y: 1)
            }
            Text(vm.compactLabel)
                .font(NotchFont.mono(12))
                .foregroundColor(Theme.green)
                .lineLimit(1)
        }
    }
}

struct OverviewView: View {
    @ObservedObject var vm: NotchViewModel
    @State private var isMuted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Top bar: usage (left) + toolbar icons (right).
            HStack(alignment: .top, spacing: 8) {
                // Usage panel — left side. Shows the currently-selected
                // provider with its name label, rotates between providers.
                UsageRotator(vm: vm)
                Spacer(minLength: 4)
                // Toolbar: mute + settings (right side).
                HStack(spacing: 10) {
                    Button(action: { isMuted.toggle() }) {
                        Image(systemName: isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                            .font(.system(size: 13)).foregroundColor(Theme.textMuted)
                    }.buttonStyle(PlainButtonStyle())
                    Image(systemName: "gearshape.fill")
                        .font(.system(size: 13)).foregroundColor(Theme.textMuted)
                }.padding(.top, 16).padding(.trailing, 20)
            }.padding(.leading, 20).padding(.top, 2)

            // Agent session list (below) — one row per live session.
            VStack(spacing: 8) {
                if vm.recentSessions.isEmpty {
                    // Fallback when no hook events have arrived yet.
                    AgentRow(session: AgentSession(
                        id: "fallback", source: "claude",
                        task: vm.compactTaskName.isEmpty ? "No active session" : vm.compactTaskName,
                        detail: vm.targetFile.isEmpty ? nil : vm.targetFile,
                        lastUpdate: Date(), isRunning: false))
                } else {
                    ForEach(Array(vm.recentSessions.prefix(3).enumerated()), id: \.element.id) { _, s in
                        AgentRow(session: s)
                    }
                }
            }.padding(.horizontal, 14).padding(.top, 14)
            Spacer(minLength: 0)
        }
    }
}

// Usage panel that rotates between providers every 3s, with the provider
// name shown as a label. Tap to manually advance. When only one provider
// exists, behaves like a static panel (no rotation, no dot indicators).
struct UsageRotator: View {
    @ObservedObject var vm: NotchViewModel

    var body: some View {
        let providers = vm.providers
        Group {
            if providers.isEmpty {
                Text("no usage data")
                    .font(NotchFont.mono(11))
                    .foregroundColor(Theme.textMuted)
                    .padding(.top, 18)
            } else {
                // Single row: [logo] 5h X% Rm | 7d X% RdRd | mo X% RmRd
                HStack(spacing: 8) {
                    // Provider logo (clickable to advance to next provider).
                    Group {
                        if let img = ProviderLogo.image(for: vm.currentUsage?.provider ?? "",
                                                        size: 16) {
                            img.resizable().scaledToFit().frame(width: 16, height: 16)
                        } else {
                            // Fallback: text initials.
                            Text(String(vm.currentUsage?.provider.prefix(1) ?? "—"))
                                .font(NotchFont.mono(11, .semibold))
                                .foregroundColor(Theme.textMain)
                                .frame(width: 16, height: 16)
                        }
                    }
                    .onTapGesture { advance(count: providers.count) }

                    if providers.count > 1 {
                        // Pagination dots when multiple providers.
                        HStack(spacing: 3) {
                            ForEach(0..<providers.count, id: \.self) { i in
                                Circle()
                                    .fill(i == vm.currentProviderIndex ? Theme.blue : Color.white.opacity(0.2))
                                    .frame(width: 3, height: 3)
                            }
                        }
                    }

                    if let usage = vm.currentUsage {
                        HStack(spacing: 10) {
                            windowCol(label: "5h",
                                      pct: usage.fiveHour,
                                      reset: usage.fiveHourReset,
                                      color: usage.color(for: .fiveHour))
                            Divider().frame(height: 12).background(Color.white.opacity(0.12))
                            windowCol(label: "7d",
                                      pct: usage.sevenDay,
                                      reset: usage.sevenDayReset,
                                      color: usage.color(for: .sevenDay))
                            if let mo = usage.monthly {
                                Divider().frame(height: 12).background(Color.white.opacity(0.12))
                                windowCol(label: "mo",
                                          pct: mo,
                                          reset: usage.monthlyReset,
                                          color: usage.color(for: .monthly))
                            }
                        }
                    }
                }
                .padding(.top, 18)
                .id(vm.currentProviderIndex)
                .animation(.easeInOut(duration: 0.25), value: vm.currentProviderIndex)
                // Auto-rotate only when multiple providers exist.
                .onReceive(Timer.publish(every: 3, on: .main, in: .common).autoconnect()) { _ in
                    if providers.count > 1 {
                        advance(count: providers.count)
                    }
                }
            }
        }
    }

    private func advance(count: Int) {
        guard count > 0 else { return }
        vm.currentProviderIndex = (vm.currentProviderIndex + 1) % count
    }

    private func windowCol(label: String, pct: Int?, reset: String?, color: Color) -> some View {
        HStack(spacing: 3) {
            Text(label)
                .font(NotchFont.mono(11, .medium))
                .foregroundColor(Theme.textMuted)
            if let pct = pct {
                Text("\(pct)%")
                    .font(NotchFont.mono(11, .semibold))
                    .foregroundColor(color)
            }
            if let reset = reset {
                Text(reset)
                    .font(NotchFont.mono(10))
                    .foregroundColor(Theme.textMuted)
            }
        }
    }
}

// One agent session row. The source badge on the right is colored per
// agent (claude=orange, zcode=blue, codex=cyan, ...).
struct AgentRow: View {
    let session: AgentSession

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // Status dot.
            Circle().fill(session.badgeColor).frame(width: 7, height: 7).padding(.top, 5)

            VStack(alignment: .leading, spacing: 3) {
                // Line 1: title (latest prompt) + badges + time on the right.
                HStack(spacing: 6) {
                    Text(session.task.isEmpty ? "Idle" : session.task)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(Theme.textMain)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    // Source badge.
                    badge(session.source.uppercased(), color: session.badgeColor)
                    // Terminal badge (warp / iterm / ...).
                    if let term = session.terminal {
                        badge(term.uppercased(), color: Theme.textMuted)
                    }
                    // Relative time.
                    Text(session.relativeTime)
                        .font(NotchFont.mono(9))
                        .foregroundColor(Theme.textMuted)
                }
                // Line 2: latest message preview (assistant reply / tool call).
                if let preview = session.preview, !preview.isEmpty {
                    Text(preview)
                        .font(NotchFont.mono(10))
                        .foregroundColor(Theme.textMuted)
                        .lineLimit(1)
                }
            }
        }
        .padding(11)
        .background(Theme.cardBg)
        .cornerRadius(13)
        .contentShape(RoundedRectangle(cornerRadius: 13, style: .continuous))
        .onTapGesture {
            JumpToApp.jump(toSessionID: session.id)
        }
    }

    private func badge(_ text: String, color: Color) -> some View {
        Text(text)
            .font(NotchFont.mono(8, .bold))
            .foregroundColor(color)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(color.opacity(0.15)).cornerRadius(3)
    }
}

struct ApprovalView: View {
    @ObservedObject var viewModel: NotchViewModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) { Circle().fill(Theme.yellow).frame(width: 8, height: 8); Text("\(viewModel.agentName) Request").font(.system(size: 14, weight: .bold)).foregroundColor(Theme.textMain) }.padding(.horizontal, 18).padding(.top, 18)
            HStack(spacing: 6) { Image(systemName: "exclamationmark.triangle.fill").foregroundColor(Theme.yellow).font(.system(size: 11)); Text(viewModel.taskName).foregroundColor(Theme.textMuted); Text(viewModel.targetFile).foregroundColor(Theme.textMain).lineLimit(1) }.font(NotchFont.mono(12, .medium)).padding(.horizontal, 18)

            VStack(alignment: .leading, spacing: 4) {
                Text("12  const verify = (token) =>").foregroundColor(Theme.textMuted)
                HStack(spacing: 0) { Text("13 -").foregroundColor(.red).frame(width: 30, alignment: .leading); Text("jwt.verify(token);").foregroundColor(.red); Spacer() }.background(Color.red.opacity(0.15))
                HStack(spacing: 0) { Text("13 +").foregroundColor(.green).frame(width: 30, alignment: .leading); Text("if (!token) throw new Error();").foregroundColor(.green); Spacer() }.background(Color.green.opacity(0.15))
            }.font(NotchFont.mono(11, .regular)).padding(10).background(Theme.codeBg).cornerRadius(10).padding(.horizontal, 16)

            HStack(spacing: 12) {
                Button(action: { NSSound(named: NSSound.Name("Pop"))?.play(); viewModel.respond(action: "deny") }) { Text("Deny  ⌘N").frame(maxWidth: .infinity) }.buttonStyle(NotchBtnStyle(bg: Theme.buttonGray, fg: Theme.textMain))
                Button(action: { NSSound(named: NSSound.Name("Pop"))?.play(); viewModel.respond(action: "allow") }) { Text("Allow  ⌘Y").frame(maxWidth: .infinity) }.buttonStyle(NotchBtnStyle(bg: Theme.textMain, fg: Theme.bg))
            }.padding(.horizontal, 16)
        }
    }
}

struct AskView: View {
    @ObservedObject var viewModel: NotchViewModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) { Image(systemName: "bubble.left.fill").foregroundColor(Theme.cyan).font(.system(size: 13)); Text("\(viewModel.agentName) asks").font(.system(size: 14, weight: .bold)).foregroundColor(Theme.textMain) }.padding(.horizontal, 18).padding(.top, 18)
            Text(viewModel.taskName).font(.system(size: 13, weight: .medium)).foregroundColor(Theme.textMain).padding(.horizontal, 18)
            VStack(spacing: 8) {
                AskOption(key: "⌘1", text: "Production", action: { NSSound(named: NSSound.Name("Pop"))?.play(); viewModel.respond(action: "Production") })
                AskOption(key: "⌘2", text: "Staging", action: { NSSound(named: NSSound.Name("Pop"))?.play(); viewModel.respond(action: "Staging") })
            }.padding(.horizontal, 16)
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
            HStack(spacing: 10) {
                Text(key).font(NotchFont.mono(11, .bold)).foregroundColor(Theme.cyan).padding(.horizontal, 6).padding(.vertical, 3).background(Theme.cyan.opacity(0.15)).cornerRadius(6)
                Text(text).font(.system(size: 13, weight: .medium)).foregroundColor(Theme.textMain); Spacer()
            }.padding(10).background(isHovered ? Theme.buttonGray : Theme.cardBg).cornerRadius(11)
        }.buttonStyle(PlainButtonStyle()).onHover { h in isHovered = h }
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
            Text("Vibe Island Controls").font(.system(size: 16, weight: .semibold))
            HStack(spacing: 12) {
                CtrlBtn(title: "Dismiss", icon: "minus", color: Theme.green) { viewModel.requestState = nil; viewModel.isPinned = false }
                CtrlBtn(title: "Overview", icon: "square.grid.2x2", color: .white) { viewModel.isPinned = true }
            }
            HStack(spacing: 12) {
                CtrlBtn(title: "Approval", icon: "checkmark.shield", color: Theme.yellow) { viewModel.requestState = .approval }
                CtrlBtn(title: "Ask", icon: "questionmark.bubble", color: Theme.cyan) { viewModel.requestState = .ask }
            }
        }.padding(24).frame(width: 320, height: 210).background(VisualEffectView(material: .hudWindow, blendingMode: .behindWindow))
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

// MARK: - Entry
@main struct VibeIslandApp: App { @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate; var body: some Scene { Settings { EmptyView() } } }

class AppDelegate: NSObject, NSApplicationDelegate {
    var notchWindow: NSWindow!; var controlWindow: NSWindow!; let viewModel = NotchViewModel(); let controlDelegate = ControllerWindowDelegate(); var server: LocalServer!
    var unixServer: UnixSocketServer?
    var scanTimer: Timer?
    // Real-time file watchers for agent rollout/transcript files.
    // When an agent writes to its file (actively working), we update
    // running=true immediately — no 10s poll delay.
    private var fileWatchers: [String: FileWatcher] = [:]
    private var runningTimeouts: [String: Timer] = [:]  // per-session timeout
    private var stateCancellable: AnyCancellable?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Accessory (background) policy: no Dock icon, no stealing focus from
        // other apps. The notch is an overlay, not a regular app.
        NSApp.setActivationPolicy(.accessory)

        // Load Departure Mono (bundled next to the executable).
        NotchFont.registerIfNeeded()

        let screen = preferredScreen(); let screenRect = screen.frame

        // Window sized to the notch body only (NOT full screen width), so it
        // doesn't cover the menu bar / other apps' window controls. Position
        // is top-centered; size tracks the active notch state.
        let initialSize = notchSize(for: viewModel.activeState)
        notchWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: initialSize.width, height: initialSize.height), styleMask: [.borderless], backing: .buffered, defer: false)
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
        notchWindow.makeKeyAndOrderFront(nil)

        // Keep the window frame pinned to the notch body as the state
        // changes (compact ↔ overview ↔ approval). This is what prevents
        // the window from covering other apps' controls when collapsed.
        stateCancellable = viewModel.objectWillChange.sink { [weak self] in
            DispatchQueue.main.async { self?.updateNotchFrame() }
        }

        // (Control panel window removed — it was for early testing and its
        // close button would terminate the whole app. The notch is now
        // fully driven by hover + IPC.)

        server = LocalServer(viewModel: viewModel, port: 14321); server.start()

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

        // Re-scan every 10s so running state stays responsive (detects
        // transcript/rollout file mtime changes = agent actively working).
        scanTimer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            self?.scanRunningAgents()
        }

        // Re-pin the notch when the display layout changes (plug/unplug
        // external monitor, resolution change).
        NotificationCenter.default.addObserver(
            self, selector: #selector(handleScreenChange),
            name: NSApplication.didChangeScreenParametersNotification, object: nil
        )
    }

    @objc private func handleScreenChange() {
        DispatchQueue.main.async { self.updateNotchFrame() }
    }

    // Set up real-time file watchers for each session's data file.
    // Called after each process scan. Only creates new watchers (idempotent).
    private func setupFileWatchers() {
        for (id, sess) in viewModel.sessions {
            guard fileWatchers[id] == nil else { continue }
            // Determine the file to watch for this session.
            let watchPath: String?
            if sess.source == "zcode" {
                // ZCode rollout file (real-time model I/O).
                watchPath = NSHomeDirectory() + "/.zcode/cli/rollout/model-io-\(id).jsonl"
            } else {
                // Claude: most recently modified transcript under the
                // project dir matching this session's cwd.
                let encoded = (sess.detail ?? "").replacingOccurrences(of: "/", with: "-")
                let projDir = NSHomeDirectory() + "/.claude/projects/-" + encoded
                // Just watch the dir's newest file; for simplicity use the
                // detail (short cwd) to find it.
                watchPath = nil  // claude uses hook events (relay.py) for live updates
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
            if source == "zcode" {
                preview = self.readZCodeRolloutPreview(sessionID: sessionID)
            } else {
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
            }) else { return }

            let task = Process()
            task.executableURL = scriptURL
            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = FileHandle()  // discard
            do {
                try task.run()
            } catch {
                NSLog("scan-agents.sh failed: \(error)")
                return
            }
            task.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            guard let text = String(data: data, encoding: .utf8) else { return }

            // Collect the IDs from this scan pass.
            var seenIDs = Set<String>()
            for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
                if let id = self.parseScanLine(String(line)) {
                    seenIDs.insert(id)
                }
            }
            NSLog("scan-agents: discovered \(seenIDs.count) session(s)")

            // Reconcile on the main thread: upsert scanned sessions, then
            // remove any session whose ID wasn't in this scan AND wasn't
            // added by a live hook event (those have a "live" marker we
            // preserve so active conversations don't disappear mid-turn).
            DispatchQueue.main.async {
                for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
                    self.applyScanLine(String(line))
                }
                // Remove sessions no longer present in the process scan.
                for (id, _) in self.viewModel.sessions {
                    if !seenIDs.contains(id) {
                        self.viewModel.removeSession(id: id)
                        self.removeFileWatcher(for: id)
                    }
                }
                // Set up real-time file watchers for the currently active
                // agent's data files (rollout for zcode, transcript for claude).
                self.setupFileWatchers()
            }
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
        let preview = json["preview"] as? String
        let terminal = json["terminal"] as? String
        let lastTs = json["last_ts"] as? String
        let running = json["running"] as? Bool
        // Parse ISO 8601 timestamp so relative-time shows the real last
        // activity, not the rescan time.
        var lastUpdate: Date? = nil
        if let ts = lastTs, !ts.isEmpty {
            let df = ISO8601DateFormatter()
            df.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            lastUpdate = df.date(from: ts) ?? ISO8601DateFormatter().date(from: ts)
        }
        DispatchQueue.main.async {
            self.viewModel.upsertSession(id: id, source: source,
                                         task: task, detail: detail,
                                         preview: preview, terminal: terminal,
                                         lastUpdate: lastUpdate,
                                         isRunning: running)
        }
    }

    // Notch body size for a given state. Must match NotchView's
    // notchWidth/notchHeight so the window exactly covers the rendered notch.
    private func notchSize(for state: NotchViewModel.NotchState) -> CGSize {
        switch state {
        case .compact:  return CGSize(width: 135, height: 30)
        case .overview: return CGSize(width: 600, height: 280)
        case .approval: return CGSize(width: 380, height: 240)
        case .ask:      return CGSize(width: 340, height: 200)
        }
    }

    // Choose the screen the notch should pin to. Prefer the external
    // display (main screen with the largest pixel area); fall back to
    // NSScreen.main if there's only one screen. NSScreen.main alone is
    // unreliable because it tracks keyboard focus and drifts between
    // displays as you type in different windows.
    private func preferredScreen() -> NSScreen {
        let screens = NSScreen.screens
        if screens.count > 1 {
            // Pick the screen with the largest frame — typically the
            // external display, which is larger than the MacBook's panel.
            if let biggest = screens.max(by: {
                ($0.frame.width * $0.frame.height) < ($1.frame.width * $1.frame.height)
            }) {
                return biggest
            }
        }
        return NSScreen.main ?? screens[0]
    }

    private func updateNotchFrame() {
        let size = notchSize(for: viewModel.activeState)
        let screen = preferredScreen()
        centerAtTop(notchWindow, size: size, screenRect: screen.frame)
    }

    // Center horizontally at the very top of the screen.
    private func centerAtTop(_ window: NSWindow, size: CGSize, screenRect: NSRect) {
        let x = screenRect.midX - size.width / 2
        let y = screenRect.maxY - size.height   // top edge aligned with screen top
        window.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }
}
