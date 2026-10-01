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
& codex mcp get gamelens --json | Out-Null
if ($LASTEXITCODE -ne 0) {
    & codex mcp add gamelens --env "GAMELENS_PROJECT_DIR=$projectPath" -- $pythonPath (Join-Path $projectPath 'plugins/gamelens/scripts/mcp_server.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not register GameLens MCP.' }
}
Write-Output 'Installed. Reload Codex to load the new plugin tools and skills.'
Write-Output 'Authorized agents may use gamelens_session arm/live/stop; no game/race starts on installation.'
