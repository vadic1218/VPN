param(
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [string]$CommitMessage = "",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:PythonExecutable = $null
$script:PythonPrefixArguments = @()

function Join-ProcessArguments {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    return ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + ($_ -replace '(\\*)"', '$1$1\"') + '"'
        }
        else {
            $_
        }
    }) -join " "
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "git"
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.Arguments = Join-ProcessArguments -Arguments $Arguments

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    $output = ($stdout + $stderr).Trim()
    if ($process.ExitCode -ne 0) {
        $joined = $Arguments -join " "
        throw "git $joined failed:`n$output"
    }

    return $output
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PythonExecutable
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.Arguments = Join-ProcessArguments -Arguments ($script:PythonPrefixArguments + $Arguments)

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    $output = ($stdout + $stderr).Trim()
    if ($process.ExitCode -ne 0) {
        $joined = $Arguments -join " "
        throw "python $joined failed:`n$output"
    }

    return $output
}

function Test-CommandWorks {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [string[]]$Arguments = @()
    )

    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Executable
        $psi.WorkingDirectory = (Get-Location).Path
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.Arguments = Join-ProcessArguments -Arguments $Arguments

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $psi
        [void]$process.Start()
        $null = $process.StandardOutput.ReadToEnd()
        $null = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return $process.ExitCode -eq 0
    }
    catch {
        return $false
    }
}

function Initialize-PythonCommand {
    $candidates = @(
        @{ Executable = "python"; Prefix = @("--version") },
        @{ Executable = "py"; Prefix = @("-3", "--version") },
        @{ Executable = "py"; Prefix = @("--version") }
    )

    foreach ($candidate in $candidates) {
        if (Test-CommandWorks -Executable $candidate.Executable -Arguments $candidate.Prefix) {
            $script:PythonExecutable = $candidate.Executable
            if ($candidate.Prefix.Length -gt 1) {
                $script:PythonPrefixArguments = @($candidate.Prefix[0..($candidate.Prefix.Length - 2)])
            }
            else {
                $script:PythonPrefixArguments = @()
            }
            return
        }
    }

    throw "Python launcher not found. Install Python or make 'python' or 'py' available in PATH."
}

function Test-BlockedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $normalized = $Path.Replace("\", "/").ToLowerInvariant()
    $blockedPatterns = @(
        "^\.env$",
        "^config\.json$",
        "^reminders\.db$",
        "^.*\.db$",
        "^.*\.sqlite$",
        "^.*\.sqlite3$"
    )

    foreach ($pattern in $blockedPatterns) {
        if ($normalized -match $pattern) {
            return $true
        }
    }

    return $false
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

$env:PYTHONDONTWRITEBYTECODE = "1"
Initialize-PythonCommand

$insideRepo = Invoke-Git -Arguments @("rev-parse", "--is-inside-work-tree")
if ($insideRepo.Trim() -ne "true") {
    throw "Current directory is not inside a git repository."
}

$currentBranch = (Invoke-Git -Arguments @("branch", "--show-current")).Trim()
if (-not $currentBranch) {
    throw "Detached HEAD is not supported for auto publish."
}

if ($currentBranch -ne $Branch) {
    throw "Current branch '$currentBranch' does not match expected branch '$Branch'."
}

Write-Host "Running syntax check..."
Invoke-Python -Arguments @(
    "-c",
    "import ast; from pathlib import Path; [ast.parse(Path(path).read_text(encoding='utf-8'), filename=path) for path in ('bot.py', 'tests/test_bot.py')]"
) | Out-Null

Write-Host "Running tests..."
Invoke-Python -Arguments @("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py") | Out-Null

$statusBefore = Invoke-Git -Arguments @("status", "--short")
if (-not $statusBefore.Trim()) {
    Write-Host "No changes to publish."
    exit 0
}

$trackedChanges = @(Invoke-Git -Arguments @("diff", "--name-only", "HEAD") | Where-Object { $_.Trim() })
$untrackedChanges = @(Invoke-Git -Arguments @("ls-files", "--others", "--exclude-standard") | Where-Object { $_.Trim() })
$changedPaths = @($trackedChanges + $untrackedChanges | Select-Object -Unique)

if (-not $changedPaths.Count) {
    Write-Host "No changed files to publish."
    exit 0
}

$blockedPaths = @($changedPaths | Where-Object { Test-BlockedPath -Path $_ })
if ($blockedPaths.Count -gt 0) {
    throw "Blocked files detected in staged changes: $($blockedPaths -join ', ')"
}

$upstreamExists = $true
try {
    Invoke-Git -Arguments @("rev-parse", "--verify", "$Remote/$Branch") | Out-Null
}
catch {
    $upstreamExists = $false
}

if ($upstreamExists) {
    Write-Host "Checking remote sync..."
    if ($DryRun) {
        Write-Host "Dry run mode: skipping remote fetch."
    }
    else {
        Invoke-Git -Arguments @("fetch", $Remote, $Branch) | Out-Null
    }

    $localSha = (Invoke-Git -Arguments @("rev-parse", "HEAD")).Trim()
    $remoteSha = (Invoke-Git -Arguments @("rev-parse", "$Remote/$Branch")).Trim()

    if ($localSha -ne $remoteSha) {
        throw "Local branch is not in sync with $Remote/$Branch. Pull or rebase before auto publish."
    }
}

if (-not $CommitMessage.Trim()) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $CommitMessage = "chore: auto publish $timestamp"
}

if ($DryRun) {
    Write-Host "Dry run completed. Changes are validated and files are safe to stage."
    exit 0
}

Invoke-Git -Arguments @("add", "-A") | Out-Null

$stagedNames = Invoke-Git -Arguments @("diff", "--cached", "--name-only")
$stagedPaths = @($stagedNames | Where-Object { $_.Trim() })
if (-not $stagedPaths.Count) {
    Write-Host "No staged changes to commit."
    exit 0
}

Write-Host "Creating commit..."
Invoke-Git -Arguments @("commit", "-m", $CommitMessage) | Out-Null

Write-Host "Pushing to $Remote/$Branch..."
Invoke-Git -Arguments @("push", $Remote, $Branch) | Out-Null

Write-Host "Auto publish completed successfully."
