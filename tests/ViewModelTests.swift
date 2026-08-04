import Foundation

struct TestFailure: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

@discardableResult
func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws -> Bool {
    if !condition() {
        throw TestFailure(message: message)
    }
    return true
}

func makeDefaultsSuite() -> UserDefaults {
    let suiteName = "vibe-island.tests.\(UUID().uuidString)"
    guard let defaults = UserDefaults(suiteName: suiteName) else {
        fatalError("Failed to create UserDefaults suite \(suiteName)")
    }
    defaults.removePersistentDomain(forName: suiteName)
    return defaults
}

func drainMainRunLoop(until deadline: Date) {
    while Date() < deadline {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.01))
    }
}

func waitUntil(timeout: TimeInterval = 1.0,
               pollInterval: TimeInterval = 0.01,
               condition: @escaping () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if condition() {
            return true
        }
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(pollInterval))
    }
    return condition()
}

func makeApprovalRequest(id: String = UUID().uuidString,
                         sessionID: String = "approval-session",
                         source: String = "claude",
                         taskName: String = "Review patch",
                         targetFile: String = "VibeIsland.swift",
                         toolName: String? = "Edit",
                         command: String? = nil,
                         cwd: String? = "/tmp",
                         diff: String? = nil,
                         expiresAt: Date) -> PendingRequest {
    PendingRequest(
        id: id,
        kind: .approval,
        sessionID: sessionID,
        source: source,
        agentName: "Claude",
        taskName: taskName,
        targetFile: targetFile,
        toolName: toolName,
        command: command,
        cwd: cwd,
        reason: nil,
        diff: diff,
        questions: [],
        arrivedAt: Date(),
        expiresAt: expiresAt
    )
}

func makeAskRequest(id: String = UUID().uuidString,
                    sessionID: String = "ask-session",
                    expiresAt: Date) -> PendingRequest {
    PendingRequest(
        id: id,
        kind: .ask,
        sessionID: sessionID,
        source: "codex",
        agentName: "Codex",
        taskName: "Need answer",
        targetFile: "",
        toolName: nil,
        command: nil,
        cwd: "/tmp",
        reason: nil,
        diff: nil,
        questions: [
            AskQuestionDraft(
                id: "question-1",
                header: "Mode",
                question: "Pick one",
                options: [AskOptionChoice(id: "a", label: "A", description: nil)],
                multiSelect: false
            )
        ],
        arrivedAt: Date(),
        expiresAt: expiresAt
    )
}

