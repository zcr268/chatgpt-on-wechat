import json
from pathlib import Path

import pytest

from channel.web.openai_api import (
    OpenAIAPIError,
    handle_chat_completions,
)


def _runner(events, calls):
    def run(query, session_id, send_chunk_fn, channel_type="", agent_id=None):
        calls.append(
            {
                "query": query,
                "session_id": session_id,
                "channel_type": channel_type,
                "agent_id": agent_id,
            }
        )
        for event in events:
            send_chunk_fn(event)

    return run


@pytest.mark.parametrize(
    ("configured_token", "authorization", "status_code"),
    [
        ("", "Bearer secret", 503),
        ("secret", "", 401),
        ("secret", "Bearer wrong", 401),
    ],
)
def test_external_api_uses_independent_bearer_auth(
    configured_token, authorization, status_code
):
    with pytest.raises(OpenAIAPIError) as exc_info:
        handle_chat_completions(
            {
                "model": "cowagent",
                "messages": [{"role": "user", "content": "hello"}],
            },
            authorization=authorization,
            external_api_token=configured_token,
            run_chat=lambda *args, **kwargs: None,
        )

    assert exc_info.value.status_code == status_code


def test_non_streaming_completion_maps_content_reasoning_and_tools():
    calls = []
    response = handle_chat_completions(
        {
            "model": "cowagent",
            "conversation_id": "conversation-42",
            "user": "user-7",
            "messages": [
                {"role": "assistant", "content": "previous answer"},
                {"role": "user", "content": "inspect the workspace"},
            ],
        },
        authorization="Bearer secret",
        external_api_token="secret",
        run_chat=_runner(
            [
                {"chunk_type": "reasoning", "delta": "Need a file. "},
                {
                    "chunk_type": "tool_start",
                    "tool": "read",
                    "tool_id": "call-1",
                    "arguments": {"path": "README.md"},
                },
                {
                    "chunk_type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "README.md"},
                            "result": "CowAgent",
                            "status": "success",
                            "elapsed": "0.01s",
                        }
                    ],
                },
                {"chunk_type": "content", "delta": "The workspace is ready."},
            ],
            calls,
        ),
        created=1700000000,
        completion_id="chatcmpl-test",
    )

    assert calls == [
        {
            "query": "inspect the workspace",
            "session_id": "openai:conversation:conversation-42",
            "channel_type": "openai_api",
            "agent_id": None,
        }
    ]
    assert response == {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "cowagent",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "The workspace is ready.",
                    "reasoning_content": "Need a file. ",
                    "tool_trace": [
                        {
                            "type": "tool_start",
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "README.md"},
                        },
                        {
                            "type": "tool_result",
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "README.md"},
                            "result": "CowAgent",
                            "status": "success",
                            "elapsed": "0.01s",
                        },
                    ],
                },
                "finish_reason": "stop",
            }
        ],
    }


def test_streaming_completion_uses_standard_deltas_and_additive_cow_events():
    calls = []
    stream = handle_chat_completions(
        {
            "model": "cowagent",
            "stream": True,
            "user": "user-7",
            "messages": [{"role": "user", "content": "hello"}],
        },
        authorization="Bearer secret",
        external_api_token="secret",
        run_chat=_runner(
            [
                {"chunk_type": "reasoning", "delta": "Think."},
                {
                    "chunk_type": "tool_start",
                    "tool": "search",
                    "tool_id": "call-2",
                    "arguments": {"query": "CowAgent"},
                },
                {"chunk_type": "content", "delta": "Hello"},
                {"chunk_type": "content", "delta": " world"},
            ],
            calls,
        ),
        created=1700000000,
        completion_id="chatcmpl-stream",
    )

    frames = list(stream)
    assert frames[-1] == "data: [DONE]\n\n"
    payloads = [
        json.loads(frame.removeprefix("data: ").removesuffix("\n\n"))
        for frame in frames[:-1]
    ]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert payloads[1]["choices"][0]["delta"] == {"reasoning_content": "Think."}
    assert payloads[1]["cow_event"] == {
        "type": "reasoning",
        "delta": "Think.",
    }
    assert payloads[2]["choices"][0]["delta"] == {}
    assert payloads[2]["cow_event"] == {
        "type": "tool_start",
        "id": "call-2",
        "name": "search",
        "arguments": {"query": "CowAgent"},
    }
    assert [
        payload["choices"][0]["delta"].get("content") for payload in payloads[3:5]
    ] == ["Hello", " world"]
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert calls[0]["session_id"] == "openai:user:user-7"


def test_invalid_messages_return_400():
    with pytest.raises(OpenAIAPIError) as exc_info:
        handle_chat_completions(
            {
                "model": "cowagent",
                "messages": [{"role": "user", "content": [{"type": "text"}]}],
            },
            authorization="Bearer secret",
            external_api_token="secret",
            run_chat=lambda *args, **kwargs: None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "invalid_request"


def test_non_streaming_agent_failure_returns_500():
    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    with pytest.raises(OpenAIAPIError) as exc_info:
        handle_chat_completions(
            {
                "model": "cowagent",
                "messages": [{"role": "user", "content": "hello"}],
            },
            authorization="Bearer secret",
            external_api_token="secret",
            run_chat=fail,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "internal_error"
    assert "provider unavailable" not in exc_info.value.message


def test_route_config_and_english_documentation_expose_the_public_api():
    root = Path(__file__).parents[1]
    web_source = (root / "channel/web/web_channel.py").read_text(encoding="utf-8")
    config = json.loads((root / "config-template.json").read_text(encoding="utf-8"))
    docs = (root / "docs/channels/web.mdx").read_text(encoding="utf-8")

    assert "'/v1/chat/completions', 'OpenAIChatCompletionsHandler'" in web_source
    assert config["external_api_token"] == ""
    assert "POST /v1/chat/completions" in docs
    assert "Authorization: Bearer" in docs
    assert '"stream": true' in docs
