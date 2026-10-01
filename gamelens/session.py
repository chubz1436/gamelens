"""Local session handoff: only the agent capability, encrypted for this user.

The operator capability stays on the console. Codex reads this stable path on
each request, so a capture restart does not require editing MCP configuration.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

MAGIC = b"GameLens-DPAPI-v1\0"
ENTROPY = b"GameLens-agent-session"
OPERATOR_MAGIC = b"GameLens-operator-DPAPI-v1\0"
OPERATOR_ENTROPY = b"GameLens-desktop-operator-session"


def agent_token_path(port: int = 8777) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return base / "GameLens" / f"agent-{port}.token"


def operator_token_path(port: int = 8777) -> Path:
    return agent_token_path(port).with_name(f"operator-{port}.token")


def publish_operator_token(path: Path, token: str) -> None:
    """Native desktop login for this Windows user; never used by the MCP client."""
    import win32crypt
    sealed = win32crypt.CryptProtectData(
        token.encode("utf-8"), "GameLens desktop operator", OPERATOR_ENTROPY, None, None, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".operator-", delete=False) as f:
            temp_path = Path(f.name)
            f.write(OPERATOR_MAGIC + sealed)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def read_operator_token(path: Path) -> str:
    import win32crypt
    data = path.read_bytes()
    if not data.startswith(OPERATOR_MAGIC):
        raise ValueError("desktop operator handoff must be Windows-encrypted")
    _, clear = win32crypt.CryptUnprotectData(
        data[len(OPERATOR_MAGIC):], OPERATOR_ENTROPY, None, None, 1)
    token = clear.decode("utf-8").strip()
    if not token:
        raise ValueError("empty desktop operator handoff")
    return token


def clear_operator_token(path: Path, token: str) -> None:
    try:
        if read_operator_token(path) == token:
            path.unlink()
    except Exception:
        pass


def publish_agent_token(path: Path, token: str) -> None:
    import win32crypt

    sealed = win32crypt.CryptProtectData(
        token.encode("utf-8"), "GameLens agent session", ENTROPY, None, None, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".agent-", delete=False) as f:
            temp_path = Path(f.name)
            f.write(MAGIC + sealed)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def read_agent_token(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(MAGIC):
        import win32crypt

        _, data = win32crypt.CryptUnprotectData(data[len(MAGIC):], ENTROPY, None, None, 1)
    # Explicit --token-file callers may still provide the original plaintext format.
    token = data.decode("utf-8").strip()
    if not token:
        raise ValueError("agent token file is empty")
    return token


def clear_agent_token(path: Path, token: str) -> None:
    """Do not remove a credential that a newer session has replaced."""
    try:
        if read_agent_token(path) == token:
            path.unlink()
    except Exception:
        pass
