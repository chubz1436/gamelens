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

function Get-PathDriveType([string]$Root) {
    # DriveType only: never query IsReady, free space, contents or enumerate drives.
    return ([IO.DriveInfo]::new($Root)).DriveType
}

function Get-LocalPathAttributes([string]$LiteralPath) {
    # One already-gated path component, without PowerShell provider resolution.
    return [IO.File]::GetAttributes($LiteralPath)
}

function Get-ApprovedLocalFile([string]$Candidate) {
    try {
        if ([string]::IsNullOrWhiteSpace($Candidate) -or $Candidate.Length -gt 32767) { return $null }
        # Only fully qualified DOS paths. Reject UNC, device/provider paths, ADS,
        # relative-drive paths, traversal and ambiguous Win32 trailing dots/spaces.
        $path = $Candidate.Replace('/', '\')
        if ($path -notmatch '^[A-Za-z]:\\') { return $null }
        $tail = $path.Substring(3)
        if ($tail -match '[\x00-\x1f:*?"<>|]') { return $null }
        $parts = @($tail.Split([char]92))
        if ($parts.Count -eq 0) { return $null }
        foreach ($part in $parts) {
            if ([string]::IsNullOrEmpty($part) -or $part -in @('.', '..') -or
                $part.EndsWith('.') -or $part.EndsWith(' ')) { return $null }
        }
        $root = $path.Substring(0, 3)
        # This is deliberately BEFORE ANY attributes/existence probe. A mapped
        # network drive still has a drive letter, so an UNC string test is not enough.
        $type = Get-PathDriveType $root
        if ($type -notin @([IO.DriveType]::Fixed, [IO.DriveType]::Removable, [IO.DriveType]::Ram)) { return $null }
        $current = $root
        for ($index = 0; $index -le $parts.Count; $index++) {
            if ($index -gt 0) { $current = [IO.Path]::Combine($current, $parts[$index - 1]) }
            # Root -> parent -> leaf. Never probe below an unchecked junction.
            $attributes = Get-LocalPathAttributes $current
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                ($attributes -band [IO.FileAttributes]::Device) -ne 0) { return $null }
            $directory = ($attributes -band [IO.FileAttributes]::Directory) -ne 0
            if ($index -lt $parts.Count -and -not $directory) { return $null }
            if ($index -eq $parts.Count -and $directory) { return $null }
        }
        return $current
    } catch {
        # Unknown drive, missing path, access failure, etc. Never leak the path/error.
        return $null
    }
}

