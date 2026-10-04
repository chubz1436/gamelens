# Test fixture only, authored NOT RUN. No Pester, native GameLens imports,
# mapped drives, network shares, elevation or installed dependencies required.
param(
    [string]$WrapperPath,
    [string]$Case,
    [string]$PythonPath,
    [string]$CheckerFile,
    [int]$TimeoutMilliseconds = 1000
)
$ErrorActionPreference = 'Stop'
try {
    # Load only reviewed function definitions, not the wrapper's entry point.
    # This is not a production test hook or a config-derived command evaluator.
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($WrapperPath, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -ne 0) { throw 'wrapper syntax invalid' }
    foreach ($statement in $ast.EndBlock.Statements) {
        if ($statement -is [Management.Automation.Language.FunctionDefinitionAst]) {
            . ([scriptblock]::Create($statement.Extent.Text))
        }
    }
    if ($Case -eq 'collect') {
        $approved = Get-ApprovedPython $PythonPath
        $scriptFile = Get-ApprovedLocalFile $CheckerFile
        if (-not $approved -or -not $scriptFile) { throw 'fixture path not approved' }
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $approved
        $start.Arguments = ((@('-E', '-B', $scriptFile) | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')
        $start.WorkingDirectory = [IO.Path]::GetDirectoryName($scriptFile)
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $result = Invoke-BoundedChecker $start $TimeoutMilliseconds
        # Never echo even the fixture's raw child output or secret canary.
        [ordered]@{
            ok = $result.Ok; exit_code = $result.ExitCode; reason = $result.Reason
            has_child_output = ($null -ne $result.Stdout)
        } | ConvertTo-Json -Compress
        exit 0
    }

    $script:DriveCalls = [Collections.Generic.List[string]]::new()
    $script:AttributeCalls = [Collections.Generic.List[string]]::new()
    $script:Launches = 0
    function Get-PathDriveType([string]$Root) {
        $script:DriveCalls.Add($Root)
        if ($Case -eq 'drive-error') { throw 'SECRET_CANARY_WRAPPER_29c1' }
        if ($Case -eq 'drive-network' -or $Root -eq 'N:\') { return [IO.DriveType]::Network }
        if ($Case -eq 'drive-unknown') { return [IO.DriveType]::Unknown }
        if ($Case -eq 'drive-no-root') { return [IO.DriveType]::NoRootDirectory }
        if ($Case -eq 'drive-cdrom') { return [IO.DriveType]::CDRom }
        return [IO.DriveType]::Fixed
    }
    function Get-LocalPathAttributes([string]$LiteralPath) {
        $script:AttributeCalls.Add($LiteralPath)
        if ($Case -eq 'attribute-error') { throw 'SECRET_CANARY_WRAPPER_29c1' }
        if ($LiteralPath -like 'N:\*') { throw 'network attributes must NEVER be probed' }
        if ($LiteralPath -eq 'Q:\Missing' -or $LiteralPath -eq 'Q:\Project With Spaces\.venv') {
            if ($Case -ne 'resolve-project') { throw 'synthetic missing directory' }
        }
        $junction = ($Case -eq 'parent-junction' -and $LiteralPath -eq 'Q:\Gate\Python With Spaces') -or
                    ($Case -eq 'root-junction' -and $LiteralPath -eq 'Q:\') -or
                    ($LiteralPath -eq 'Q:\Junction')
        if ($junction) { return ([IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint) }
        if ($LiteralPath.EndsWith('python.exe')) {
            if ($Case -eq 'leaf-reparse') { return ([IO.FileAttributes]::Normal -bor [IO.FileAttributes]::ReparsePoint) }
            return [IO.FileAttributes]::Normal
        }
        return [IO.FileAttributes]::Directory
    }
    function Invoke-BoundedChecker {
        $script:Launches++
        throw 'validation probe must not launch a checker'
    }
    $candidate = 'Q:\Gate\Python With Spaces\python.exe'
    if ($Case -eq 'resolve-explicit-missing') {
        $env:PATH = 'Q:\System Python'
        $approved = Resolve-CheckerPython 'Q:\Missing\python.exe' $true 'Q:\Project With Spaces'
    } elseif ($Case -eq 'resolve-system') {
        $env:PATH = 'N:\Mapped Python;Q:\Junction\Python;Q:\System Python'
        $approved = Resolve-CheckerPython '' $false 'Q:\Project With Spaces'
    } elseif ($Case -eq 'resolve-project') {
        $env:PATH = 'N:\Mapped Python'
        $approved = Resolve-CheckerPython '' $false 'Q:\Project With Spaces'
    } else {
        if ($Case -eq 'literal-unc') { $candidate = '\\server\share\python.exe' }
        if ($Case -eq 'relative-drive') { $candidate = 'Q:python.exe' }
        if ($Case -eq 'traversal') { $candidate = 'Q:\Gate\..\python.exe' }
        if ($Case -eq 'store-alias') { $candidate = 'Q:\Users\Owner\Microsoft\WindowsApps\python.exe' }
        $approved = Get-ApprovedPython $candidate
    }
    [ordered]@{
        approved = ($null -ne $approved); path = $approved
        drive_roots = @($script:DriveCalls.ToArray())
        attribute_paths = @($script:AttributeCalls.ToArray())
        launches = $script:Launches
    } | ConvertTo-Json -Depth 5 -Compress
    exit 0
} catch {
    # Test failures never expose arbitrary parser/fixture/OS exception contents.
    Write-Output '{"fixture_error":"WRAPPER_PROBE_FAILED"}'
    exit 1
}
