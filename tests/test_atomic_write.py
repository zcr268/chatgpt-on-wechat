import json
import os
import stat

import pytest

from common import atomic_write
from common.atomic_write import write_json_atomic, write_text_atomic


def test_text_write_replaces_the_file(tmp_path):
    path = tmp_path / "MEMORY.md"
    path.write_text("old", encoding="utf-8")

    write_text_atomic(path, "新内容\n")

    assert path.read_text(encoding="utf-8") == "新内容\n"
    assert [p.name for p in tmp_path.iterdir()] == ["MEMORY.md"]


def test_json_write_creates_the_file(tmp_path):
    path = tmp_path / "config.json"

    write_json_atomic(str(path), {"name": "小牛"})

    assert path.read_text(encoding="utf-8") == '{\n    "name": "小牛"\n}'


def test_a_failed_write_keeps_the_old_file_and_no_temp(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}', encoding="utf-8")

    with pytest.raises(TypeError):
        write_json_atomic(path, {"bad": object()})

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_temp_file_is_a_dot_prefixed_sibling(tmp_path, monkeypatch):
    path = tmp_path / "skills_config.json"
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(src)
        real_replace(src, dst)

    monkeypatch.setattr(atomic_write.os, "replace", spy)
    write_json_atomic(path, {})

    tmp_name = os.path.basename(seen[0])
    assert os.path.dirname(seen[0]) == str(tmp_path)
    assert tmp_name.startswith(".skills_config.json.") and tmp_name.endswith(".tmp")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_permissions_are_kept(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o600)

    write_json_atomic(path, {"token": "x"})

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
