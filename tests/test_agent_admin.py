import json
from pathlib import Path

import pytest

from agent.admin import (
    AgentAdminError,
    AgentAdminService,
    StaleAgentFileError,
    StaleRosterError,
)
from agent import team
from agent.registry import AgentRegistry, set_agent_registry


def _pin(settings):
    """Point state_dir at this test's config instead of the developer's own.

    Without this, resolving a shared directory falls through to the real
    ``agent_workspace`` and the test scaffolds skills into it.
    """
    set_agent_registry(AgentRegistry.from_config(team.resolve(settings)))


def _saved(root):
    """The roster as the app reads it, wherever it is being kept."""
    return team.read({"agent_workspace": str(root)})


@pytest.fixture
def admin(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    settings = {
        "agent_workspace": str(tmp_path),
        "default_agent_id": "primary",
        "agents": [
            {
                "id": "primary",
                "name": "Primary",
                "workspace": str(primary),
                "enabled": True,
            }
        ],
        "agent_bindings": [],
        "unrelated_setting": "preserved",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    _pin(settings)
    try:
        yield AgentAdminService(str(config_path)), tmp_path, config_path
    finally:
        set_agent_registry(None)


def test_create_agent_bootstraps_persona_without_forking_shared_assets(admin):
    service, root, config_path = admin
    workspace = root / "research"

    created = service.create_agent("research", "Research", str(workspace))

    assert created["workspace"] == str(workspace.resolve())
    for filename in ("AGENT.md", "USER.md", "RULE.md", "MEMORY.md", "BOOTSTRAP.md"):
        assert (workspace / filename).is_file()
    assert (workspace / "scheduler").is_dir()
    # The shared assets must NOT be materialised here: an Agent opts out of the
    # shared copy by having its own directory, so creating these empty would
    # leave the new Agent with no skills and no knowledge at all.
    for dirname in ("skills", "knowledge"):
        assert not (workspace / dirname).exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["unrelated_setting"] == "preserved"
    # The roster left config.json entirely rather than being duplicated there.
    assert "agents" not in config
    assert [item["id"] for item in _saved(root)["agents"]] == ["primary", "research"]


def test_new_agent_reads_the_installed_shared_skills(admin):
    service, root, _ = admin
    (root / "primary" / "skills" / "web-search").mkdir(parents=True)

    service.create_agent("research", "Research", str(root / "research"))

    from common.runtime_identity import RuntimeIdentity
    from common import state_dir

    _pin({"agent_workspace": str(root)})
    resolved = state_dir.skills_dir(RuntimeIdentity(agent_id="research"))
    assert resolved == root / "primary" / "skills"
    assert [p.name for p in resolved.iterdir()] == ["web-search"]


def test_workspace_defaults_to_the_derived_per_agent_location(admin):
    service, root, _ = admin

    created = service.create_agent("sales", "Sales")

    assert created["workspace"] == str((root / "agents" / "sales").resolve())
    assert (root / "agents" / "sales" / "AGENT.md").is_file()


def test_clone_copies_persona_but_not_secrets_history_or_nested_agents(admin):
    service, root, _ = admin
    source = root / "primary"
    (source / "AGENT.md").write_text("# Primary persona", encoding="utf-8")
    (source / "MEMORY.md").write_text("what I learned about my user", encoding="utf-8")
    (source / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
    (source / "memory" / "long-term").mkdir(parents=True)
    (source / "memory" / "long-term" / "index.db").write_text("history", encoding="utf-8")

    clone = root / "clone"
    service.create_agent("clone", "Clone", str(clone), clone_from="primary")

    assert (clone / "AGENT.md").read_text(encoding="utf-8") == "# Primary persona"
    assert not (clone / ".env").exists()
    assert not (clone / "memory" / "long-term" / "index.db").exists()
    # MEMORY.md is scaffolded from the template, not inherited from the source.
    assert "what I learned about my user" not in (clone / "MEMORY.md").read_text(
        encoding="utf-8"
    )


def test_cloning_the_default_agent_into_its_own_subtree_terminates(tmp_path):
    """A whole-tree copy here recursed until the path length was rejected.

    On the default layout the first Agent's workspace *is* the instance root, so
    every later Agent lands inside it and cloning the first one means copying a
    directory into a directory beneath itself. Asking for a copy of the Agent
    you already have is the most ordinary thing a user can do.
    """
    settings = {"agent_workspace": str(tmp_path)}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(settings), encoding="utf-8")
    _pin(settings)
    try:
        service = AgentAdminService(str(config_path))
        (tmp_path / "AGENT.md").write_text("# Root persona", encoding="utf-8")
        (tmp_path / "skills" / "web-search").mkdir(parents=True)
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")

        created = service.create_agent("assistant", "Assistant", clone_from="default")

        nested = tmp_path / "agents" / "assistant"
        assert created["workspace"] == str(nested.resolve())
        assert (nested / "AGENT.md").read_text(encoding="utf-8") == "# Root persona"
        assert not (nested / "agents").exists()
        assert not (nested / "skills").exists()
        assert not (nested / ".env").exists()
    finally:
        set_agent_registry(None)


def test_workspace_overlapping_another_agent_is_rejected(admin):
    service, root, config_path = admin
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(AgentAdminError):
        service.create_agent("nested", "Nested", str(root / "primary" / "inside"))
    with pytest.raises(AgentAdminError):
        service.create_agent("outer", "Outer", str(root))

    assert config_path.read_text(encoding="utf-8") == before


def test_archive_disables_profile_without_deleting_workspace(admin):
    service, root, _ = admin
    workspace = root / "research"
    service.create_agent("research", "Research", str(workspace))

    archived = service.archive_agent("research")

    assert archived["enabled"] is False
    assert workspace.is_dir()
    assert next(
        item for item in service.snapshot()["agents"] if item["id"] == "research"
    )["enabled"] is False


def test_default_agent_cannot_be_archived(admin):
    service, _, _ = admin
    with pytest.raises(Exception):
        service.archive_agent("primary")


def test_delete_agent_removes_roster_entry_and_own_workspace(admin):
    service, root, _ = admin
    workspace = root / "agents" / "research"
    service.create_agent("research", "Research")
    assert workspace.is_dir()

    result = service.delete_agent("research")

    assert result["deleted"] is True
    assert [item["id"] for item in service.snapshot()["agents"]] == ["primary"]
    assert not workspace.exists()


def test_delete_agent_prunes_bindings_pointing_at_it(admin):
    service, root, _ = admin
    service.create_agent("research", "Research")
    service.set_binding("research", "feishu", "chat-1")

    service.delete_agent("research")

    bindings = _saved(root).get("agent_bindings") or []
    assert all(b.get("agent_id") != "research" for b in bindings)


def test_default_agent_cannot_be_deleted(admin):
    service, _, _ = admin
    with pytest.raises(Exception):
        service.delete_agent("primary")


def test_core_file_write_is_allowlisted_atomic_and_revision_guarded(admin):
    service, root, _ = admin
    workspace = root / "research"
    service.create_agent("research", "Research", str(workspace))
    original = service.read_core_file("research", "AGENT.md")

    saved = service.write_core_file(
        "research", "AGENT.md", "# Updated persona\n", original["revision"]
    )

    assert saved["revision"] != original["revision"]
    assert (workspace / "AGENT.md").read_text(encoding="utf-8") == "# Updated persona\n"
    with pytest.raises(StaleAgentFileError):
        service.write_core_file(
            "research", "AGENT.md", "stale", original["revision"]
        )
    with pytest.raises(AgentAdminError):
        service.read_core_file("research", "../config.json")


def test_duplicate_or_nonempty_workspace_is_rejected_without_config_change(admin):
    service, root, config_path = admin
    occupied = root / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(AgentAdminError):
        service.create_agent("research", "Research", str(occupied))

    assert config_path.read_text(encoding="utf-8") == before
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_a_concurrent_unrelated_setting_survives_a_roster_write(admin):
    """The console writes one config file from several pages.

    Reading the whole file, editing the roster and writing the whole thing back
    drops anything another page saved in between, which is silent data loss the
    user only notices later.
    """
    service, root, config_path = admin
    service.snapshot()

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    stored["model"] = "chosen-on-another-page"
    config_path.write_text(json.dumps(stored), encoding="utf-8")
    service._settings = None  # drop the cache the way a fresh request would

    service.create_agent("research", "Research", str(root / "research"))

    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["model"]
        == "chosen-on-another-page"
    )
    assert [item["id"] for item in _saved(root)["agents"]] == ["primary", "research"]


def test_a_stale_roster_revision_is_refused(admin):
    service, root, _ = admin
    stale = service.snapshot()["revision"]
    service.create_agent("first", "First", str(root / "first"))

    with pytest.raises(StaleRosterError):
        service.create_agent(
            "second", "Second", str(root / "second"), revision=stale
        )


def test_bindings_are_edited_one_row_at_a_time(admin):
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))

    service.set_binding("research", "feishu", "chat-1")
    service.set_binding("primary", "wechat")
    rows = service.snapshot()["bindings"]

    assert {
        (r["agent_id"], r["channel_type"], r.get("conversation_id")) for r in rows
    } == {("research", "feishu", "chat-1"), ("primary", "wechat", None)}

    service.set_binding("primary", "feishu", "chat-1")
    retargeted = service.snapshot()["bindings"]
    assert len(retargeted) == 2
    assert {
        (r["agent_id"], r["channel_type"], r.get("conversation_id")) for r in retargeted
    } == {("primary", "feishu", "chat-1"), ("primary", "wechat", None)}

    service.remove_binding("feishu", "chat-1")
    assert [r["channel_type"] for r in service.snapshot()["bindings"]] == ["wechat"]


