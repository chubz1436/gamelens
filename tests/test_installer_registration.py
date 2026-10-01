"""Run the installer against a fake Codex CLI; never touch real registrations."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("repair,matching,custom", [
    (False, False, False), (True, False, False),
    (False, True, False), (True, False, True),
])
def test_existing_registration_requires_review_and_preserves_client_config(tmp_path, repair, matching, custom):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell required")
    project = tmp_path / "project with spaces"
    (project / "tools").mkdir(parents=True)
    plugin = project / "plugins/gamelens"
    plugin.mkdir(parents=True)
    python = project / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()  # Path existence only: the installer must never execute this.
    script = project / "tools/install_plugins.ps1"
    script.write_text((ROOT / "tools/install_plugins.ps1").read_text(), encoding="utf-8")
    (plugin / "mcp.json").write_text(json.dumps({"mcpServers": {"gamelens": {
        "command": "", "args": [], "env": {"GAMELENS_PROJECT_DIR": ""}
    }}}))
    clients = project / "clients.json"
    clients.write_text('{"clients": []}')
    registration = {
        "transport": {
            "type": "stdio",
            "command": python.as_posix() if matching else "C:/old/python.exe",
            "args": [(plugin / "scripts/mcp_server.py").as_posix()] if matching else ["-m", "gamelens.mcp"],
            "env": {
                "GAMELENS_PROJECT_DIR": project.as_posix() if matching else "C:/old",
                "GAMELENS_CLIENTS_FILE": str(clients),
            },
        },
    }
    if custom:
        registration["transport"]["env"]["CUSTOM_OPTION"] = "synthetic"
    config = tmp_path / "state.json"
    config.write_text(json.dumps(registration))
    output = tmp_path / "result.json"
    harness = tmp_path / "harness.ps1"
    harness.write_text(r"""
param($Installer, $Config, $Output, $Repair)
$global:Registration = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$global:AddCount = 0
function global:codex {
    $a = @($args)
    $global:LASTEXITCODE = 0
    if ($a[0] -eq 'plugin') { return '{}' }
    if ($a[0] -eq 'mcp' -and $a[1] -eq 'get') {
        return ($global:Registration | ConvertTo-Json -Depth 12 -Compress)
    }
    if ($a[0] -eq 'mcp' -and $a[1] -eq 'add') {
        $global:AddCount++
        $envMap = @{}
        $i = 3
        while ($a[$i] -eq '--env') {
            $pair = ([string]$a[$i+1]).Split(@('='), 2)
            $envMap[$pair[0]] = $pair[1]
            $i += 2
        }
        if ($a[$i] -ne '--') { throw 'Expected argument separator' }
        $global:Registration = [pscustomobject]@{ transport = [pscustomobject]@{
            type = 'stdio'; command = $a[$i+1]; args = @($a[$i+2]); env = [pscustomobject]$envMap
        }}
        return '{}'
    }
    throw 'Unexpected Codex command'
}
$ok = $false
$errorText = ''
try {
    if ($Repair -eq 'yes') { & $Installer -RepairRegistration | Out-Null }
    else { & $Installer | Out-Null }
    $ok = $true
} catch { $errorText = $_.Exception.Message }
@{ok=$ok; error=$errorText; adds=$global:AddCount; registration=$global:Registration} |
    ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $Output -Encoding UTF8
""", encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(harness),
                             str(script), str(config), str(output), "yes" if repair else "no"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8-sig"))
    if custom or (not repair and not matching):
        assert not report["ok"] and report["adds"] == 0
        assert report["registration"] == registration
    else:
        assert report["ok"], report["error"]
        assert report["adds"] == (0 if matching else 1)
        final = report["registration"]["transport"]
        assert final["command"] == python.as_posix()
        assert final["env"]["GAMELENS_PROJECT_DIR"] == project.as_posix()
        assert final["env"]["GAMELENS_CLIENTS_FILE"] == str(clients)
