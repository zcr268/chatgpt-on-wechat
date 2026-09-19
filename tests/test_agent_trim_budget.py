"""Regression tests for budget-aware context trimming (#3178).

``_trim_messages()`` used to drop the older half of turns (and fire a
summary LLM call) whenever turn count exceeded ``max_context_turns``,
even when the estimated token total was well under the model budget.
Token budget must be checked first: many short turns that still fit
must be kept without summarizing; over-budget conversations must still
trim.
"""

from types import SimpleNamespace

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.message_utils import identify_complete_turns


class _MemoryManager:
    def __init__(self):
        self.flush_calls = []

    def flush_memory(self, **kwargs):
        self.flush_calls.append(kwargs)


def _make_turn_messages(turn_count):
    messages = []
    for i in range(turn_count):
        messages.append({
            "role": "user",
            "content": [{"type": "text", "text": f"q{i}"}],
        })
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": f"a{i}"}],
        })
    return messages


def _make_executor(*, turn_count, max_context_turns, tokens_per_message, max_tokens):
    memory_manager = _MemoryManager()
    agent = SimpleNamespace(
        memory_manager=memory_manager,
        max_context_tokens=max_tokens,
        _get_model_context_window=lambda: max_tokens,
        _get_output_reserve_tokens=lambda: 0,
        _estimate_message_tokens=lambda message: tokens_per_message,
    )
    executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
    executor.agent = agent
    executor.messages = _make_turn_messages(turn_count)
    executor.system_prompt = "system"
    executor.max_context_turns = max_context_turns
    return executor, memory_manager


def test_many_short_turns_under_budget_do_not_summarize():
    """31 one-token turns fit a 1000-token budget: keep all, no summary."""
    executor, memory_manager = _make_executor(
        turn_count=31,
        max_context_turns=30,
        tokens_per_message=1,
        max_tokens=1000,
    )

    executor._trim_messages()

    assert memory_manager.flush_calls == []
    assert len(identify_complete_turns(executor.messages)) == 31
    assert executor.messages[-1]["content"][0]["text"] == "a30"


def test_over_budget_still_trims():
    """Turns that exceed the token budget must still be discarded."""
    executor, memory_manager = _make_executor(
        turn_count=10,
        max_context_turns=30,
        tokens_per_message=100,
        max_tokens=200,
    )

    executor._trim_messages()

    kept_turns = identify_complete_turns(executor.messages)
    assert len(kept_turns) < 10
    assert len(kept_turns) >= 1
    assert memory_manager.flush_calls, "over-budget trim should flush discarded turns"
    assert executor.messages[-1]["content"][0]["text"] == "a9"
