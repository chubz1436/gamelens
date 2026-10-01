param([switch]$RepairRegistration)

$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) {
    throw 'Create the GameLens .venv and install requirements.txt first.'
}
# Bind the companion to this local installation, without embedding credentials.
$mcpPath = Join-Path $projectPath 'plugins/gamelens/mcp.json'
$mcpConfig = Get-Content -LiteralPath $mcpPath -Raw | ConvertFrom-Json
$mcpConfig.mcpServers.gamelens.command = $pythonPath.Replace('\', '/')
$mcpConfig.mcpServers.gamelens.env.GAMELENS_PROJECT_DIR = $projectPath.Replace('\', '/')
# Windows PowerShell's UTF8 writer adds a BOM; keep these JSON files portable.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($mcpPath, ($mcpConfig | ConvertTo-Json -Depth 12) + "`n", $utf8NoBom)
$compatConfig = @{ mcpServers = @{ gamelens = @{
    command = $mcpConfig.mcpServers.gamelens.command
    args = $mcpConfig.mcpServers.gamelens.args
    env = $mcpConfig.mcpServers.gamelens.env
} } }
[System.IO.File]::WriteAllText((Join-Path $projectPath 'plugins/gamelens/.mcp.json'), ($compatConfig | ConvertTo-Json -Depth 12) + "`n", $utf8NoBom)
& codex plugin marketplace add $projectPath --json
if ($LASTEXITCODE -ne 0) { throw 'Could not register the GameLens marketplace.' }
foreach ($pluginName in @('gamelens', 'gamelens-godsarena')) {
    & codex plugin add "$pluginName@gamelens-local" --json
    if ($LASTEXITCODE -ne 0) { throw "Could not install $pluginName." }
}
& codex plugin list --marketplace gamelens-local --json
if ($LASTEXITCODE -ne 0) { throw 'Could not verify installed GameLens plugins.' }
# The current local host uses standalone stdio registrations. Keep one working
# registration; the packaged MCP files also support hosts that load bundled MCP.
$existingJson = & codex mcp get gamelens --json 2>$null
$exists = $LASTEXITCODE -eq 0
$expectedCommand = $pythonPath.Replace('\', '/')
$expectedScript = (Join-Path $projectPath 'plugins/gamelens/scripts/mcp_server.py').Replace('\', '/')
$expectedProject = $projectPath.Replace('\', '/')
$environment = @{ GAMELENS_PROJECT_DIR = $expectedProject }
$needsUpdate = !$exists
if ($exists) {
    $existing = ($existingJson -join "`n") | ConvertFrom-Json
    $transport = $existing.transport
    if (!$transport -or $transport.type -ne 'stdio') {
        throw 'Existing gamelens registration is not a recognized stdio transport. Review it manually; nothing was replaced.'
    }
    $currentArgs = @($transport.args)
    $matches = ([string]$transport.command).Replace('\', '/') -eq $expectedCommand -and
        $currentArgs.Count -eq 1 -and ([string]$currentArgs[0]).Replace('\', '/') -eq $expectedScript -and
        ([string]$transport.env.GAMELENS_PROJECT_DIR).Replace('\', '/') -eq $expectedProject
    if (!$matches) {
        Write-Output "Proposed gamelens command: $expectedCommand"
        Write-Output "Proposed launcher: $expectedScript"
        Write-Output "Proposed GAMELENS_PROJECT_DIR: $expectedProject"
        if (!$RepairRegistration) {
            throw 'Existing MCP registration differs. Review the proposed paths, then rerun with -RepairRegistration to update it. No registration was changed.'
        }
        # Do not replay credentials or unknown configuration through CLI arguments.
        $allowedEnv = @('GAMELENS_PROJECT_DIR', 'GAMELENS_CLIENTS_FILE')
        if ($transport.env) {
            foreach ($property in $transport.env.PSObject.Properties) {
                if ($property.Name -notin $allowedEnv) {
                    throw 'Existing MCP has custom environment settings. Review it manually; nothing was replaced.'
                }
            }
        }
        # Keep the named-client config; it must still exist at the reviewed path.
        if ($transport.env) {
            foreach ($property in $transport.env.PSObject.Properties) {
                $environment[$property.Name] = [string]$property.Value
            }
        }
        $environment.GAMELENS_PROJECT_DIR = $expectedProject
        if ($environment.ContainsKey('GAMELENS_CLIENTS_FILE') -and
            !(Test-Path -LiteralPath $environment.GAMELENS_CLIENTS_FILE)) {
            throw 'Existing named-client configuration is missing. Restore/review it before updating the registration.'
        }
        $needsUpdate = $true
    }
}
if ($needsUpdate) {
    $addArgs = @('mcp', 'add', 'gamelens')
    foreach ($key in $environment.Keys) {
        $addArgs += @('--env', "$key=$($environment[$key])")
    }
    $addArgs += @('--', $expectedCommand, $expectedScript)
    & codex @addArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not register GameLens MCP.' }
    $verifiedJson = & codex mcp get gamelens --json
    if ($LASTEXITCODE -ne 0) { throw 'Could not read back GameLens MCP registration.' }
    $verified = ($verifiedJson -join "`n") | ConvertFrom-Json
    if (([string]$verified.transport.command).Replace('\', '/') -ne $expectedCommand -or
        @($verified.transport.args).Count -ne 1 -or
        ([string]$verified.transport.args[0]).Replace('\', '/') -ne $expectedScript -or
        ([string]$verified.transport.env.GAMELENS_PROJECT_DIR).Replace('\', '/') -ne $expectedProject) {
        throw 'MCP registration read-back did not match; do not assume setup completed.'
    }
    $verifiedEnv = @($verified.transport.env.PSObject.Properties)
    if ($verifiedEnv.Count -ne $environment.Count) {
        throw 'MCP environment read-back did not match; do not assume setup completed.'
    }
    foreach ($key in $environment.Keys) {
        $property = $verified.transport.env.PSObject.Properties[$key]
        if ($null -eq $property -or [string]$property.Value -cne [string]$environment[$key]) {
            throw 'MCP environment read-back did not match; do not assume setup completed.'
        }
    }
}
Write-Output 'Installed. Reload Codex to load the new plugin tools and skills.'
Write-Output 'Authorized agents may use gamelens_session arm/live/stop; no game/race starts on installation.'
