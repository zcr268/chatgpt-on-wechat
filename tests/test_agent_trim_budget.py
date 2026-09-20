"""Regression tests for budget-aware context trimming."""

from types import SimpleNamespace

from agent.protocol.agent_stream import AgentStreamExecutor


class _MemoryManager:
    def __init__(self):
        self.flush_calls = []

    def flush_memory(self, **kwargs):
        self.flush_calls.append(kwargs)


def _make_executor(*, turn_count, max_context_turns, estimated_tokens, max_tokens):
    memory_manager = _MemoryManager()
    agent = SimpleNamespace(
        memory_manager=memory_manager,
        max_context_turns=max_context_turns,
        max_context_tokens=max_tokens,
        _get_model_context_window=lambda: max_tokens,
        _get_output_reserve_tokens=lambda: 0,
        _estimate_message_tokens=lambda message: 0,
    )
    executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
    executor.agent = agent
    executor.messages = [{"role": "user", "content": "existing"}]
    executor.system_prompt = "system"
    executor.max_context_turns = max_context_turns
    turns = [
        {"messages": [{"role": "user", "content": f"turn-{i}"}]}
        for i in range(turn_count)
    ]
    executor._truncate_historical_tool_results = lambda: None
    executor._identify_complete_turns = lambda: turns
    executor._estimate_turn_tokens = lambda turn: estimated_tokens
    return executor, memory_manager


def test_under_budget_within_turn_cap_keeps_everything():
    # Many short turns that are BOTH under the token budget AND within the
    # turn cap: nothing is trimmed and no summary LLM call fires.
    executor, memory_manager = _make_executor(
        turn_count=30,
        max_context_turns=30,
        estimated_tokens=1,
        max_tokens=1000,
    )

    executor._trim_messages()

    assert memory_manager.flush_calls == []
    assert len(executor.messages) == 30


def test_turn_cap_still_trims_even_when_under_token_budget():
    # 31 tiny turns fit the token budget but exceed the turn cap. The turn cap
    # is an explicit cost limit (AND semantics), so history is trimmed to the
    # cap and a single summary flush fires for the discarded turn.
    executor, memory_manager = _make_executor(
        turn_count=31,
        max_context_turns=30,
        estimated_tokens=1,
        max_tokens=1000,
    )

    executor._trim_messages()

    assert len(memory_manager.flush_calls) == 1
    assert len(executor.messages) == 30
