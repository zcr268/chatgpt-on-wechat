"""Fallback chat model: when it engages, and when it must stay out of the way.

The fallback is opt-in and only takes over once a turn has failed for good, so
these tests pin down both directions — that a dead provider hands over to the
backup, and that a healthy one is never disturbed. The failure modes worth
guarding are the quiet ones: a half-configured entry silently answering for a
working model, or a turn ping-ponging between two broken ones.
"""

import pytest

from bridge.agent_bridge import AgentLLMModel


def _model(monkeypatch, chat_fallback, **extra_conf):
    """An AgentLLMModel with a stubbed config (no bridge, no real bot)."""
    conf = {"model": "primary-model", "chat_fallback": chat_fallback}
    conf.update(extra_conf)
    monkeypatch.setattr("bridge.agent_bridge.conf", lambda: conf, raising=False)
    return AgentLLMModel.__new__(AgentLLMModel)


FALLBACK = {
    "enabled": True,
    "provider": "openai",
    "model": "backup-model",
    "max_switches": 1,
}


class TestFallbackConfig:
    """A malformed or disabled entry must never be usable."""

    def test_a_disabled_entry_is_not_available(self, monkeypatch):
        model = _model(monkeypatch, dict(FALLBACK, enabled=False))
        assert model.fallback_available() is False

    def test_a_missing_entry_is_not_available(self, monkeypatch):
        model = _model(monkeypatch, None)
        assert model.fallback_available() is False

    def test_a_non_dict_entry_is_not_available(self, monkeypatch):
        model = _model(monkeypatch, "openai/backup-model")
        assert model.fallback_available() is False

    @pytest.mark.parametrize(
        "field",
        ["provider", "model"],
    )
    def test_a_half_configured_entry_is_not_available(self, monkeypatch, field):
        """Half an entry is 'off': a fallback without a provider or a model
        could hijack a perfectly healthy primary."""
        model = _model(monkeypatch, dict(FALLBACK, **{field: ""}))
        assert model.fallback_available() is False

    def test_a_garbage_max_switches_does_not_raise(self, monkeypatch):
        model = _model(monkeypatch, dict(FALLBACK, max_switches="soon"))
        assert model.fallback_config()["max_switches"] == 0


class TestEngagingTheFallback:
    def test_the_answer_comes_from_the_fallback_once_engaged(self, monkeypatch):
        model = _model(monkeypatch, FALLBACK)
        assert model.model == "primary-model"

        assert model.use_fallback() is True
        assert model.model == "backup-model"

    def test_routing_follows_the_fallback_provider(self, monkeypatch):
        """The bot type has to change too, or the request would go back to the
        provider that just failed."""
        model = _model(monkeypatch, dict(FALLBACK, provider="qianfan"))
        model.use_fallback()
        assert model._resolve_bot_type("backup-model") == "qianfan"

    def test_openai_routes_through_the_compatible_bot(self, monkeypatch):
        """Same mapping the models console persists for the primary model."""
        model = _model(monkeypatch, FALLBACK)
        model.use_fallback()
        assert model._resolve_bot_type("backup-model") == "chatGPT"

    def test_it_outranks_a_session_override(self, monkeypatch):
        """A user's per-conversation pick shouldn't survive their own provider
        going down — the fallback exists precisely to leave it."""
        model = _model(monkeypatch, FALLBACK)
        model.set_session_override("deepseek", "pinned-model")
        model.use_fallback()
        assert model.model == "backup-model"

    def test_it_engages_only_once_per_turn(self, monkeypatch):
        """Already on the fallback, so a second failure must not loop back."""
        model = _model(monkeypatch, FALLBACK)
        assert model.use_fallback() is True
        assert model.use_fallback() is False

    def test_disabled_fallback_returns_false(self, monkeypatch):
        model = _model(monkeypatch, dict(FALLBACK, enabled=False))
        assert model.use_fallback() is False
        assert model.model == "primary-model"


class TestResettingBetweenTurns:
    def test_a_new_turn_starts_on_the_primary_again(self, monkeypatch):
        """Scoping the switch to one turn keeps a transient outage from
        quietly downgrading the rest of the conversation."""
        model = _model(monkeypatch, FALLBACK)
        model.use_fallback()
        model.reset_fallback()
        assert model.model == "primary-model"

    def test_reset_lets_the_fallback_engage_again_later(self, monkeypatch):
        model = _model(monkeypatch, FALLBACK)
        model.use_fallback()
        model.reset_fallback()
        assert model.fallback_available() is True
        assert model.use_fallback() is True

    def test_reset_on_a_healthy_model_is_a_no_op(self, monkeypatch):
        model = _model(monkeypatch, FALLBACK)
        model.reset_fallback()
        assert model.model == "primary-model"