function Get-ApprovedPython([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $null }
    if ($Candidate -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') { return $null }
    try {
        if ([IO.Path]::GetFileName($Candidate) -notmatch '^python(?:3(?:\.[0-9]+)?)?(?:\.exe)?$') { return $null }
    } catch { return $null }
    return Get-ApprovedLocalFile $Candidate
}

function Find-SystemPython {
    # Do NOT use Get-Command/where.exe: discovery itself could probe a network PATH.
    # Inspect only literal, explicitly inherited PATH directories through the same
    # drive/parent gate. No CWD, PATHEXT expansion, py.exe, registry or host config.
    $searchPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    if ([string]::IsNullOrEmpty($searchPath) -or $searchPath.Length -gt 32768) { return $null }
    $directories = @($searchPath.Split([char]59))
    if ($directories.Count -gt 256) { return $null }
    foreach ($directory in $directories) {
        $directory = $directory.Trim()
        if ($directory.Length -ge 2 -and $directory[0] -eq [char]34 -and $directory[$directory.Length - 1] -eq [char]34) {
            $directory = $directory.Substring(1, $directory.Length - 2)
        }
        if ([string]::IsNullOrEmpty($directory)) { continue }
        foreach ($name in @('python.exe', 'python3.exe')) {
            try { $candidate = [IO.Path]::Combine($directory, $name) } catch { continue }
            $approved = Get-ApprovedPython $candidate
            if ($approved) { return $approved }
        }
    }
    return $null
}

function Resolve-CheckerPython([string]$ExplicitPath, [bool]$ExplicitSupplied, [string]$Project) {
    if ($ExplicitSupplied) { return Get-ApprovedPython $ExplicitPath }
    $candidate = [IO.Path]::Combine($Project, '.venv\Scripts\python.exe')
    $approved = Get-ApprovedPython $candidate
    if ($approved) { return $approved }
    return Find-SystemPython
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

function Close-OwnedChecker($Process, [bool]$Started, $Readers) {
    $verified = $true
    if ($Started) {
        try {
            if (-not $Process.HasExited) { $Process.Kill() }
            # Kill is asynchronous. Confirm exit before reporting ordinary cleanup.
            if (-not $Process.WaitForExit(2000)) { $verified = $false }
        } catch { $verified = $false }
    }
    foreach ($reader in $Readers) {
        try { $reader.Dispose() } catch { $verified = $false }
    }
    if ($null -ne $Process) {
        try { $Process.Dispose() } catch { $verified = $false }
    }
    return $verified
}

function Invoke-BoundedChecker([Diagnostics.ProcessStartInfo]$StartInfo, [int]$TimeoutMilliseconds = 30000) {
    # The CLI always uses 30 seconds. Tests may shorten, never extend, this bound.
    # All caps count BYTES, not decoded chars. stderr is counted/drained, never retained.
    $stdoutCap = 65536
    $stderrCap = 16384
    $combinedCap = 65536
    $chunkSize = 4096
    $process = $null
    $started = $false
    $readers = @()
    $retained = $null
    $failure = $null
    $result = $null
    try {
        if ($TimeoutMilliseconds -lt 1 -or $TimeoutMilliseconds -gt 30000) { throw 'invalid internal limit' }
        if ($StartInfo.UseShellExecute -or -not $StartInfo.RedirectStandardOutput -or -not $StartInfo.RedirectStandardError) {
            throw 'invalid internal process configuration'
        }
        $retained = [IO.MemoryStream]::new($stdoutCap)
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $StartInfo
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $started = $process.Start()
        if (-not $started) { throw 'launch failed' }
        $readers = @($process.StandardOutput, $process.StandardError)
        $pipes = @()
        for ($index = 0; $index -lt 2; $index++) {
            $pipe = [pscustomobject]@{
                Stream = $readers[$index].BaseStream
                Buffer = (New-Object byte[] $chunkSize)
                Task = $null; Eof = $false; Count = 0
                Cap = $(if ($index -eq 0) { $stdoutCap } else { $stderrCap })
                Retain = ($index -eq 0)
            }
            # Exactly one bounded outstanding read per pipe. Both are active
            # before polling either; no ReadLine/ReadToEnd or unbounded event queues.
            $pipe.Task = $pipe.Stream.ReadAsync($pipe.Buffer, 0, $chunkSize)
            $pipes += $pipe
        }
        $combined = 0
        while ($true) {
            if ($clock.ElapsedMilliseconds -ge $TimeoutMilliseconds) {
                $failure = 'CHECKER_PROCESS_DEADLINE'; break
            }
            foreach ($pipe in $pipes) {
                if ($pipe.Eof -or -not $pipe.Task.IsCompleted) { continue }
                $count = $pipe.Task.GetAwaiter().GetResult()
                if ($count -eq 0) { $pipe.Eof = $true; continue }
                $pipe.Count += $count
                $combined += $count
                if ($pipe.Count -gt $pipe.Cap -or $combined -gt $combinedCap) {
                    $failure = 'CHECKER_OUTPUT_LIMIT'; break
                }
                if ($pipe.Retain) { $retained.Write($pipe.Buffer, 0, $count) }
                $pipe.Task = $pipe.Stream.ReadAsync($pipe.Buffer, 0, $chunkSize)
            }
            if ($failure) { break }
            if ($pipes[0].Eof -and $pipes[1].Eof -and $process.HasExited) {
                if ($pipes[1].Count -gt 0 -or $retained.Length -eq 0) {
                    $failure = 'PYTHON_LAUNCH_FAILED'
                } else {
                    # Decode only AFTER complete bounded collection. Invalid UTF-8
                    # is rejected, not replaced or emitted as partial/raw output.
                    $utf8 = [Text.UTF8Encoding]::new($false, $true)
                    $text = $utf8.GetString($retained.ToArray())
                    $result = [pscustomobject]@{ Ok = $true; Stdout = $text; ExitCode = $process.ExitCode; Reason = $null }
                }
                break
            }
            Start-Sleep -Milliseconds 10
        }
    } catch {
        $failure = 'CHECKER_CHILD_IO_FAILED'
    } finally {
        # Stop/reap our own checker BEFORE releasing pipes, including output-cap,
        # timeout, decoding errors and early exceptions. Never touch GameLens.
        $clean = Close-OwnedChecker $process $started $readers
        if (-not $clean) { $failure = 'CHECKER_CLEANUP_UNVERIFIED' }
        if ($null -ne $retained) { $retained.Dispose() }
    }
    if ($failure) {
        return [pscustomobject]@{ Ok = $false; Stdout = $null; ExitCode = 70; Reason = $failure }
    }
    return $result
}

try {
    if ($args.Count -gt 0 -or $Format -notin @('text', 'json') -or $Scope -notin @('core', 'desktop')) {
        Write-MinimalReport 'CLI_INVALID' 64
        exit 64
    }
    $project = Split-Path -Parent $PSScriptRoot
    $python = Resolve-CheckerPython $PythonPath ($PSBoundParameters.ContainsKey('PythonPath')) $project
    if (-not $python) {
        Write-MinimalReport 'PYTHON_NOT_FOUND' 2
        exit 2
    }
    $script = Get-ApprovedLocalFile ([IO.Path]::Combine($PSScriptRoot, 'check_setup.py'))
    if (-not $script) {
        Write-MinimalReport 'CHECKER_FILE_MISSING' 2
        exit 2
    }
    # -E/-B also exist in Python 2.7. The checker handles old versions before
    # modern imports. No configured MCP command or shell is evaluated.
    $arguments = @('-E', '-B', $script, '--format', 'json', '--scope', $Scope)
    foreach ($pair in @(
        @('--mcp-host', $McpHost), @('--mcp-config', $McpConfig),
        @('--client', $Client), @('--clients-file', $ClientsFile), @('--url', $Url),
        @('--expected-target-hwnd', $ExpectedTargetHwnd)
    )) {
        if (-not [string]::IsNullOrEmpty($pair[1])) { $arguments += @($pair[0], $pair[1]) }
    }
    if ($Connect) { $arguments += '--connect' }
    # Recheck immediately before constructing the child. This narrows, but cannot
    # eliminate, rename/remap races; no handle-based executable identity is claimed.
    $python = Get-ApprovedPython $python
    if (-not $python -or -not (Get-ApprovedLocalFile $script)) {
        Write-MinimalReport 'CHECKER_PATH_CHANGED' 2
        exit 2
    }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $python
    $start.Arguments = (($arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')
    $start.WorkingDirectory = $project
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $child = Invoke-BoundedChecker $start
    if (-not $child.Ok) {
        Write-MinimalReport $child.Reason 70
        exit 70
    }
    # Do not emit any child output until exit, EOF on both streams, strict decode
    # and report validation succeeded. Re-serialize the complete parsed report.
    $report = $child.Stdout | ConvertFrom-Json
    if ($null -eq $report -or $report.schema_version -ne 1 -or
        $report.mode -notin @('offline', 'connected') -or
        $report.exit_code -ne $child.ExitCode -or
        $child.ExitCode -notin @(0, 1, 2, 64, 70, 130) -or
        $null -eq $report.checks -or @($report.checks).Count -gt 64 -or
        $report.actual_frame_verification.status -ne 'unverified' -or
        $report.actual_frame_verification.reason_code -ne 'FRAME_NOT_REQUESTED' -or
        $report.input_authorization.granted_by_checker -isnot [bool] -or
        $report.input_authorization.granted_by_checker -ne $false) {
        Write-MinimalReport 'CHECKER_REPORT_INVALID' 70
        exit 70
    }
    if ($Format -eq 'json') { $report | ConvertTo-Json -Depth 16 -Compress }
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
    exit $child.ExitCode
} catch {
    # No ErrorRecord, child buffers, command line, raw stderr or exception text.
    Write-MinimalReport 'CHECKER_BOOTSTRAP_FAILED' 70
    exit 70
}