func storedDefaultsDataStrings(_ defaults: UserDefaults) -> [String] {
    defaults.dictionaryRepresentation().values.compactMap { value in
        guard let data = value as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

func testLiveUpdatedScannerSessionSurvivesEmptyReconcile() throws {
    let viewModel = NotchViewModel(defaults: makeDefaultsSuite())
    viewModel.upsertSession(
        id: "scanner-live",
        source: "claude",
        task: "scan",
        isRunning: false,
        isScanManaged: true
    )
    viewModel.upsertSession(
        id: "scanner-live",
        source: "claude",
        task: "live update",
        preview: "working",
        isRunning: true,
        isLiveUpdate: true
    )

    let removed = viewModel.reconcileScannedSessions(seenIDs: [])
    let session = viewModel.sessions["scanner-live"]

    try expect(removed.isEmpty, "live-updated scan session should not be removed by empty reconcile")
    try expect(session != nil, "live-updated session should remain in live sessions")
    try expect(session?.hasLiveUpdates == true, "live-updated session should remember live-updates state")
    try expect(session?.isScanManaged == false, "live-updated session should stop being scan-managed")
}

func testScannerOnlySessionMovesIntoHistoryDuringReconcile() throws {
    let viewModel = NotchViewModel(defaults: makeDefaultsSuite())
    viewModel.upsertSession(
        id: "scanner-only",
        source: "codex",
        task: "background scan",
        isRunning: false,
        isScanManaged: true
    )

    let removed = viewModel.reconcileScannedSessions(seenIDs: [])

    try expect(removed == ["scanner-only"], "scanner-only session should be removed when reconcile cannot see it")
    try expect(viewModel.sessions["scanner-only"] == nil, "scanner-only session should leave live sessions")
    try expect(viewModel.sessionHistory.count == 1, "scanner-only session should move into limited history")
    try expect(viewModel.sessionHistory.first?.id == "scanner-only", "history should contain the removed scanner session")
    try expect(viewModel.sessionHistory.first?.endedAt != nil, "history entry should have an ended timestamp")
}

func testApprovalTimeoutDeniesAndClearsQueue() throws {
    let viewModel = NotchViewModel(defaults: makeDefaultsSuite())
    var decisions: [RequestDecision] = []
    viewModel.onDecision = { decisions.append($0) }

    let request = makeApprovalRequest(id: "approval-timeout", expiresAt: Date().addingTimeInterval(0.05))
    viewModel.enqueueRequest(request)

    let completed = waitUntil(timeout: 1.0) {
        decisions.count == 1 && viewModel.pendingRequests.isEmpty
    }

    try expect(completed, "approval timeout should fire within the timeout window")
    try expect(decisions.count == 1, "approval timeout should emit exactly one decision")
    try expect(decisions[0].requestID == "approval-timeout", "approval timeout should respond to the expired request")
    try expect(decisions[0].payload["action"] as? String == "deny", "approval timeout should deny the request")
    try expect(decisions[0].payload["reason"] as? String == "request_timeout", "approval timeout should report request_timeout")
    try expect(viewModel.pendingRequests.isEmpty, "approval timeout should clear the pending queue")
}

func testAskTimeoutCancelsAndClearsQueue() throws {
    let viewModel = NotchViewModel(defaults: makeDefaultsSuite())
    var decisions: [RequestDecision] = []
    viewModel.onDecision = { decisions.append($0) }

    let request = makeAskRequest(id: "ask-timeout", expiresAt: Date().addingTimeInterval(0.05))
    viewModel.enqueueRequest(request)

    let completed = waitUntil(timeout: 1.0) {
        decisions.count == 1 && viewModel.pendingRequests.isEmpty
    }

    try expect(completed, "ask timeout should fire within the timeout window")
    try expect(decisions.count == 1, "ask timeout should emit exactly one decision")
    try expect(decisions[0].requestID == "ask-timeout", "ask timeout should respond to the expired request")
    try expect(decisions[0].payload["action"] as? String == "cancel", "ask timeout should cancel the request")
    try expect(decisions[0].payload["reason"] as? String == "request_timeout", "ask timeout should report request_timeout")
    try expect(viewModel.pendingRequests.isEmpty, "ask timeout should clear the pending queue")
}

func testCompactOverflowCountUsesRunningAgentsOnly() throws {
    let viewModel = NotchViewModel(defaults: makeDefaultsSuite())
    viewModel.upsertSession(id: "run-1", source: "claude", task: "one", isRunning: true)
    viewModel.upsertSession(id: "run-2", source: "codex", task: "two", isRunning: true)
    viewModel.upsertSession(id: "run-3", source: "gemini", task: "three", isRunning: true)
    viewModel.upsertSession(id: "idle-1", source: "zcode", task: "idle", isRunning: false)

    try expect(viewModel.activeSessionCount == 3, "active session count should only include running sessions")
    try expect(viewModel.compactOverflowCount == 2, "compact overflow should equal running session count minus one")
}

func testIPCAuthenticatorRejectsReplayAndTampering() throws {
    let token = Data((0..<32).map(UInt8.init))
    let authenticator = try IPCAuthenticator(token: token)
    let payload: [String: Any] = [
        "event": "approval",
        "session_id": "sess-1",
        "task": "review"
    ]

    let signed = try authenticator.signedPayload(payload)
    let firstVerification = authenticator.verifyAndConsume(signed)
    let replayVerification = authenticator.verifyAndConsume(signed)

    var tampered = signed
    tampered["task"] = "tampered"
    let tamperedVerification = authenticator.verifyAndConsume(tampered)

    try expect(firstVerification == true, "signed payload should verify once")
    try expect(replayVerification == false, "signed payload replay should be rejected")
    try expect(tamperedVerification == false, "tampered payload should be rejected")
}

func testApprovalRiskAnalyzerMarksGitResetHardAsCritical() throws {
    let request = makeApprovalRequest(
        toolName: "Bash",
        command: "git reset --hard HEAD~1",
        expiresAt: Date().addingTimeInterval(60)
    )

    try expect(
        request.riskAssessment.level == .critical,
        "git reset --hard should be marked critical"
    )
}

func testApprovalRiskAnalyzerMarksWorkspaceExternalEditAsHigh() throws {
    let request = makeApprovalRequest(
        targetFile: "../outside.txt",
        toolName: "Edit",
        cwd: "/tmp/workspace",
        expiresAt: Date().addingTimeInterval(60)
    )

    try expect(
        request.riskAssessment.level == .high,
        "editing outside the workspace should be marked high risk"
    )
}

func testApprovalRiskAnalyzerMarksWorkspaceLocalEditAsMedium() throws {
    let request = makeApprovalRequest(
        targetFile: "notes/todo.txt",
        toolName: "Edit",
        cwd: "/tmp/workspace",
        expiresAt: Date().addingTimeInterval(60)
    )

    try expect(
        request.riskAssessment.level == .medium,
        "editing inside the workspace should be marked medium risk"
    )
}

func testApprovalDecisionHistoryPersistsSanitizedEntries() throws {
    let defaults = makeDefaultsSuite()
    let viewModel = NotchViewModel(defaults: defaults)
    let request = makeApprovalRequest(
        id: "approval-1",
        sessionID: "sensitive-session-123",
        source: "codex",
        taskName: "Sensitive task name",
        targetFile: "/private/tmp/secret.txt",
        toolName: "Bash",
        command: "echo secret",
        cwd: "/private/tmp/workspace",
        diff: "SECRET_DIFF",
        expiresAt: Date().addingTimeInterval(60)
    )

    viewModel.enqueueRequest(request)
    viewModel.respondToRequest(id: request.id, action: "allow")

    try expect(viewModel.approvalDecisionHistory.count == 1, "decision should be recorded once")
    let entry = try viewModel.approvalDecisionHistory.first.unwrap("history entry should exist")
    try expect(entry.provider == "codex", "provider should persist as sanitized provider value")
    try expect(entry.toolCategory == "shell", "tool category should persist as shell")
    try expect(entry.risk == .medium, "shell command should persist the derived risk level")
    try expect(entry.outcome == "allow", "decision outcome should persist")
    try expect(
        entry.decisionSource == "queue_button",
        "decision source should persist as the whitelisted source"
    )

    let reloaded = NotchViewModel(defaults: defaults)
    try expect(reloaded.approvalDecisionHistory == [entry], "history should reload from defaults")

    let persistedStrings = storedDefaultsDataStrings(defaults)
    try expect(!persistedStrings.isEmpty, "history should be encoded into defaults data")
    let persisted = persistedStrings.joined(separator: "\n")
    for forbidden in [
        "command", "diff", "cwd", "path", "task", "session_id",
        "echo secret", "SECRET_DIFF", "/private/tmp/workspace",
        "/private/tmp/secret.txt", "Sensitive task name", "sensitive-session-123"
    ] {
        try expect(
            !persisted.contains(forbidden),
            "persisted history should not contain sensitive field \(forbidden)"
        )
    }
}

func testApprovalDecisionHistoryKeepsOnlyThirtyEntries() throws {
    let defaults = makeDefaultsSuite()
    let viewModel = NotchViewModel(defaults: defaults)

    for index in 0..<35 {
        let request = makeApprovalRequest(
            id: "approval-\(index)",
            targetFile: "file-\(index).txt",
            toolName: "Edit",
            cwd: "/tmp/workspace",
            expiresAt: Date().addingTimeInterval(60)
        )
        viewModel.enqueueRequest(request)
        viewModel.respondToRequest(id: request.id, action: "allow")
    }

    try expect(viewModel.approvalDecisionHistory.count == 30, "history should cap at 30 entries")
    let reloaded = NotchViewModel(defaults: defaults)
    try expect(reloaded.approvalDecisionHistory.count == 30, "reloaded history should keep the 30-entry cap")
}

func testDisablingDecisionHistoryClearsStoredEntries() throws {
    let defaults = makeDefaultsSuite()
    let viewModel = NotchViewModel(defaults: defaults)
    let request = makeApprovalRequest(
        id: "approval-clear",
        targetFile: "inside.txt",
        toolName: "Edit",
        cwd: "/tmp/workspace",
        expiresAt: Date().addingTimeInterval(60)
    )

    viewModel.enqueueRequest(request)
    viewModel.respondToRequest(id: request.id, action: "allow")
    try expect(viewModel.approvalDecisionHistory.count == 1, "history should contain the initial decision")

    viewModel.decisionHistoryEnabled = false

    try expect(viewModel.approvalDecisionHistory.isEmpty, "disabling history should clear in-memory entries")
    let reloaded = NotchViewModel(defaults: defaults)
    try expect(reloaded.approvalDecisionHistory.isEmpty, "disabling history should clear persisted entries")
}

func testAllowAllPendingApprovesOnlyLowAndMediumRiskRequests() throws {
    let viewModel = NotchViewModel(defaults: makeDefaultsSuite())
    var decisions: [RequestDecision] = []
    viewModel.onDecision = { decisions.append($0) }

    let low = makeApprovalRequest(
        id: "low",
        toolName: nil,
        command: nil,
        expiresAt: Date().addingTimeInterval(60)
    )
    let medium = makeApprovalRequest(
        id: "medium",
        targetFile: "inside.txt",
        toolName: "Edit",
        cwd: "/tmp/workspace",
        expiresAt: Date().addingTimeInterval(60)
    )
    let high = makeApprovalRequest(
        id: "high",
        targetFile: "../outside.txt",
        toolName: "Edit",
        cwd: "/tmp/workspace",
        expiresAt: Date().addingTimeInterval(60)
    )
    let critical = makeApprovalRequest(
        id: "critical",
        toolName: "Bash",
        command: "git reset --hard HEAD",
        expiresAt: Date().addingTimeInterval(60)
    )

    [low, medium, high, critical].forEach(viewModel.enqueueRequest)

    viewModel.allowAllPending()

    let allowedIDs = decisions.map(\.requestID).sorted()
    try expect(
        allowedIDs == ["low", "medium"],
        "batch approval should only emit allow decisions for low and medium risk requests"
    )
    try expect(
        Set(viewModel.pendingRequests.map(\.id)) == Set(["high", "critical"]),
        "high and critical risk requests should remain pending"
    )
}

func testDisplaySelectionPersistsAcrossViewModelInstances() throws {
    let defaults = makeDefaultsSuite()
    let viewModel = NotchViewModel(defaults: defaults)

    viewModel.displaySelection = "display:studio-monitor"

    let reloaded = NotchViewModel(defaults: defaults)
    try expect(
        reloaded.displaySelection == "display:studio-monitor",
        "display selection should reload from defaults"
    )
}

func testJumpToAppFallbackUsesTerminalMappingBeforeProviderFallback() throws {
    let application = JumpToApp.fallbackApplication(terminal: "iTerm2", source: "codex")

    try expect(application == "iTerm", "terminal mapping should take precedence when present")
}

func testJumpToAppFallbackUsesProviderMappingWhenTerminalIsUnknown() throws {
    let application = JumpToApp.fallbackApplication(terminal: "unknown", source: "zcode")

    try expect(
        application == "/Applications/ZCode.app",
        "provider fallback should supply the app when terminal mapping is unavailable"
    )
}

extension Optional {
    func unwrap(_ message: String) throws -> Wrapped {
        guard let value = self else { throw TestFailure(message: message) }
        return value
    }
}

@main
struct ViewModelTestsRunner {
    static func main() {
        let tests: [(String, () throws -> Void)] = [
            ("scanner live update survives empty reconcile", testLiveUpdatedScannerSessionSurvivesEmptyReconcile),
            ("scanner-only session moves into history on reconcile", testScannerOnlySessionMovesIntoHistoryDuringReconcile),
            ("approval timeout denies and clears queue", testApprovalTimeoutDeniesAndClearsQueue),
            ("ask timeout cancels and clears queue", testAskTimeoutCancelsAndClearsQueue),
            ("compact overflow count uses running agents only", testCompactOverflowCountUsesRunningAgentsOnly),
            ("IPCAuthenticator rejects replay and tampering", testIPCAuthenticatorRejectsReplayAndTampering),
            ("ApprovalRiskAnalyzer marks git reset hard as critical", testApprovalRiskAnalyzerMarksGitResetHardAsCritical),
            ("ApprovalRiskAnalyzer marks workspace external edit as high", testApprovalRiskAnalyzerMarksWorkspaceExternalEditAsHigh),
            ("ApprovalRiskAnalyzer marks workspace local edit as medium", testApprovalRiskAnalyzerMarksWorkspaceLocalEditAsMedium),
            ("approval decision history persists sanitized entries", testApprovalDecisionHistoryPersistsSanitizedEntries),
            ("approval decision history keeps only thirty entries", testApprovalDecisionHistoryKeepsOnlyThirtyEntries),
            ("disabling decision history clears stored entries", testDisablingDecisionHistoryClearsStoredEntries),
            ("allowAllPending approves only low and medium risk requests", testAllowAllPendingApprovesOnlyLowAndMediumRiskRequests),
            ("displaySelection persists across view model instances", testDisplaySelectionPersistsAcrossViewModelInstances),
            ("JumpToApp fallback uses terminal mapping before provider fallback", testJumpToAppFallbackUsesTerminalMappingBeforeProviderFallback),
            ("JumpToApp fallback uses provider mapping when terminal is unknown", testJumpToAppFallbackUsesProviderMappingWhenTerminalIsUnknown),
        ]

        var failures: [(String, String)] = []
        for (name, test) in tests {
            do {
                try test()
                print("PASS: \(name)")
            } catch {
                failures.append((name, String(describing: error)))
                fputs("FAIL: \(name) — \(error)\n", stderr)
            }
        }

        if failures.isEmpty {
            print("All \(tests.count) Swift regression tests passed.")
            exit(0)
        }

        fputs("\n\(failures.count) test(s) failed.\n", stderr)
        exit(1)
    }
}