class TestStreamRecovery:
    """The executor only asks to switch once retries are exhausted."""

    def test_a_plain_model_is_never_asked_to_switch(self):
        """Doubles and plain LLMModels have no use_fallback; the turn must
        still fail normally instead of erroring on a missing method."""
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
        executor.model = object()
        assert executor._switch_to_fallback("boom") is False

    def test_a_failing_switch_does_not_break_the_turn(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class Broken:
            def use_fallback(self):
                raise RuntimeError("config is unreadable")

        executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
        executor.model = Broken()
        assert executor._switch_to_fallback("boom") is False

    def test_reset_is_a_no_op_without_support(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
        executor.model = object()
        executor._reset_model_fallback()  # must not raise

    def test_the_switch_delegates_to_the_model(self, monkeypatch):
        from agent.protocol.agent_stream import AgentStreamExecutor

        calls = []

        class Fake:
            def use_fallback(self):
                calls.append(True)
                return True

        executor = AgentStreamExecutor.__new__(AgentStreamExecutor)
        executor.model = Fake()
        assert executor._switch_to_fallback("provider is down") is True
        assert calls == [True]


class TestConsoleValidation:
    """The models console must not persist an entry that can't do its job."""

    def _handler(self):
        from channel.web.web_channel import ModelsHandler

        return ModelsHandler.__new__(ModelsHandler)

    def test_enabling_without_a_model_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "channel.web.web_channel.conf", lambda: {"model": "primary-model"}
        )
        result = self._handler()._set_chat_fallback("openai", "", True)
        assert "error" in result

    def test_enabling_without_a_provider_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "channel.web.web_channel.conf", lambda: {"model": "primary-model"}
        )
        result = self._handler()._set_chat_fallback("", "backup-model", True)
        assert "error" in result

    def test_disabling_a_broken_entry_is_always_allowed(self, monkeypatch):
        """Turning it off is the safe direction — never block the user."""
        written = {}
        monkeypatch.setattr(
            "channel.web.web_channel.conf", lambda: {"model": "primary-model"}
        )
        monkeypatch.setattr(
            self._handler().__class__, "_read_file_config", lambda self: {}
        )
        handler = self._handler()
        monkeypatch.setattr(
            handler, "_write_file_config", lambda cfg: written.update(cfg)
        )
        result = handler._set_chat_fallback("", "", False)
        assert '"status": "success"' in result
        assert written["chat_fallback"]["enabled"] is False

    def test_an_unknown_provider_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "channel.web.web_channel.conf", lambda: {"model": "primary-model"}
        )
        result = self._handler()._set_chat_fallback("not-a-vendor", "m", True)
        assert "error" in result

    def test_max_switches_is_clamped(self, monkeypatch):
        """0 would mean 'never engage'; 99 would risk unbounded ping-pong."""
        written = {}
        monkeypatch.setattr(
            "channel.web.web_channel.conf", lambda: {"model": "primary-model"}
        )
        handler = self._handler()
        monkeypatch.setattr(handler, "_read_file_config", lambda: {})
        monkeypatch.setattr(
            handler, "_write_file_config", lambda cfg: written.update(cfg)
        )
        handler._set_chat_fallback("openai", "backup-model", True, max_switches=0)
        assert written["chat_fallback"]["max_switches"] == 1
        handler._set_chat_fallback("openai", "backup-model", True, max_switches=99)
        assert written["chat_fallback"]["max_switches"] == 5

    def test_a_valid_entry_is_persisted(self, monkeypatch):
        written = {}
        monkeypatch.setattr(
            "channel.web.web_channel.conf", lambda: {"model": "primary-model"}
        )
        handler = self._handler()
        monkeypatch.setattr(handler, "_read_file_config", lambda: {})
        monkeypatch.setattr(
            handler, "_write_file_config", lambda cfg: written.update(cfg)
        )
        handler._set_chat_fallback("openai", "backup-model", True)
        assert written["chat_fallback"] == {
            "enabled": True,
            "provider": "openai",
            "model": "backup-model",
            "max_switches": 1,
        }
