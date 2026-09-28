"""Feishu image events must not accept unbounded or partial downloads."""

import json
from pathlib import Path

from channel.feishu import feishu_message


class FakeResponse:
    def __init__(self, chunks, status_code=200, headers=None):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"".join(chunks)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def iter_content(self, chunk_size):
        yield from self.chunks


def _event(message_type, content):
    return {
        "app_id": "bot",
        "sender": {"sender_id": {"open_id": "user"}},
        "message": {
            "message_id": "message-1",
            "chat_id": "chat-1",
            "message_type": message_type,
            "content": json.dumps(content),
        },
    }


def test_large_single_image_is_rejected_without_leaving_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_message.state_dir, "tmp_dir", lambda: tmp_path)
    monkeypatch.setattr(feishu_message, "MAX_FEISHU_IMAGE_BYTES", 5, raising=False)
    response = FakeResponse([b"abc", b"def"])
    monkeypatch.setattr(feishu_message.requests, "get", lambda **kwargs: response)

    message = feishu_message.FeishuMessage(
        _event("image", {"image_key": "image-1"}), access_token="tenant-token"
    )

    assert message.image_path is None
    assert "download" in message.content.lower() or "下载失败" in message.content
    assert list(tmp_path.iterdir()) == []
    assert response.closed


def test_large_post_image_does_not_publish_a_missing_path(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_message.state_dir, "tmp_dir", lambda: tmp_path)
    monkeypatch.setattr(feishu_message, "MAX_FEISHU_IMAGE_BYTES", 5, raising=False)
    response = FakeResponse([b"abcdef"], headers={"Content-Length": "6"})
    monkeypatch.setattr(feishu_message.requests, "get", lambda **kwargs: response)

    post = {"content": [[{"tag": "text", "text": "hello"}, {"tag": "img", "image_key": "image-1"}]]}
    message = feishu_message.FeishuMessage(_event("post", post), access_token="tenant-token")

    assert message.content == "hello"
    assert message.image_paths == {}
    assert list(tmp_path.iterdir()) == []
    assert response.closed


def test_small_image_streams_to_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_message.state_dir, "tmp_dir", lambda: tmp_path)
    response = FakeResponse([b"abc", b"de"])
    calls = []

    def get(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(feishu_message.requests, "get", get)
    message = feishu_message.FeishuMessage(
        _event("image", {"image_key": "image-1"}), access_token="tenant-token"
    )

    assert Path(message.image_path).read_bytes() == b"abcde"
    assert calls[0]["stream"] is True
    assert calls[0]["timeout"]
    assert response.closed


def test_post_uses_each_successful_image_once(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_message.state_dir, "tmp_dir", lambda: tmp_path)
    response = FakeResponse([b"image-bytes"])
    calls = []

    def get(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(feishu_message.requests, "get", get)
    post = {"content": [[{"tag": "img", "image_key": "image-1"}, {"tag": "img", "image_key": "image-1"}]]}
    message = feishu_message.FeishuMessage(_event("post", post), access_token="tenant-token")

    assert len(calls) == 1
    assert list(message.image_paths) == ["image-1"]
    assert message.content.count("[图片:") == 1
    assert Path(message.image_paths["image-1"]).read_bytes() == b"image-bytes"


def test_interrupted_image_stream_cleans_up_partial_file(tmp_path, monkeypatch):
    class InterruptedResponse(FakeResponse):
        def iter_content(self, chunk_size):
            yield b"partial"
            raise OSError("connection lost")

    monkeypatch.setattr(feishu_message.state_dir, "tmp_dir", lambda: tmp_path)
    response = InterruptedResponse([b"partial"])
    monkeypatch.setattr(feishu_message.requests, "get", lambda **kwargs: response)
    message = feishu_message.FeishuMessage(
        _event("image", {"image_key": "image-1"}), access_token="tenant-token"
    )

    assert message.image_path is None
    assert list(tmp_path.iterdir()) == []
    assert response.closed