def test_per_agent_asset_selection_round_trips(admin):
    service, root, _ = admin

    def stored(agent_id):
        return next(
            item for item in _saved(root)["agents"] if item["id"] == agent_id
        )

    service.create_agent(
        "research", "Research", str(root / "research"), skills=["web-search"]
    )
    assert stored("research")["skills"] == ["web-search"]

    # Absent means "everything", so it must not be confused with an empty
    # selection, which means "nothing".
    assert "skills" not in stored("primary")

    service.update_agent("research", skills=[])
    assert stored("research")["skills"] == []

    # Explicit None means "all of them" again, and is distinct from omitting
    # the argument (which would leave the empty list in place).
    service.update_agent("research", skills=None)
    assert "skills" not in stored("research")


def test_an_agents_model_travels_with_its_provider(admin):
    """A model asked of the wrong vendor is an error, so the two move together."""
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))

    def stored():
        return next(
            item for item in _saved(root)["agents"] if item["id"] == "research"
        )

    service.update_agent("research", model="claude-sonnet-4", bot_type="claude")
    assert (stored()["model"], stored()["bot_type"]) == ("claude-sonnet-4", "claude")

    # Back to following the configured model: the provider goes with it, rather
    # than lingering to route somebody else's model.
    service.update_agent("research", model="")
    assert "model" not in stored() and "bot_type" not in stored()


def test_the_default_agent_follows_the_configured_model(admin):
    service, root, _ = admin
    with pytest.raises(AgentAdminError):
        service.update_agent("primary", model="claude-sonnet-4")


def test_promotion_drops_the_agents_own_model(admin):
    """Otherwise settings and the Agent would both claim to set the model."""
    service, root, _ = admin
    service.create_agent("research", "Research", str(root / "research"))
    service.update_agent("research", model="claude-sonnet-4", bot_type="claude")

    service.update_agent("research", make_default=True)
    promoted = next(
        item for item in _saved(root)["agents"] if item["id"] == "research"
    )
    assert "model" not in promoted and "bot_type" not in promoted


def test_a_description_round_trips(admin):
    service, root, _ = admin
    service.create_agent(
        "research", "Research", str(root / "research"), description="Digs up sources"
    )

    def stored():
        return next(
            item for item in _saved(root)["agents"] if item["id"] == "research"
        )

    assert stored()["description"] == "Digs up sources"
    service.update_agent("research", description="")
    assert "description" not in stored()
