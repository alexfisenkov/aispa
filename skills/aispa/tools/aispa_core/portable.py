"""Portable relative names and link detection, independent of journal policy."""
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat


_RESERVED = re.compile(r"^(?:con|conin\$|conout\$|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.I)


def relative_name(value, *, root=False):
    """Use one POSIX spelling on disk/JSON, rejecting Windows alias/device names."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Нужен относительный путь с разделителем /")
    windows = PureWindowsPath(value)
    path = PurePosixPath(value)
    if windows.drive or windows.root or path.is_absolute() or ".." in path.parts:
        raise ValueError("Абсолютные пути, диски и переходы .. запрещены")
    if not path.parts:
        if root:
            return "."
        raise ValueError("Пустой путь запрещён")
    for part in path.parts:
        if (part.endswith((".", " ")) or _RESERVED.match(part)
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)):
            raise ValueError("Путь содержит зарезервированное имя или символ Windows")
    return path.as_posix()


def is_link(path):
    """Detect symlinks and name-surrogate reparse points, including junctions.

    Hydrated cloud/compression reparse files do not redirect names and are not
    rejected merely for being cloud-backed. Unknown reparse metadata fails closed.
    """
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & 0x400:
        tag = getattr(info, "st_reparse_tag", 0)
        return not tag or bool(tag & 0x20000000)
    return False


def executable(path, git_mode=None):
    """NTFS has no portable POSIX execute bit; use a known Git mode if supplied."""
    if os.name == "nt":
        return git_mode in ("100755", b"100755") if git_mode is not None else False
    return bool(Path(path).stat().st_mode & 0o111)


def match_name(value):
    return value.casefold() if os.name == "nt" else value
