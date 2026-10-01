# Read-only wrapper. Never install Python, elevate, change policy, or run config commands.
param(
    [string]$PythonPath,
    [string]$Format = 'text',
    [string]$Scope = 'core',
    [string]$McpHost,
    [string]$McpConfig,
    [string]$Client,
    [string]$ClientsFile,
    [string]$Url,
    [switch]$Connect,
    [string]$ExpectedTargetHwnd
)
$ErrorActionPreference = 'Stop'

function Write-MinimalReport([string]$Reason, [int]$Code) {
    $report = [ordered]@{
        schema_version = 1; mode = 'offline'; exit_code = $Code
        checks = @([ordered]@{
            id = 'bootstrap'; scope = 'environment'; status = 'blocked'
            affects_exit = $true; reason_code = $Reason; safe_evidence = @{}
            manual_next_step = 'Select an existing Python 3.11+ interpreter manually; nothing was installed.'
        })
        actual_frame_verification = @{status = 'unverified'; reason_code = 'FRAME_NOT_REQUESTED'}
        input_authorization = @{granted_by_checker = $false}
    }
    if ($Format -eq 'json') { $report | ConvertTo-Json -Depth 12 -Compress }
    else { Write-Output ('blocked: ' + $Reason + '. No setup changes were made.') }
}

function Get-ApprovedPython([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $null }
    if ($Candidate.StartsWith('\\') -or $Candidate.StartsWith('//')) { return $null }
    if (-not [IO.Path]::IsPathRooted($Candidate)) { return $null }
    if ($Candidate -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') { return $null }
    if ([IO.Path]::GetFileName($Candidate) -notmatch '^python(?:3(?:\.[0-9]+)?)?(?:\.exe)?$') { return $null }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    $item = Get-Item -LiteralPath $Candidate -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $null }
    return $item.FullName
}

function Quote-NativeArgument([string]$Value) {
    # Windows argv quoting, not shell quoting. UseShellExecute is always false.
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append([char]34)
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) { $slashes++; continue }
        if ($character -eq [char]34) {
            [void]$builder.Append([char]92, (2 * $slashes + 1))
            [void]$builder.Append([char]34)
        } else {
            [void]$builder.Append([char]92, $slashes)
            [void]$builder.Append($character)
        }
        $slashes = 0
    }
    [void]$builder.Append([char]92, (2 * $slashes))
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

$process = $null
try {
    if ($args.Count -gt 0 -or $Format -notin @('text', 'json') -or $Scope -notin @('core', 'desktop')) {
        Write-MinimalReport 'CLI_INVALID' 64
        exit 64
    }
    $project = Split-Path -Parent $PSScriptRoot
    if ($PythonPath) {
        # An explicitly supplied missing interpreter must NOT fall back.
        $python = Get-ApprovedPython $PythonPath
    } else {
        $python = Get-ApprovedPython (Join-Path $project '.venv/Scripts/python.exe')
        if (-not $python) {
            foreach ($name in @('python.exe', 'python3.exe')) {
                $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($command) { $python = Get-ApprovedPython $command.Source }
                if ($python) { break }
            }
        }
    }
    if (-not $python) {
        Write-MinimalReport 'PYTHON_NOT_FOUND' 2
        exit 2
    }
    $script = Join-Path $PSScriptRoot 'check_setup.py'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        Write-MinimalReport 'CHECKER_FILE_MISSING' 2
        exit 2
    }
    # -E/-B also exist in Python 2.7. The script itself handles old versions
    # before modern imports. Do not invoke configured MCP commands or py.exe.
    $arguments = @('-E', '-B', $script, '--format', 'json', '--scope', $Scope)
    foreach ($pair in @(
        @('--mcp-host', $McpHost), @('--mcp-config', $McpConfig),
        @('--client', $Client), @('--clients-file', $ClientsFile), @('--url', $Url),
        @('--expected-target-hwnd', $ExpectedTargetHwnd)
    )) {
        if (-not [string]::IsNullOrEmpty($pair[1])) { $arguments += @($pair[0], $pair[1]) }
    }
    if ($Connect) { $arguments += '--connect' }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $python
    $start.Arguments = (($arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')
    $start.WorkingDirectory = $project
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'launch failed' }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    # A wrapper watchdog, independent of the stricter 3-second HTTP deadline.
    if (-not $process.WaitForExit(30000)) {
        try { $process.Kill() } catch { }
        Write-MinimalReport 'CHECKER_PROCESS_DEADLINE' 70
        exit 70
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    if ($stderr.Length -gt 0 -or $stdout.Length -gt 65536 -or [string]::IsNullOrWhiteSpace($stdout)) {
        Write-MinimalReport 'PYTHON_LAUNCH_FAILED' 70
        exit 70
    }
    $report = $stdout | ConvertFrom-Json
    if ($report.schema_version -ne 1 -or $report.exit_code -ne $process.ExitCode -or
        $process.ExitCode -notin @(0, 1, 2, 64, 70, 130) -or
        $report.input_authorization.granted_by_checker -ne $false) {
        Write-MinimalReport 'CHECKER_REPORT_INVALID' 70
        exit 70
    }
    if ($Format -eq 'json') { Write-Output $stdout.TrimEnd() }
    else {
        Write-Output ('GameLens setup diagnostics: ' + $report.mode + ' (read-only)')
        foreach ($check in $report.checks) {
            Write-Output ($check.status + ' ' + $check.id + ': ' + $check.reason_code)
            Write-Output ('  ' + ($check.safe_evidence | ConvertTo-Json -Depth 8 -Compress))
            if ($check.affects_exit -and $check.status -ne 'pass' -and $check.status -ne 'skipped') {
                Write-Output ('  ' + $check.manual_next_step)
            }
        }
        foreach ($name in @('selected_session', 'reported_capture', 'reported_target', 'reported_input_state', 'target_match')) {
            if ($null -ne $report.$name) { Write-Output ($name + ': ' + ($report.$name | ConvertTo-Json -Depth 8 -Compress)) }
        }
        Write-Output 'Actual frame: unverified. Input authorization granted by checker: false.'
        Write-Output ('Exit code: ' + $report.exit_code + ' (not permission to control input).')
    }
    exit $process.ExitCode
} catch {
    # Do not print ErrorRecord, command lines, native stderr or exception text.
    Write-MinimalReport 'CHECKER_BOOTSTRAP_FAILED' 70
    exit 70
} finally {
    if ($process) { $process.Dispose() }
}
