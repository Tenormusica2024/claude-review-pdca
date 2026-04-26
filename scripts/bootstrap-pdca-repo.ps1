[CmdletBinding()]
param(
    [string]$WorkspaceRoot = (Get-Location).Path,
    [string]$ClaudereviewPdcaPath = "",
    [string]$ReviewFixPipelinePath = "",
    [switch]$SetUserEnv
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Resolve-RepoPath {
    param(
        [string]$Explicit,
        [string]$FallbackName
    )

    if ($Explicit) {
        return (Resolve-Path -LiteralPath $Explicit).Path
    }

    $workspaceResolved = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
    if ((Split-Path -Leaf $workspaceResolved) -eq $FallbackName) {
        return $workspaceResolved
    }

    $candidate = Join-Path $workspaceResolved $FallbackName
    if (Test-Path -LiteralPath $candidate) {
        return (Resolve-Path -LiteralPath $candidate).Path
    }

    return $null
}

function Test-CommandExists {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

$pdca = Resolve-RepoPath -Explicit $ClaudereviewPdcaPath -FallbackName "claude-review-pdca"
$rfp = Resolve-RepoPath -Explicit $ReviewFixPipelinePath -FallbackName "review-fix-pipeline"

Write-Host "== claude-review-pdca bootstrap stub ==" -ForegroundColor Cyan
Write-Host "WorkspaceRoot: $WorkspaceRoot"
Write-Host ""

if (-not (Test-CommandExists python)) {
    Write-Warning "python was not found"
} else {
    python --version
}

if (-not (Test-CommandExists git)) {
    Write-Warning "git was not found"
} else {
    git --version
}

Write-Host ""
Write-Host "[Repo detection]"
Write-Host "claude-review-pdca  : $pdca"
Write-Host "review-fix-pipeline : $rfp"

if (-not $pdca) {
    Write-Warning "claude-review-pdca was not found"
}
if (-not $rfp) {
    Write-Warning "review-fix-pipeline was not found"
}

if ($rfp) {
    $reviewFeedbackScript = Join-Path $rfp "scripts\\review-feedback.py"
    if (Test-Path -LiteralPath $reviewFeedbackScript) {
        $env:REVIEW_FEEDBACK_SCRIPT = $reviewFeedbackScript
        Write-Host ""
        Write-Host "[Session env]"
        Write-Host "REVIEW_FEEDBACK_SCRIPT=$($env:REVIEW_FEEDBACK_SCRIPT)"

        if ($SetUserEnv) {
            [Environment]::SetEnvironmentVariable("REVIEW_FEEDBACK_SCRIPT", $reviewFeedbackScript, "User")
            Write-Host "Saved REVIEW_FEEDBACK_SCRIPT to User environment"
        }
    }
}

Write-Host ""
Write-Host "[Next steps]" -ForegroundColor Green
Write-Host "1. Open docs/quickstart-from-fork.md"
Write-Host "2. Confirm REVIEW_FEEDBACK_SCRIPT"
Write-Host "3. Run prepare-implementation-context.py against an actual target repo"
Write-Host ""
Write-Host "python scripts/prepare-implementation-context.py --session-id demo-session --cwd C:/path/to/actual-target-repo --prompt `"sc-rfl this file`" --file-path src/app/main.py"
Write-Host ""
Write-Host "This is a bootstrap stub. It does not yet automate hook registration or producer vendoring."
