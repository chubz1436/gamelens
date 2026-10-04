"""Bounded local file reads and lazy selected-port agent credential access.

No GameLens imports, credential publishing, directory creation or fallback to
operator/shared/plaintext credentials. Importing this module does not load DPAPI.
"""
import contextlib
import importlib
import os
from pathlib import Path
import stat

MAGIC = b"GameLens-DPAPI-v1\0"
ENTROPY = b"GameLens-agent-session"
MAX_FILE_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096


class FileBoundaryError(Exception):
    """args[0] is a fixed reason code, never a path or OS exception."""


class SessionError(Exception):
    """args[0] is a fixed credential diagnostic, never credential contents."""


def _local_drive(path):
    if os.name != "nt":
        return
    # A drive letter may map to SMB. This read-only OS query avoids opening it.
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_type = kernel.GetDriveTypeW
    get_type.argtypes = [ctypes.c_wchar_p]
    get_type.restype = ctypes.c_uint
    if get_type(path.anchor) not in (2, 3, 6):  # removable, fixed, RAM disk
        raise FileBoundaryError("FILE_LOCATION_UNSUPPORTED")


def local_path(value):
    try:
        text = os.fspath(value)
        if not isinstance(text, str) or not text or "\0" in text:
            raise FileBoundaryError("FILE_LOCATION_UNSUPPORTED")
        if text.startswith(("\\\\", "//")):
            raise FileBoundaryError("FILE_LOCATION_UNSUPPORTED")
        path = Path(text)
        if not path.is_absolute() or ".." in path.parts:
            raise FileBoundaryError("FILE_LOCATION_UNSUPPORTED")
        _local_drive(path)
        return path
    except FileBoundaryError:
        raise
    except Exception:
        raise FileBoundaryError("FILE_LOCATION_UNSUPPORTED") from None


def _checked(path, directory=False):
    path = local_path(path)
    try:
        for parent in reversed(path.parents):
            info = parent.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 1024
                    or not stat.S_ISDIR(info.st_mode)):
                raise FileBoundaryError("FILE_LOCATION_UNSUPPORTED")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise FileBoundaryError("FILE_LOCATION_UNSUPPORTED")
        predicate = stat.S_ISDIR if directory else stat.S_ISREG
        if not predicate(info.st_mode):
            raise FileBoundaryError("FILE_TYPE_UNSUPPORTED")
        return path, info
    except FileBoundaryError:
        raise
    except FileNotFoundError:
        raise FileBoundaryError("FILE_MISSING") from None
    except OSError:
        raise FileBoundaryError("FILE_UNREADABLE") from None


def safe_directory(path):
    return _checked(path, directory=True)[0]


def read_local_bytes(path, limit=MAX_FILE_BYTES):
    """Read a named regular local file without following symlinks/reparse points.

    Check the descriptor identity and path again after reading. This is not a
    filesystem sandbox against an administrator racing directory replacement.
    """
    if not 0 < limit <= MAX_FILE_BYTES:
        raise FileBoundaryError("FILE_LIMIT_INVALID")
    path, before = _checked(path)
    if before.st_size > limit:
        raise FileBoundaryError("FILE_TOO_LARGE")
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise FileBoundaryError("FILE_CHANGED_DURING_READ")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(4096, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise FileBoundaryError("FILE_TOO_LARGE")
        _, after = _checked(path)
        final = os.fstat(fd)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if signature(before) != signature(final) or signature(final) != signature(after):
            raise FileBoundaryError("FILE_CHANGED_DURING_READ")
        return bytes(raw)
    except FileBoundaryError:
        raise
    except OSError:
        raise FileBoundaryError("FILE_UNREADABLE") from None
    finally:
        if fd is not None:
            os.close(fd)


def selected_agent_path(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise SessionError("SESSION_PORT_INVALID")
    # tempfile.gettempdir() can write a probe file. Do not call it or guess its
    # fallback location. Missing LOCALAPPDATA leaves this location unverified.
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise SessionError("SESSION_LOCATION_UNVERIFIED")
    try:
        return local_path(base) / "GameLens" / ("agent-%d.token" % port)
    except FileBoundaryError:
        raise SessionError("SESSION_LOCATION_UNSUPPORTED") from None


class _Discard:
    def write(self, text):
        return len(text)

    def flush(self):
        pass


def _load_dpapi():
    # Neither stdout nor stderr emitted during Python-level import belongs in
    # a diagnostic report. No package is imported until --connect is explicit.
    with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
        return importlib.import_module("win32crypt")


def read_selected_agent(port):
    path = selected_agent_path(port)
    try:
        raw = read_local_bytes(path)
    except FileBoundaryError as exc:
        reason = {"FILE_MISSING": "SESSION_TOKEN_MISSING",
                  "FILE_TOO_LARGE": "SESSION_TOKEN_TOO_LARGE"}.get(
                      exc.args[0], "SESSION_TOKEN_UNREADABLE")
        raise SessionError(reason) from None
    if not raw.startswith(MAGIC) or len(raw) <= len(MAGIC):
        raise SessionError("SESSION_FORMAT_UNSUPPORTED")
    try:
        dpapi = _load_dpapi()
    except Exception:
        raise SessionError("SESSION_DECRYPTION_UNAVAILABLE") from None
    try:
        with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
            _, clear = dpapi.CryptUnprotectData(raw[len(MAGIC):], ENTROPY, None, None, 1)
        if not isinstance(clear, bytes) or not 1 <= len(clear) <= MAX_TOKEN_BYTES:
            raise SessionError("SESSION_TOKEN_INVALID")
        token = clear.decode("ascii").strip()
        if not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise SessionError("SESSION_TOKEN_INVALID")
        return token
    except SessionError:
        raise
    except Exception:
        raise SessionError("SESSION_DECRYPTION_FAILED") from None
