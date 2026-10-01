"""Resolve the installed GameLens companion regardless of the client's cwd."""
import os
from pathlib import Path
import sys

project = Path(os.environ.get("GAMELENS_PROJECT_DIR", "B:/AI_Agent_folder/GAME VIDEO"))
if not (project / "gamelens" / "mcp.py").is_file():
    print("GameLens project missing; reinstall the companion plugin for its new location.", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(project.resolve()))

if __name__ == "__main__":
    clients_file = os.environ.get("GAMELENS_CLIENTS_FILE")
    if clients_file:
        from gamelens.multi_mcp import main
        raise SystemExit(main(["--clients-file", clients_file]))
    else:
        from gamelens.mcp import main
        raise SystemExit(main())
