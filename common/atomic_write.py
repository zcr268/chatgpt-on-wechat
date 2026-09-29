"""Replace a file's contents without ever leaving it half-written.

Writing straight into a file truncates it before the new bytes exist, so a
crash, a kill or a full disk in between leaves a partial document that most
stores here cannot parse back. These helpers write a sibling first and rename
it over the target, so readers see either the old file or the new one.

The sibling is dot-prefixed (workspace watchers skip it) and unique per call,
so overlapping writers never rename each other's partial file.
"""

import json
import os
import stat
import uuid


def write_text_atomic(path, text: str, encoding: str = "utf-8") -> None:
    _replace(path, lambda f: f.write(text), encoding)


def write_json_atomic(path, data, indent: int = 4, ensure_ascii: bool = False) -> None:
    _replace(path, lambda f: json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii))


def _replace(path, write, encoding: str = "utf-8") -> None:
    path = os.fspath(path)
    directory, name = os.path.split(os.path.abspath(path))
    tmp_path = os.path.join(directory, f".{name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        _copy_mode(path, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _copy_mode(src: str, dst: str) -> None:
    """Keep the target's permissions, e.g. 0600 on a credentials file."""
    try:
        os.chmod(dst, stat.S_IMODE(os.stat(src).st_mode))
    except OSError:
        pass
