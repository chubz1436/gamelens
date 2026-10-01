$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonWindowPath = Join-Path $projectPath '.venv/Scripts/pythonw.exe'
$launcherPath = Join-Path $projectPath 'GameLens.pyw'
if (!(Test-Path -LiteralPath $pythonWindowPath)) {
    throw 'Create the project .venv and install requirements-desktop.txt first.'
}
$shortcutFolders = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
$shellObject = New-Object -ComObject WScript.Shell
foreach ($shortcutFolder in $shortcutFolders) {
    $shortcutPath = Join-Path $shortcutFolder 'GameLens.lnk'
    $shortcut = $shellObject.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonWindowPath
    $shortcut.Arguments = '"' + $launcherPath + '"'
    $shortcut.WorkingDirectory = $projectPath
    $shortcut.Description = 'GameLens desktop capture and controls'
    $shortcut.IconLocation = "$pythonWindowPath,0"
    $shortcut.Save()
    Write-Output "Installed: $shortcutPath"
}
