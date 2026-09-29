"""Inbound Slack files with the same name must keep their own bytes."""

from pathlib import Path
from unittest.mock import patch

from channel.slack.slack_channel import SlackChannel
from channel.slack.slack_message import SlackMessage


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.body


def test_same_named_files_do_not_overwrite_earlier_message(tmp_path):
    channel = SlackChannel.__wrapped__.__new__(SlackChannel.__wrapped__)
    channel.bot_token = "test-token"
    first_response = FakeResponse(b"first user")
    second_response = FakeResponse(b"second user")

    with patch.object(SlackMessage, "get_tmp_dir", return_value=str(tmp_path)):
        with patch(
            "channel.slack.slack_channel.requests.get",
            side_effect=[first_response, second_response],
        ):
            first = channel._download_file("https://files.slack.test/first", "report.pdf")
            second = channel._download_file("https://files.slack.test/second", "report.pdf")

    assert first != second
    assert Path(first).parent == tmp_path
    assert Path(second).parent == tmp_path
    assert Path(first).suffix == ".pdf"
    assert Path(second).suffix == ".pdf"
    assert Path(first).read_bytes() == b"first user"
    assert Path(second).read_bytes() == b"second user"
    assert first_response.closed and second_response.closed


def test_interrupted_download_removes_its_partial_file(tmp_path):
    class InterruptedResponse(FakeResponse):
        def iter_content(self, chunk_size):
            yield b"partial"
            raise OSError("connection lost")

    channel = SlackChannel.__wrapped__.__new__(SlackChannel.__wrapped__)
    channel.bot_token = "test-token"
    response = InterruptedResponse(b"partial")
    with patch.object(SlackMessage, "get_tmp_dir", return_value=str(tmp_path)):
        with patch("channel.slack.slack_channel.requests.get", return_value=response):
            assert channel._download_file("https://files.slack.test/file", "report.pdf") is None

    assert list(tmp_path.iterdir()) == []
    assert response.closed
