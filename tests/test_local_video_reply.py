# encoding:utf-8
"""Slack and Telegram upload a local ``ReplyType.VIDEO`` instead of posting its path.

``ChatChannel`` hands an agent-produced video over as
``Reply(ReplyType.VIDEO, "file://" + path)``; without a VIDEO branch the reply
fell through to the plain-text fallback and the user got the raw path.
"""
import asyncio
import sys
import types

from bridge.reply import Reply, ReplyType


def test_slack_uploads_a_local_video():
    from channel.slack import slack_channel as mod

    uploads, posts = [], []
    ch = mod.SlackChannel.__wrapped__.__new__(mod.SlackChannel.__wrapped__)
    ch._client = types.SimpleNamespace(
        files_upload_v2=lambda **kw: uploads.append(kw),
        chat_postMessage=lambda **kw: posts.append(kw),
    )

    ch._do_send(Reply(ReplyType.VIDEO, "file:///srv/cow/tmp/clip.mp4"), "C1", "1.0")

    assert posts == []
    assert uploads[0]["file"] == "/srv/cow/tmp/clip.mp4"


def test_telegram_sends_a_local_video_as_video(monkeypatch, tmp_path):
    from channel.telegram import telegram_channel as mod

    telegram = types.ModuleType("telegram")
    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = types.SimpleNamespace(HTML="HTML")
    error = types.ModuleType("telegram.error")
    for name in ("BadRequest", "NetworkError", "TimedOut"):
        setattr(error, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants)
    monkeypatch.setitem(sys.modules, "telegram.error", error)

    calls = []

    class Bot:
        async def send_video(self, **kw):
            calls.append(("video", kw["video"].name))

        async def send_document(self, **kw):
            calls.append(("document", kw["document"].name))

        async def send_message(self, **kw):
            calls.append(("message", kw["text"]))

    clip = tmp_path / "clip"
    clip.write_bytes(b"video")
    ch = mod.TelegramChannel.__wrapped__.__new__(mod.TelegramChannel.__wrapped__)
    ch._bot = Bot()

    asyncio.run(ch._async_send(Reply(ReplyType.VIDEO, f"file://{clip}"), 7, None))

    assert calls == [("video", str(clip))]
