# install-hook.ps1 — register the Vibe Center relay into Claude Code
# settings on Windows (mirror of install-hook.sh).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install-hook.ps1              # install
#   powershell -ExecutionPolicy Bypass -File install-hook.ps1 -Uninstall   # remove
param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$mode = if ($Uninstall) { "uninstall" } else { "install" }

Push-Location $repoRoot
try {
    if ($Uninstall) {
        python -c "import sys; sys.path.insert(0, r'$repoRoot'); from vibecenter import hooks; ok, msg = hooks.uninstall(); print(msg)"
    } else {
        python -c "import sys; sys.path.insert(0, r'$repoRoot'); from vibecenter import hooks; ok, msg = hooks.install(); print(msg)"
        Write-Host ""
        Write-Host "Hook events: SessionStart UserPromptSubmit PreToolUse PostToolUse PostToolUseFailure Stop StopFailure Notification PermissionRequest SessionEnd"
    }
} finally {
    Pop-Location
}
