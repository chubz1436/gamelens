param([string]$ClientsFile)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv/Scripts/python.exe'
$pythonWindowPath = Join-Path $projectPath '.venv/Scripts/pythonw.exe'
if (!$ClientsFile) { $ClientsFile = Join-Path $projectPath 'profiles/clients.local.json' }
if (!(Test-Path -LiteralPath $ClientsFile)) {
    if ($PSBoundParameters.ContainsKey('ClientsFile')) { throw 'Specified client configuration does not exist.' }
    $localClients = @{clients=@(1..10 | ForEach-Object {
        @{name=('client-{0:d2}' -f $_);url=('http://127.0.0.1:' + (8776 + $_))}
    })}
    [IO.File]::WriteAllText($ClientsFile,($localClients | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
}
$ClientsFile = (Resolve-Path -LiteralPath $ClientsFile).Path
# Validate the complete named-client config before changing registration/shortcuts.
& $pythonPath -c 'import sys; from gamelens.multi_client import load_clients; load_clients(sys.argv[1]); print("Client configuration validated")' $ClientsFile
if ($LASTEXITCODE -ne 0) { throw 'Invalid client configuration; registration unchanged.' }
$clients = (Get-Content -LiteralPath $ClientsFile -Raw | ConvertFrom-Json).clients
$shellObject = New-Object -ComObject WScript.Shell
foreach ($client in $clients) {
    $port = ([Uri]$client.url).Port
    foreach ($folder in @([Environment]::GetFolderPath('Desktop'),(Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))) {
        $shortcut = $shellObject.CreateShortcut((Join-Path $folder ("GameLens " + $client.name + '.lnk')))
        $shortcut.TargetPath = $pythonWindowPath
        $shortcut.Arguments = '"' + (Join-Path $projectPath 'GameLens.pyw') + '" --port ' + $port
        $shortcut.WorkingDirectory = $projectPath
        $shortcut.Description = "GameLens $($client.name) on port $port; choose its game window"
        $shortcut.IconLocation = "$pythonWindowPath,0"
        $shortcut.Save()
    }
}
# This replaces the same registration; it never creates a second provider with
# duplicate tool names. It does not touch capture servers, windows, Arm or Live.
& codex mcp add gamelens --env "GAMELENS_PROJECT_DIR=$projectPath" --env "GAMELENS_CLIENTS_FILE=$ClientsFile" -- $pythonPath (Join-Path $projectPath 'plugins/gamelens/scripts/mcp_server.py')
if ($LASTEXITCODE -ne 0) { throw 'Could not register named GameLens clients.' }
Write-Output 'Multiple-client MCP configured. Reload Codex; choose client explicitly on tool calls.'
Write-Output 'Launch the matching GameLens shortcut, choose its game and Arm/Go live yourself.'
