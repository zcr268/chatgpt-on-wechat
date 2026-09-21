"""Serve a hand-off that arrived from another process.

The far side ran the delegation tool and picked a teammate hosted here. This
module turns that request into the same private, delegated turn a local
hand-off would run — same session key, same guards, same prompt — streams the
teammate's tool steps back as they happen, and finishes with one result
record in the shape the delegation tool already returns.

Transport-agnostic: the caller supplies ``send_chunk`` and whatever carried
the request is none of this module's business.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from common.log import logger

CHUNK_TOOL_STEP = "tool_step"
CHUNK_RESULT = "result"


def serve_invoke(payload: dict, agent_bridge, send_chunk: Callable[[dict], None]) -> None:
    """Run one incoming hand-off to completion, reporting through ``send_chunk``.

    ``payload`` fields (all optional except ``target_agent_id`` and ``task``):

    - ``request_id``: echoed in every chunk so the caller can correlate.
    - ``source_agent_id`` / ``source_name``: who is asking.
    - ``target_agent_id``: the local Agent to run; ``target_aliases`` lists
      other ids the caller knows that Agent by, which are folded onto the
      local id wherever they appear.
    - ``task``, ``root_session_id``, ``trace``, ``depth``, ``members``,
      ``peers``: the delegated turn's context, mirroring a local hand-off.
    - ``timeout``: seconds the caller is prepared to wait.

    Never raises: any refusal or failure is reported as a failed result.
    """
    from agent.multiagent import get_transport
    from agent.tools.agent_delegate.agent_delegate import (
        TASK_SOURCE,
        AgentDelegateTool,
        DelegationPolicy,
        _relay_lock,
        delegated_prompt,
    )

    request_id = str(payload.get("request_id") or "")
    source_id = str(payload.get("source_agent_id") or "").strip() or "unknown"
    source_name = str(payload.get("source_name") or source_id).strip() or source_id
    task = str(payload.get("task") or "").strip()
    addressed_id = str(payload.get("target_agent_id") or "").strip()

    def fail(error: str, agent_id: str = addressed_id, agent_name: str = "") -> None:
        logger.warning(f"[MultiAgent] hand-off {request_id or '?'} refused: {error}")
        send_chunk({
            "chunk_type": CHUNK_RESULT,
            "request_id": request_id,
            "status": "failed",
            "error": error,
            "agent_id": agent_id,
            "agent_name": agent_name,
        })

    if agent_bridge is None:
        return fail("agent runtime not available")
    if not addressed_id or not task:
        return fail("target_agent_id and task are required")

    try:
        target = agent_bridge.agent_registry.get_addressed(addressed_id, require_enabled=True)
    except Exception:
        return fail(f"Target Agent '{addressed_id}' is not available")

    try:
        from config import conf

        policy = DelegationPolicy.from_config(conf().get("agent_delegation", {}))
    except (TypeError, ValueError) as exc:
        return fail(f"Invalid delegation policy: {exc}", target.id, target.name)
    if not policy.enabled:
        return fail("Agent delegation is disabled", target.id, target.name)
    if len(task) > policy.max_message_chars:
        return fail(
            f"Delegated task exceeds {policy.max_message_chars} characters", target.id, target.name
        )

    # The caller may know this Agent by another id; fold every such alias onto
    # the local id so the chain and roster compare against what runs here.
    aliases = {addressed_id, *(str(a).strip() for a in payload.get("target_aliases") or [] if a)}
    aliases.discard("")

    def local(agent_id) -> str:
        agent_id = str(agent_id or "").strip()
        return target.id if agent_id in aliases else agent_id

    trace = tuple(local(t) for t in (payload.get("trace") or []) if str(t or "").strip())
    if not trace or trace[-1] != target.id:
        trace = (*trace, target.id)
    if target.id in trace[:-1]:
        return fail(f"Delegation cycle rejected: {' -> '.join(trace)}", target.id, target.name)

    try:
        depth = int(payload.get("depth") or (len(trace) - 1))
    except (TypeError, ValueError):
        depth = len(trace) - 1
    if depth > policy.max_depth:
        return fail(
            f"Delegation depth {depth} exceeds the maximum {policy.max_depth}", target.id, target.name
        )

    members = []
    for member_id in payload.get("members") or []:
        member_id = local(member_id)
        if member_id and member_id != target.id and member_id not in members:
            members.append(member_id)
    # Let this process name the rest of the team, wherever they live, so the
    # teammate can hand work onward. Its own aliases are not peers.
    transport = get_transport()
    if transport is not None:
        transport.register_peers(
            p for p in (payload.get("peers") or [])
            if isinstance(p, dict) and str(p.get("id") or "").strip() not in aliases
        )

    root_session_id = str(payload.get("root_session_id") or uuid.uuid4())
    session_id = AgentDelegateTool._session_id(source_id, target.id, root_session_id)
    from common.utils import current_agent_run_id

    run_id = uuid.uuid4().hex

    context = Context(ContextType.TEXT, task, kwargs={})
    context["session_id"] = session_id
    context["request_id"] = request_id or f"delegate_{uuid.uuid4().hex}"
    context["receiver"] = target.id
    context["isgroup"] = False
    context["channel_type"] = "agent"
    context["agent_id"] = target.id
    context["is_delegated_task"] = True
    context["delegated_by"] = source_id
    context["delegation_depth"] = depth
    context["delegation_trace"] = list(trace)
    context["delegation_root_session"] = root_session_id
    context["delegation_members"] = members
    context["run_id"] = run_id
    context["parent_run_id"] = current_agent_run_id() or ""
    context["task_source"] = TASK_SOURCE

    def forward(event) -> None:
        # Only tool steps travel back: prose and reasoning belong to this
        # teammate's own run, exactly as for a local hand-off.
        if isinstance(event, dict) and event.get("type") in (
            "tool_execution_start", "tool_execution_end",
        ):
            try:
                send_chunk({"chunk_type": CHUNK_TOOL_STEP, "request_id": request_id, "event": event})
            except Exception as exc:
                logger.debug(f"[MultiAgent] step forward failed: {exc}")

    try:
        timeout = float(payload.get("timeout") or policy.timeout_seconds)
    except (TypeError, ValueError):
        timeout = policy.timeout_seconds

    lock = _relay_lock(session_id)
    if not lock.acquire(timeout=timeout):
        return fail("timed out waiting for the teammate to be free", target.id, target.name)
    started_at = time.monotonic()
    logger.info(
        f"[MultiAgent] serving hand-off {request_id or '?'}: {source_id} -> {target.id}, depth={depth}"
    )
    try:
        reply = agent_bridge.agent_reply(
            delegated_prompt(source_name, source_id, task), context=context, on_event=forward
        )
    except Exception as exc:
        return fail(str(exc), target.id, target.name)
    finally:
        lock.release()
    duration = time.monotonic() - started_at

    if reply is not None and reply.type == ReplyType.ERROR:
        return fail(str(reply.content), target.id, target.name)
    send_chunk({
        "chunk_type": CHUNK_RESULT,
        "request_id": request_id,
        "status": "done",
        "content": reply.content if reply is not None else "",
        "agent_id": target.id,
        "agent_name": target.name,
        "duration": round(duration, 3),
    })
