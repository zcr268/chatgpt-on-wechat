import base64
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import random
import re
import sys
import time
import uuid
from typing import Dict, List, Optional
from urllib.parse import quote

import web

from bridge.context import *
from common import const
from common import i18n
from common.log import logger
from config import (
    conf,
    get_data_root,
)
from models import model_catalog
from agent.permission import (
    MODES as PERMISSION_MODES,
    global_mode as permission_global_mode,
    normalize_mode as permission_normalize_mode,
)
# Handlers that have moved to api/. Not used here, but build_app() resolves
# the URL table against this module's globals, so every handler name has to
# be in scope here -- see build_app at the bottom of the file.
from channel.web.api.channels import (  # noqa: F401
    ChannelsHandler, FeishuRegisterHandler, WeixinQrHandler,
)
from channel.web.api.config import ConfigHandler  # noqa: F401
from channel.web.api.models import ModelsHandler  # noqa: F401
from channel.web.api.openai_compat import OpenAIChatCompletionsHandler
# By name, not as a module: "providers" is a common local variable in the
# handlers below, and a local binding would shadow the module for the
# whole function -- including the lines above the assignment.
from channel.web.core.providers import PROVIDER_MODELS
# Shared with WebChannel, so it lives in _common. Imported by name rather
# than as a module: the handlers below read these out of this module's
# globals, the same place web.py resolves the handler names themselves
# from, and it keeps the names patchable where the tests already patch them.
from channel.web.core._common import (
    _agent_badge, _build_artifact_payload, _build_preview_url, _check_auth,
    _live_channel_manager,
    _ensure_list, _get_preview_secret, _get_upload_dir, _get_web_password,
    _get_workspace_root, _is_password_enabled, _raw_web_input,
    _read_uploaded_file_bytes, _request_agent_id,
    _require_auth, _rewrite_relative_media, _roster_from_members,
    _session_expire_seconds,
)
# Re-exported, not used here. app.py waits on SERVING and channel_factory
# resolves "channel.web.web_channel.WebChannel" by name, so both have to
# stay reachable through this module; the tests reach for the rest.
from channel.web.core._common import (  # noqa: F401
    SERVING, SSEStreamState, WebMessage,
)
from channel.web.core.channel import WebChannel  # noqa: F401
from channel.web.core import template

def _parse_sse_cursor(*values) -> int:
    cursors = []
    for value in values:
        try:
            cursors.append(max(0, int(value or 0)))
        except (TypeError, ValueError):
            cursors.append(0)
    return max(cursors, default=0)


def _create_auth_token():
    """Create a stateless signed token: ``<timestamp_hex>.<hmac_hex>``."""
    ts = format(int(time.time()), "x")
    sig = hmac.new(
        _get_web_password().encode(),
        ts.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{ts}.{sig}"


def _decode_dir_token(token: str) -> str:
    """Verify and decode a /preview directory token. Raises ValueError if invalid."""
    body, _, sig = (token or "").partition(".")
    if not body or not sig:
        raise ValueError("Malformed preview token")
    padding = "=" * (-len(body) % 4)
    try:
        real = base64.urlsafe_b64decode(body + padding).decode("utf-8")
    except Exception:
        raise ValueError("Malformed preview token")
    expected = hmac.new(_get_preview_secret(), real.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Bad preview token signature")
    return real


def _serve_allowed_roots() -> list:
    """Roots that /api/file and /preview may read from (symlinks resolved).

    Includes the configured serve root, the Agent workspace, and any project
    directory a session has opened. Project dirs may live outside the serve
    root (e.g. ``/tmp/foo``), so previewing files in an opened project would
    otherwise be denied.
    """
    serve_root = conf().get("web_file_serve_root", "~") or "~"
    roots = [
        os.path.realpath(os.path.expanduser(serve_root)),
        os.path.realpath(_get_workspace_root()),
    ]
    try:
        from agent.workspace import project_store
        for rec in project_store.list_recents():
            roots.append(os.path.realpath(rec["path"]))
    except Exception:
        pass
    return roots


def _is_path_allowed(real_path: str) -> bool:
    roots = _serve_allowed_roots()
    if os.sep in roots:
        return True
    for root in roots:
        try:
            if os.path.commonpath([real_path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _paths_written_by_step(step: dict) -> list:
    """Files a persisted tool step produced, if any.

    `write`/`edit` name theirs in the arguments. A `subagent` step lists the
    ones its sub agents wrote in its result: those files never passed through
    a tool call of this agent's own, so nothing else records them.
    """
    name = step.get("name")
    if name in ("write", "edit"):
        args = step.get("arguments")
        path = str((args or {}).get("path") or "").strip() if isinstance(args, dict) else ""
        return [path] if path else []
    if name != "subagent":
        return []
    try:
        results = json.loads(step.get("result") or "{}").get("results") or []
    except (ValueError, TypeError, AttributeError):
        return []
    return [
        path
        for item in results if isinstance(item, dict)
        for path in (item.get("files") or [])
    ]


def _artifacts_from_steps(steps, session_id: str = None, agent_id: str = None) -> list:
    """
    Rebuild the artifact cards of a persisted assistant message.

    History replay has no SSE events, so the tool calls are the only record.
    Doing this server-side keeps one implementation of the workspace-internal
    filter — and lets absolute paths inside the workspace be recognised, which
    a client mirroring the rules can't do.

    ``session_id`` anchors detection to the session's working dir (the project
    dir when one is open), matching the live SSE path; otherwise state_root.
    """
    from agent.protocol.artifact import get_workspace_root, safe_build_artifact

    out = []
    seen = set()
    root = None
    for step in steps or []:
        if not isinstance(step, dict) or step.get("type") != "tool" or step.get("is_error"):
            continue
        for path in _paths_written_by_step(step):
            if root is None:
                root = _get_workspace_root(session_id, agent_id) if session_id else get_workspace_root()
            info = safe_build_artifact(path, root)
            if not info or info["path"] in seen:
                continue
            seen.add(info["path"])
            payload = _build_artifact_payload(info)
            if payload:
                out.append(payload)
    return out


def _add_subagent_displays(steps) -> None:
    """Give persisted `subagent` steps the same readable form they had live.

    `display` is deliberately kept out of the model's context, so it is not in
    the stored conversation either. Rebuilding it here means a reloaded page
    shows the sub agents' reports rather than the JSON the model was handed.
    """
    from agent.tools.subagent import format_results

    for step in steps or []:
        if not isinstance(step, dict) or step.get("name") != "subagent":
            continue
        try:
            results = json.loads(step.get("result") or "{}").get("results")
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(results, list) and results:
            step["display"] = format_results(results)


def _add_delegate_displays(steps) -> None:
    """Give persisted `agent_delegate` steps the readable form they had live.

    Same story as `_add_subagent_displays`: `display` is kept out of the model's
    context and so out of storage, so a reloaded page would otherwise show the
    JSON handed to the model rather than "who → whom" and the teammate's reply.
    """
    from agent.tools.agent_delegate.agent_delegate import format_delegate_result

    for step in steps or []:
        if not isinstance(step, dict) or step.get("name") != "agent_delegate":
            continue
        try:
            payload = json.loads(step.get("result") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or not payload.get("content"):
            continue
        source_id = payload.get("delegated_by") or ""
        source_name = source_id
        try:
            from bridge.bridge import Bridge

            source_name = (
                Bridge().get_agent_bridge().agent_registry.get(source_id).name
                or source_id
            )
        except Exception:
            pass
        step["display"] = format_delegate_result(
            source_name,
            payload.get("agent_name") or payload.get("agent_id") or "",
            payload.get("content") or "",
            status=payload.get("status") or "done",
        )


def _read_uploaded_file_bytes_limited(file_obj, max_bytes: int) -> bytes:
    """Read uploaded content and fail once it exceeds max_bytes."""
    if isinstance(file_obj, bytes):
        content = file_obj
    elif isinstance(file_obj, str):
        content = file_obj.encode("utf-8")
    elif hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read(max_bytes + 1)
    elif hasattr(file_obj, "read"):
        content = file_obj.read(max_bytes + 1)
    elif hasattr(file_obj, "value"):
        content = file_obj.value
    else:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, bytes):
        raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")
    if len(content) > max_bytes:
        raise ValueError("file too large")
    return content


def _generate_session_title(user_message: str, assistant_reply: str = "",
                            session_id: str = "") -> str:
    """Delegate to the shared SessionService implementation."""
    from agent.chat.session_service import generate_session_title
    return generate_session_title(user_message, assistant_reply, session_id)


class RootHandler:
    """Where /chat used to live. The console is at / now, so that the address
    bar reads as paths into one app rather than as a page with state after it.
    Kept as a redirect because /chat is what older bookmarks, and the startup
    banner of any running instance, still point at."""

    def GET(self):
        raise web.seeother('/')


class HealthHandler:
    # Unauthenticated liveness probe. The desktop shell polls this to know the
    # backend is up; it must never require auth (a set web_password would
    # otherwise make startup hang). Returns no sensitive data.
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        return json.dumps({"status": "ok"})


class McpOAuthCallbackHandler:
    """OAuth redirect target for MCP servers requiring authorization.

    The browser lands here after the user authorizes a remote MCP server.
    We exchange the authorization code for tokens and bring the server
    online. Unauthenticated by design: the OAuth `state` param is the
    single-use secret that binds this request to a pending authorization.
    """

    def GET(self):
        web.header('Content-Type', 'text/html; charset=utf-8')
        params = web.input(code="", state="", error="", error_description="")

        def _page(title: str, message: str) -> str:
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{title}</title></head>"
                "<body style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
                "max-width:520px;margin:64px auto;padding:0 20px;text-align:center;color:#1f2328'>"
                f"<h2>{title}</h2><p style='color:#57606a'>{message}</p></body></html>"
            )

        if params.error:
            logger.warning(f"[MCP-OAuth] callback error: {params.error} {params.error_description}")
            return _page("授权失败", f"{params.error}: {params.error_description or ''}")

        if not params.code or not params.state:
            return _page("参数缺失", "回调缺少 code 或 state 参数。")

        try:
            from agent.tools.mcp.mcp_oauth import pop_pending
            from agent.tools.mcp.mcp_client import notify_server_authorized
        except Exception as e:
            logger.warning(f"[MCP-OAuth] callback import failed: {e}")
            return _page("内部错误", "OAuth 模块不可用。")

        handler = pop_pending(params.state)
        if handler is None:
            return _page("会话已过期", "授权请求不存在或已过期，请重新触发授权。")

        try:
            ok = handler.finish_authorization(params.code)
        except Exception as e:
            logger.warning(f"[MCP-OAuth] token exchange crashed: {e}")
            ok = False

        if not ok:
            return _page("授权失败", "换取令牌失败，请重试。")

        notify_server_authorized(handler.server_name)
        logger.info(f"[MCP-OAuth] Server '{handler.server_name}' authorized via web callback")
        return _page(
            "授权成功",
            f"MCP 服务 “{handler.server_name}” 已授权，可以返回聊天继续使用了。",
        )


class AuthCheckHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success", "auth_required": False})
        if _check_auth():
            return json.dumps({"status": "success", "auth_required": True, "authenticated": True})
        return json.dumps({"status": "success", "auth_required": True, "authenticated": False})


class AuthLoginHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success"})
        try:
            data = json.loads(web.data())
        except Exception:
            return json.dumps({"status": "error", "message": "Invalid request"})
        password = str(data.get("password", "") or "")
        expected = _get_web_password()
        if not hmac.compare_digest(password, expected):
            logger.warning("[WebChannel] Invalid login attempt")
            return json.dumps({"status": "error", "message": "Wrong password"})
        token = _create_auth_token()
        web.setcookie("cow_auth_token", token, expires=_session_expire_seconds(),
                       path="/", httponly=True, samesite="Lax")
        # Also return the token in the body: the desktop client (file:// origin)
        # can't rely on the cookie and sends it back via an Authorization header.
        return json.dumps({"status": "success", "token": token})


class AuthLogoutHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.setcookie("cow_auth_token", "", expires=-1, path="/")
        return json.dumps({"status": "success"})


class MessageHandler:
    def POST(self):
        _require_auth()
        return WebChannel().post_message()


class UploadHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().upload_file()


class VoiceAsrHandler:
    """Receive a mic recording, persist it under uploads/ and run ASR.
    Returns {status, text, audio_url} so the UI can render a playback bubble."""
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')

        saved_path = None
        try:
            params = _raw_web_input()
            agent_id = _request_agent_id(params)
            file_obj = params.get("file")
            if file_obj is None:
                return json.dumps({"status": "error", "message": "no audio file"})

            filename = getattr(file_obj, "filename", "") or "recording.webm"
            ext = os.path.splitext(filename)[1].lower() or ".webm"
            if ext not in (".webm", ".ogg", ".opus", ".mp4", ".m4a", ".mp3", ".wav"):
                ext = ".webm"

            upload_dir = _get_upload_dir(agent_id)
            os.makedirs(upload_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            saved_name = f"voice_input_{ts}_{random.randint(0, 9999)}{ext}"
            saved_path = os.path.join(upload_dir, saved_name)
            with open(saved_path, "wb") as f:
                f.write(file_obj.file.read() if hasattr(file_obj, "file") else file_obj.value)

            suffix = f"?agent_id={agent_id}" if agent_id else ""
            audio_url = f"/uploads/{saved_name}{suffix}"

            from bridge.bridge import Bridge
            reply = Bridge().fetch_voice_to_text(saved_path)
            if reply is None:
                return json.dumps({
                    "status": "error",
                    "message": "ASR returned no reply",
                    "audio_url": audio_url,
                })

            from bridge.reply import ReplyType
            if reply.type == ReplyType.TEXT:
                return json.dumps({
                    "status": "success",
                    "text": reply.content or "",
                    "audio_url": audio_url,
                })
            return json.dumps({
                "status": "error",
                "message": reply.content or "ASR failed",
                "audio_url": audio_url,
            })
        except Exception as e:
            logger.exception(f"[VoiceAsrHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class VoiceTtsHandler:
    """On-demand TTS for the in-chat "read aloud" button. Returns the
    audio URL and (when session_id is given) persists it onto the message."""
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data() or b"{}")
            text = (data.get("text") or "").strip()
            session_id = (data.get("session_id") or "").strip()
            agent_id = data.get("agent_id")
            if not text:
                return json.dumps({"status": "error", "message": "empty text"})
            # `@singleton` makes WebChannel a factory function — go via instance.
            channel = WebChannel()
            if not channel._tts_provider_ready():
                return json.dumps({"status": "error", "message": "tts not configured"})

            from bridge.bridge import Bridge
            reply = Bridge().fetch_text_to_voice(text)
            if reply is None or reply.type != ReplyType.VOICE or not reply.content:
                msg = getattr(reply, "content", "") or "tts failed"
                return json.dumps({"status": "error", "message": str(msg)})

            url = channel._publish_tts_audio(reply.content, agent_id)
            if not url:
                return json.dumps({"status": "error", "message": "publish failed"})

            if session_id:
                try:
                    from agent.memory import get_conversation_store
                    from agent.registry import get_agent_registry
                    profile = get_agent_registry().get(agent_id)
                    get_conversation_store(profile.workspace).attach_extras_to_last_assistant(
                        session_id, {"audio": {"url": url, "kind": "tts"}},
                    )
                except Exception as e:
                    logger.debug(f"[VoiceTtsHandler] persist skipped: {e}")

            return json.dumps({"status": "success", "audio_url": url})
        except Exception as e:
            logger.exception(f"[VoiceTtsHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class UploadsHandler:
    def GET(self, file_name):
        _require_auth()
        try:
            params = web.input(agent_id='')
            upload_dir = _get_upload_dir(_request_agent_id(params))
            full_path = os.path.normpath(os.path.join(upload_dir, file_name))
            if not os.path.abspath(full_path).startswith(os.path.abspath(upload_dir)):
                raise web.notfound()
            if not os.path.isfile(full_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            web.header('Cache-Control', 'public, max-age=86400')
            with open(full_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving upload: {e}")
            raise web.notfound()


class FileServeHandler:
    def GET(self):
        _require_auth()
        try:
            params = web.input(path="")
            file_path = params.path
            if not file_path or not os.path.isabs(file_path):
                raise web.notfound()
            # Resolve symlinks and confine access to the allowed root dirs,
            # so this endpoint can't be abused to read arbitrary files (e.g. /etc/passwd, ~/.ssh).
            # Defaults to the user home dir plus the agent workspace; set web_file_serve_root="/"
            # to allow the whole filesystem.
            file_path = os.path.realpath(file_path)
            if not _is_path_allowed(file_path):
                raise web.notfound()
            if not os.path.isfile(file_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            file_name = os.path.basename(file_path)
            from urllib.parse import quote
            web.header('Content-Type', content_type)
            web.header('Content-Disposition', f"inline; filename*=UTF-8''{quote(file_name)}")
            web.header('Cache-Control', 'public, max-age=3600')
            with open(file_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving file: {e}")
            raise web.notfound()


# Injected into previewed HTML so the iframe's scrollbars match the app chrome
# instead of falling back to the platform default (wide, opaque track).
# Placed at the top of <head> so a page that styles its own scrollbars still wins.
_PREVIEW_SCROLLBAR_CSS = (
    "<style>"
    "html{scrollbar-width:thin;scrollbar-color:rgba(128,128,128,.45) transparent}"
    "::-webkit-scrollbar{width:8px;height:8px}"
    "::-webkit-scrollbar-track{background:transparent}"
    "::-webkit-scrollbar-corner{background:transparent}"
    "::-webkit-scrollbar-thumb{background:rgba(128,128,128,.45);border-radius:4px;"
    "border:2px solid transparent;background-clip:padding-box}"
    "::-webkit-scrollbar-thumb:hover{background:rgba(128,128,128,.7);"
    "background-clip:padding-box}"
    "</style>"
)

_HEAD_OPEN_RE = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(rb"<html\b[^>]*>", re.IGNORECASE)


def _inject_preview_chrome(raw: bytes) -> bytes:
    """Insert the scrollbar stylesheet into a previewed HTML document."""
    css = _PREVIEW_SCROLLBAR_CSS.encode("utf-8")
    for pattern in (_HEAD_OPEN_RE, _HTML_OPEN_RE):
        m = pattern.search(raw)
        if m:
            return raw[: m.end()] + css + raw[m.end():]
    return css + raw


class PreviewHandler:
    """
    Directory-mounted file server for the preview panel: /preview/<token>/<relpath>

    Unlike /api/file (single file, query param) this mounts the file's directory,
    so relative assets inside a generated HTML page resolve normally. The token is
    HMAC-signed, which is what authorizes the request - the sandboxed iframe can't
    send the auth cookie.
    """

    def GET(self, path_info):
        try:
            token, _, rel_path = (path_info or "").partition("/")
            if not token or not rel_path:
                raise web.notfound()

            from urllib.parse import unquote
            rel_path = unquote(rel_path)

            try:
                base_dir = _decode_dir_token(token)
            except ValueError:
                raise web.notfound()

            full_path = os.path.realpath(os.path.join(base_dir, rel_path))
            base_real = os.path.realpath(base_dir)
            # Confine to the mounted directory, then to the globally allowed roots.
            if os.path.commonpath([full_path, base_real]) != base_real:
                raise web.notfound()
            if not _is_path_allowed(full_path) or not os.path.isfile(full_path):
                raise web.notfound()

            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            web.header('Cache-Control', 'no-cache')
            web.header('X-Content-Type-Options', 'nosniff')
            is_html = content_type.startswith("text/html")
            if is_html:
                # Agent-generated pages are untrusted. The CSP sandbox forces an
                # opaque origin even when the page is opened as a top-level tab,
                # so it can't read the console's localStorage auth token; the
                # panel's iframe already applies the same flags.
                #
                # No frame-ancestors here: the desktop renderer is loaded from
                # file:// (or the Vite dev server), so 'self' would block its
                # preview iframe outright. The sandbox is what carries the
                # security guarantee; framing alone reveals nothing extra.
                web.header(
                    'Content-Security-Policy',
                    "sandbox allow-scripts allow-popups allow-forms allow-modals",
                )
            with open(full_path, 'rb') as f:
                data = f.read()
            return _inject_preview_chrome(data) if is_html else data
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving preview: {e}")
            raise web.notfound()


class PollHandler:
    def POST(self):
        _require_auth()
        return WebChannel().poll_response()


class CancelHandler:
    def POST(self):
        _require_auth()
        return WebChannel().cancel_request()


class StreamHandler:
    def GET(self):
        _require_auth()
        params = web.input(request_id='', after_seq='')
        request_id = params.request_id
        if not request_id:
            raise web.badrequest()

        # Explicit query cursors are used by the frontend's manually-created
        # EventSource. Native EventSource reconnects remain compatible via the
        # standard Last-Event-ID request header.
        after_seq = _parse_sse_cursor(
            params.after_seq,
            web.ctx.env.get('HTTP_LAST_EVENT_ID', '0'),
        )

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')
        web.header('Access-Control-Allow-Origin', '*')

        return WebChannel().stream_response(request_id, after_seq)


class ChatHandler:
    def GET(self):
        # Content-Type must be explicit: behind a reverse proxy that sends
        # X-Content-Type-Options: nosniff, a missing type makes browsers
        # refuse to sniff and render the page as plain text source.
        web.header('Content-Type', 'text/html; charset=utf-8')
        web.header('Cache-Control', 'no-cache, no-store, must-revalidate')
        web.header('Pragma', 'no-cache')
        # The shell pulls its layout, views and modals in from templates/;
        # render() assembles them and stamps every first-party asset with its
        # own mtime, so an upgraded console never runs against cached old
        # scripts while unchanged ones stay cacheable.
        html = template.render('chat.html')
        # Inject the backend-resolved default language for first-load fallback.
        html = html.replace("{{COW_DEFAULT_LANG}}", i18n.get_language())
        return html


def _resolve_instance_agent_id(instance_id: str) -> str:
    """The Agent a channel instance is currently bound to, or "" for none.

    Thin alias over the scheduler's resolver so there is one implementation of
    "which Agent owns this instance" shared by task execution, the task list,
    and task creation. A legacy single-instance channel (instance_id ==
    channel_type) has no explicit binding and resolves to "" -> default Agent.
    """
    from agent.tools.scheduler.integration import _resolve_instance_agent_id as _resolve
    return _resolve(instance_id)


class ToolsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.tool_manager import ToolManager
            from common import i18n
            tm = ToolManager()
            if not tm.tool_classes:
                tm.load_tools()
            tools = []
            lang = i18n.get_language()
            for name, cls in tm.tool_classes.items():
                try:
                    instance = cls()
                    desc = instance.description
                    if lang == i18n.ZH_HANT and desc:
                        desc = i18n.to_traditional(desc)
                    elif lang == "en" and name == "scheduler":
                        desc = (
                            "Create, query and manage scheduled tasks (reminders, periodic tasks, etc.).\n\n"
                            "⚠️ IMPORTANT: Only use this tool when delayed or periodic execution is needed."
                        )
                    tools.append({
                        "name": name,
                        "description": desc,
                    })
                except Exception:
                    tools.append({"name": name, "description": ""})
            return json.dumps({"status": "success", "tools": tools}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tools API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _skill_service(agent_id: str = ''):
    """
    A SkillService over the skills the console manages.

    Skills stay anchored to the agent's state root even while a session has a
    project open, so this deliberately resolves the workspace without a session.
    ``agent_id`` selects which agent's skills to manage, so a multi-agent setup
    keeps each agent's library isolated.
    """
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService
    from common import state_dir
    workspace_root = _get_workspace_root(agent_id=agent_id or None)
    custom_dir = str(state_dir.skills_dir(base=workspace_root))
    return SkillService(SkillManager(custom_dir=custom_dir))


class SkillsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common import i18n
            params = web.input(agent_id='')
            # The library page lists everything installed, unnarrowed by the
            # Agent's selection: a skill it has not selected still has to be
            # visible here for the selection to be editable at all.
            service = _skill_service(_request_agent_id(params))
            skills = service.query()
            if i18n.get_language() == i18n.ZH_HANT:
                for skill in skills:
                    if isinstance(skill, dict):
                        for k, v in list(skill.items()):
                            if k in ("name", "description", "display_name") and isinstance(v, str):
                                skill[k] = i18n.to_traditional(v)
            return json.dumps({"status": "success", "skills": skills}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            name = body.get("name")
            if not action or not name:
                return json.dumps({"status": "error", "message": "action and name are required"})
            service = _skill_service(_request_agent_id(body))
            if action == "open":
                service.open({"name": name})
            elif action == "close":
                service.close({"name": name})
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SkillContentHandler:
    """
    A skill's definition file, for the console's viewer and editor.

    Addressed by skill name rather than by path, because the loader is what
    resolves a name to a file: a workspace skill shadows a builtin of the same
    name, and a builtin sits outside the workspace that the file APIs are
    confined to.

    Unlike the skill list, the text is served exactly as stored - no
    simplified-to-traditional conversion. What comes back here is what a save
    would write, and rewriting someone's file into another script because of
    the console's display language is not a conversion they asked for.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(name='', agent_id='')
            name = (params.name or '').strip()
            if not name:
                return json.dumps({"status": "error", "message": "name is required"})
            result = _skill_service(_request_agent_id(params)).read_content(name)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Skill content error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace.service import WorkspaceConflictError

            body = json.loads(web.data() or b'{}')
            name = (body.get("name") or "").strip()
            if not name:
                return json.dumps({"status": "error", "message": "name is required"})
            content = body.get("content")
            if not isinstance(content, str):
                return json.dumps({"status": "error", "message": "content must be a string"})

            try:
                result = _skill_service(_request_agent_id(body)).write_content(
                    name, content, expected_mtime=body.get("expected_mtime"),
                )
            except WorkspaceConflictError as e:
                return json.dumps({"status": "error", "code": "conflict", "message": str(e)})

            logger.info(f"[WebChannel] Skill saved: {name} ({result['size']} bytes)")
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except PermissionError:
            return json.dumps({"status": "error", "message": "permission denied"})
        except Exception as e:
            logger.error(f"[WebChannel] Skill write error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(
                page='1', page_size='20', category='memory', agent_id=''
            )
            workspace_root = _get_workspace_root(agent_id=_request_agent_id(params))
            service = MemoryService(workspace_root)
            result = service.list_files(
                page=int(params.page), page_size=int(params.page_size),
                category=params.category,
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Memory API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryContentHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(filename='', category='memory', agent_id='')
            if not params.filename:
                return json.dumps({"status": "error", "message": "filename required"})
            workspace_root = _get_workspace_root(agent_id=_request_agent_id(params))
            service = MemoryService(workspace_root)
            result = service.get_content(params.filename, category=params.category)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except ValueError:
            return json.dumps({"status": "error", "message": "invalid filename"})
        except FileNotFoundError:
            return json.dumps({"status": "error", "message": "file not found"})
        except Exception as e:
            logger.error(f"[WebChannel] Memory content API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _global_task_store():
    """The one global task store the console reads and writes.

    All Agents' tasks live in a single file now (each task carries its own
    ``agent_id``), so handlers no longer resolve a per-Agent workspace path.
    """
    from agent.tools.scheduler.integration import get_task_store
    return get_task_store()


class SchedulerHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(agent_id='')
            requested = _request_agent_id(params)

            store = _global_task_store()
            if store is None:
                return json.dumps({"status": "success", "tasks": []}, ensure_ascii=False)

            # An explicit agent_id scopes the list to that Agent; without it the
            # console shows the whole team's schedule.
            tasks = store.list_tasks(agent_id=requested or None)
            # Stamp each task with its *effective* owner so the card/owner-chip
            # always matches what actually runs. For an IM task that is the
            # delivery instance's current binding (a channel re-bind moves the
            # owner with zero data migration); for Web / unbound tasks it is the
            # stored id, defaulting so the client never sees a blank owner it
            # uses to route mutations.
            from agent.tools.scheduler.integration import effective_task_agent_id
            try:
                from agent.registry import get_agent_registry
                default_id = get_agent_registry().default_agent_id
            except Exception:
                default_id = ""
            for task in tasks:
                task["agent_id"] = effective_task_agent_id(task) or default_id
            return json.dumps({"status": "success", "tasks": tasks}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerRunsHandler:
    """Execution history for scheduled tasks.

    Reads the same global ``runs`` ledger every scheduled execution writes to
    (``task_source='scheduler'``) rather than a side-car store, so history JOINs
    cleanly with native turns and needs no extra file. An explicit ``agent_id``
    scopes the list to one Agent; without it the console shows the whole team's
    history. An optional ``task_id`` narrows to a single task's runs.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(agent_id='', task_id='', limit='100', since='', offset='0')
            requested = _request_agent_id(params)
            task_id = (getattr(params, 'task_id', '') or '').strip()
            try:
                limit = max(1, min(500, int(params.limit)))
            except (TypeError, ValueError):
                limit = 100
            # ``offset`` pages the history list ("load more"). Cross-session poll
            # callers omit it (default 0).
            try:
                offset = max(0, int(getattr(params, 'offset', '0') or 0))
            except (TypeError, ValueError):
                offset = 0
            # ``since`` (epoch seconds) powers the client's cross-session
            # scheduler poll: return only executions started after the last one
            # it saw, so a background window/tab can surface a notification for a
            # task that fired in a session the user isn't currently viewing.
            since_raw = (getattr(params, 'since', '') or '').strip()
            try:
                since = int(since_raw) if since_raw else None
            except (TypeError, ValueError):
                since = None

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            if store is None:
                return json.dumps({"status": "success", "runs": []}, ensure_ascii=False)

            runs = store.list_runs(
                task_source="scheduler",
                task_id=task_id or None,
                agent_id=requested,  # None -> whole team; '' -> default Agent
                since=since,
                limit=limit,
                offset=offset,
            )
            # Flatten the light extras index onto each row so the client needs no
            # knowledge of the sidecar shape.
            for run in runs:
                extras = run.pop("extras", {}) or {}
                run["task_name"] = extras.get("task_name", "")
                run["action_type"] = extras.get("action_type", "")
                run["channel_type"] = extras.get("channel_type", "")
                run["instance_id"] = extras.get("instance_id", "")
                run["trigger"] = extras.get("trigger", "")
                run["output_preview"] = extras.get("output_preview", "")
            return json.dumps({"status": "success", "runs": runs}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler runs API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerRunDetailHandler:
    """One execution's full detail for the history detail dialog.

    The list view shows the short ``output_preview`` kept on the run row; opening
    a record fetches the full delivered body by joining back to the receiver's
    session (best-effort — see ``ConversationStore.get_run_detail``). Falls back
    to the preview when the session copy was pruned or never injected.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(run_id='')
            run_id = (getattr(params, 'run_id', '') or '').strip()
            if not run_id:
                return json.dumps({"status": "error", "message": "run_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            if store is None:
                return json.dumps({"status": "error", "message": "store unavailable"})

            detail = store.get_run_detail(run_id)
            if detail is None:
                return json.dumps({"status": "error", "message": "run not found"})
            return json.dumps({"status": "success", "run": detail}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler run detail API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerRunDeleteHandler:
    """Delete a single execution-history record from the runs ledger.

    Removes only the ledger row (the list item); the delivered message kept in
    the session history is untouched. Accepts run_id in the JSON body.
    """

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data()) if web.data() else {}
            run_id = (body.get("run_id") or "").strip()
            if not run_id:
                return json.dumps({"status": "error", "message": "run_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            if store is None:
                return json.dumps({"status": "error", "message": "store unavailable"})

            deleted = store.delete_run(run_id)
            if not deleted:
                return json.dumps({"status": "error", "message": "run not found"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler run delete API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerRunHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            agent_id = _request_agent_id(body)
            task_id = body.get("task_id")
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})

            from agent.tools.scheduler.integration import get_scheduler_service
            service = get_scheduler_service(agent_id=agent_id)
            if service is None:
                return json.dumps({
                    "status": "error",
                    "message": "Scheduler service is not running",
                })

            service.run_task_now(task_id)
            return json.dumps({
                "status": "success",
                "message": f"Task '{task_id}' queued for immediate execution",
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler manual run error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerToggleHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            enabled = body.get("enabled", True)
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})
            store = _global_task_store()
            if store is None:
                return json.dumps({"status": "error", "message": "Scheduler store unavailable"})
            store.enable_task(task_id, enabled)
            task = store.get_task(task_id)
            return json.dumps({"status": "success", "task": task}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler toggle error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerUpdateHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})
            
            from agent.tools.scheduler.scheduler_service import SchedulerService
            from datetime import datetime
            store = _global_task_store()
            if store is None:
                return json.dumps({"status": "error", "message": "Scheduler store unavailable"})

            # Get original task (single query to avoid repeated I/O)
            original_task = store.get_task(task_id)
            if not original_task:
                return json.dumps({"status": "error", "message": f"Task '{task_id}' not found"})
            
            # Build updates dict
            updates = {}
            if "name" in body:
                updates["name"] = body["name"]
            if "enabled" in body:
                updates["enabled"] = body["enabled"]
            
            # Update schedule
            if "schedule" in body:
                updates["schedule"] = body["schedule"]
                # If schedule config changed, recalculate next_run_at
                # Build merged temp task data for calculation (without modifying the original object)
                merged = dict(original_task)
                merged.update(updates)
                if "action" in body:
                    merged["action"] = body["action"]
                temp_service = SchedulerService(store, lambda t: None)
                next_run = temp_service._calculate_next_run(merged, datetime.now())
                if next_run:
                    updates["next_run_at"] = next_run.isoformat()
                else:
                    # Cannot calculate next run time, schedule config may be invalid
                    return json.dumps({
                        "status": "error", 
                        "message": "Cannot calculate next run time. Please check the schedule config (e.g., cron expression format, or whether the one-time task time has already passed)."
                    }, ensure_ascii=False)
            
            # Update action
            if "action" in body:
                # Get the task's original channel_type
                original_action = original_task.get("action", {})
                if not isinstance(original_action, dict):
                    original_action = {}
                action_patch = body["action"]
                if not isinstance(action_patch, dict):
                    return json.dumps({
                        "status": "error",
                        "message": "Action must be an object."
                    }, ensure_ascii=False)

                # The Web editor only exposes a subset of action fields. Merge
                # that patch into the stored action so scheduler metadata such
                # as notify_session_id, silent, and channel-specific delivery
                # fields survive unrelated edits.
                action = dict(original_action)
                action.update(action_patch)
                action_type = action.get("type")
                if action_type == "send_message":
                    action.pop("task_description", None)
                    action.pop("silent", None)
                elif action_type == "agent_task":
                    action.pop("content", None)

                old_channel = original_action.get("channel_type", "web")
                channel_type = action.get("channel_type") or old_channel
                action["channel_type"] = channel_type

                if not action.get("receiver"):
                    return json.dumps({
                        "status": "error",
                        "message": "Receiver is required. Please create a new task through the chat interface."
                    }, ensure_ascii=False)

                # Web tasks target a chat session, which is not a switchable
                # delivery identity — freeze channel/receiver for them.
                if old_channel == "web" or channel_type == "web":
                    if old_channel != channel_type:
                        return json.dumps({
                            "status": "error",
                            "message": f"Cannot change channel type from '{old_channel}' to '{channel_type}'.",
                        }, ensure_ascii=False)
                else:
                    # IM task: the editor lets the user re-point it at another
                    # channel instance / recipient. Only allow a target that is
                    # in the trusted recipient directory, and take its identity
                    # from the store rather than trusting the request body — the
                    # same rule the create endpoint enforces. This also keeps an
                    # app-scoped id (a Feishu open_id) bound to the instance that
                    # actually owns it.
                    new_instance = (action.get("instance_id") or channel_type).strip()
                    new_receiver = action.get("receiver")
                    changed = (
                        channel_type != old_channel
                        or new_instance != (original_action.get("instance_id") or old_channel)
                        or new_receiver != original_action.get("receiver")
                    )
                    if changed:
                        from agent.tools.scheduler.integration import get_recipient_store
                        target = get_recipient_store().get(new_instance, new_receiver)
                        if not target:
                            return json.dumps({
                                "status": "error",
                                "message": "recipient is not in the trusted directory",
                            }, ensure_ascii=False)
                        action["channel_type"] = target["channel_type"]
                        action["instance_id"] = target.get("instance_id") or new_instance
                        action["receiver"] = target["receiver"]
                        action["receiver_name"] = target.get("name") or target["receiver"]
                        action["is_group"] = bool(target.get("is_group", False))
                        action["notify_session_id"] = target.get("session_id") or target["receiver"]
                        # No need to touch agent_id: the effective owner of an IM
                        # task is derived from instance_id's live binding, so
                        # switching the delivery instance here already moves the
                        # task to the new instance's Agent on the next tick.
                updates["action"] = action
                
                # If schedule was not updated but action was, ensure next_run_at exists
                if "schedule" not in body and "next_run_at" not in original_task:
                    merged = dict(original_task)
                    merged.update(updates)
                    temp_service = SchedulerService(store, lambda t: None)
                    next_run = temp_service._calculate_next_run(merged, datetime.now())
                    if next_run:
                        updates["next_run_at"] = next_run.isoformat()
            
            store.update_task(task_id, updates)
            task = store.get_task(task_id)
            return json.dumps({"status": "success", "task": task}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler update error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerDeleteHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})
            
            store = _global_task_store()
            if store is None:
                return json.dumps({"status": "error", "message": "Scheduler store unavailable"})
            store.delete_task(task_id)
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerInstancesHandler:
    """List channel instances that can be a scheduled-task delivery target.

    The task-create flow picks an instance first, then a recipient within it.
    This returns every external IM instance (web/unknown excluded, as web is not
    a stable delivery target), each with a human-friendly name and how many
    trusted recipients it currently has, so the console can show all instances
    and note the empty ones rather than hiding them.

    Legacy single-instance installs surface here too: each legacy channel type
    resolves to one instance whose id equals the type, so an old setup gets one
    entry per configured channel with no config change.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.scheduler.integration import get_recipient_store
            from channel.channel_instances import (
                resolve_channel_instances,
                _CHANNEL_TYPE_LABELS,
            )
            from agent import team

            # Count recipients per instance so the picker can flag empty ones.
            counts = {}
            for r in get_recipient_store().list():
                iid = r.get("instance_id") or r.get("channel_type") or ""
                counts[iid] = counts.get(iid, 0) + 1

            resolved = team.resolve(conf())
            instances = []
            seen = set()
            for inst in resolve_channel_instances(resolved):
                if inst.channel_type in ("web", "unknown"):
                    continue
                if inst.instance_id in seen:
                    continue
                seen.add(inst.instance_id)
                creds = inst.credentials or {}
                # Prefer the user label; then a credential-carried bot name; then
                # the type's friendly label (so a legacy install without a name
                # reads "微信" not "weixin"); finally the raw id.
                friendly = (
                    inst.name
                    or creds.get("feishu_bot_name")
                    or creds.get("wecom_bot_id")
                    or _CHANNEL_TYPE_LABELS.get(inst.channel_type)
                    or inst.instance_id
                )
                instances.append({
                    "instance_id": inst.instance_id,
                    "channel_type": inst.channel_type,
                    "name": friendly,
                    # Friendly channel-type label (e.g. "微信"), shown on the
                    # right of the picker since the left is the instance name.
                    "channel_label": _CHANNEL_TYPE_LABELS.get(inst.channel_type, inst.channel_type),
                    "agent_id": inst.agent_id or "",
                    "recipient_count": counts.get(inst.instance_id, 0),
                })
            return json.dumps({"status": "success", "instances": instances}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler instances error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerRecipientsHandler:
    """List the trusted cross-channel recipients learned from inbound messages.

    These are people/groups who have already contacted the Agent on an IM
    channel (feishu / wecom_bot / dingtalk / weixin / ...). The Web console uses
    this directory to let a user hand-create a scheduled task that delivers to
    someone on another channel, instead of guessing raw receiver ids.

    Web sessions are intentionally excluded from the directory (they are
    ephemeral and not stable identities), so this endpoint only ever returns
    external-channel recipients.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.scheduler.integration import get_recipient_store

            # The directory is shared across Agents, so it is one read. Each entry
            # already carries instance_id (the exact channel login that saw the
            # person); we enrich it with a human-friendly instance label so the
            # picker reads as "instance · name". The owning Agent is derived at
            # create time from the instance, so it is not needed here.
            recipients = get_recipient_store().list()
            instance_meta = self._instance_meta()
            for item in recipients:
                inst_id = item.get("instance_id") or item.get("channel_type") or ""
                meta = instance_meta.get(inst_id)
                item["instance_name"] = (meta or {}).get("name") or inst_id
            return json.dumps({"status": "success", "recipients": recipients}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler recipients error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    @staticmethod
    def _instance_meta():
        """Map instance_id -> {name} for every configured channel instance, so a
        recipient can be shown by a readable channel label rather than a raw id.
        Best-effort: a missing map just falls back to the id."""
        meta = {}
        try:
            from channel.channel_instances import resolve_channel_instances
            from agent import team
            resolved = team.resolve(conf())
            for inst in resolve_channel_instances(resolved):
                # Prefer the user-set label; then a bot name the channel carries;
                # else the instance id, which at least names the type.
                creds = inst.credentials or {}
                friendly = (
                    inst.name
                    or creds.get("feishu_bot_name")
                    or creds.get("wecom_bot_id")
                    or inst.instance_id
                )
                meta[inst.instance_id] = {"name": friendly}
        except Exception as e:
            logger.debug(f"[WebChannel] Instance meta unavailable: {e}")
        return meta


class SchedulerCreateHandler:
    """Hand-create a scheduled task from the Web console.

    The chat-driven path (SchedulerTool) remains the way to create a task that
    delivers back to the current conversation. This endpoint exists for the
    "actively reach someone else" case: the user picks a trusted recipient and
    the task is stored to deliver on that recipient's channel.

    The recipient is identified by ``instance_id`` (the specific channel instance
    that saw them), not just a channel type: two instances of one channel type
    have separate logins and receiver id spaces, and delivery must go back out
    through the exact instance. The owning Agent is *derived* from that instance's
    binding rather than chosen separately, since a channel instance already binds
    to one Agent. A legacy single-instance channel has ``instance_id ==
    channel_type``, so an old client that only sends a channel type still works.

    Cross-channel delivery is only allowed to a recipient already present in the
    trusted directory; an arbitrary receiver id is rejected. The recipient's
    stable identity (receiver_name / is_group / session_id) is filled from the
    directory rather than trusted from the request body.
    """

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            import uuid
            from datetime import datetime
            from agent.tools.scheduler.scheduler_service import SchedulerService
            from agent.tools.scheduler.integration import get_recipient_store

            body = json.loads(web.data() or b"{}")

            name = (body.get("name") or "").strip()
            if not name:
                return json.dumps({"status": "error", "message": "name is required"})

            schedule = body.get("schedule")
            if not isinstance(schedule, dict) or not schedule.get("type"):
                return json.dumps({"status": "error", "message": "schedule is required"})

            action_in = body.get("action")
            if not isinstance(action_in, dict):
                return json.dumps({"status": "error", "message": "action is required"})

            action_type = action_in.get("type")
            if action_type not in ("send_message", "agent_task"):
                return json.dumps({"status": "error", "message": "unsupported action type"})

            channel_type = (action_in.get("channel_type") or "").strip()
            receiver = (action_in.get("receiver") or "").strip()
            # instance_id identifies the exact channel login; fall back to the
            # channel type for a legacy single-instance channel / older client.
            instance_id = (action_in.get("instance_id") or "").strip() or channel_type
            if not channel_type or not receiver:
                return json.dumps({"status": "error", "message": "channel_type and receiver are required"})
            if channel_type in ("web", "unknown"):
                # Web sessions are ephemeral and not a stable delivery target;
                # the console only creates tasks for external-channel recipients.
                return json.dumps({"status": "error", "message": "web is not a valid cross-channel recipient"})

            content = (action_in.get("content") or action_in.get("task_description") or "").strip()
            if not content:
                return json.dumps({"status": "error", "message": "content is required"})

            # The receiver must be in the shared trusted directory; identity
            # fields are taken from the store, never trusted from the client.
            target = get_recipient_store().get(instance_id, receiver)
            if not target:
                return json.dumps({
                    "status": "error",
                    "message": "recipient is not in the trusted directory",
                })

            # Record the current owner as a hint only. For an IM task the true
            # owner is derived at run time from the delivery instance's binding
            # (see effective_task_agent_id), so re-binding the channel moves the
            # task with no rewrite; this stored value is just a fallback for when
            # the instance later has no explicit binding.
            agent_id = _resolve_instance_agent_id(target.get("instance_id") or instance_id)

            action = {
                "type": action_type,
                "receiver": target["receiver"],
                "receiver_name": target.get("name") or target["receiver"],
                "is_group": bool(target.get("is_group", False)),
                "channel_type": target["channel_type"],
                "instance_id": target.get("instance_id") or instance_id,
                "notify_session_id": target.get("session_id") or target["receiver"],
            }
            if action_type == "send_message":
                action["content"] = content
            else:
                action["task_description"] = content
                if action_in.get("silent"):
                    action["silent"] = True

            task_data = {
                "id": str(uuid.uuid4())[:8],
                "name": name,
                "agent_id": agent_id,
                "enabled": bool(body.get("enabled", True)),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "schedule": schedule,
                "action": action,
            }

            store = _global_task_store()
            if store is None:
                return json.dumps({"status": "error", "message": "Scheduler store unavailable"})
            temp_service = SchedulerService(store, lambda t: None)
            next_run = temp_service._calculate_next_run(task_data, datetime.now())
            if not next_run:
                return json.dumps({
                    "status": "error",
                    "message": "Cannot calculate next run time. Please check the schedule (cron format, or a one-time time already in the past).",
                }, ensure_ascii=False)
            task_data["next_run_at"] = next_run.isoformat()

            store.add_task(task_data)
            return json.dumps({"status": "success", "task": task_data}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler create error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _agent_admin_service():
    from agent.admin import AgentAdminService
    return AgentAdminService(os.path.join(get_data_root(), "config.json"))


def _bind_channel_instance(channel_type: str, instance_id: str = "", agent_id: str = "", members=None):
    """Point one channel instance at an Agent (and team), hot-swapping without a restart.

    The binding lives on the channel instance itself (channel_instances[].agent_id
    in team.json), the single source of truth for routing. For a single-instance
    channel the instance id is just the channel type. An empty agent_id unbinds it
    (falls back to the default Agent).

    Rebinding only changes *which* Agent inbound messages route to — the
    credentials, connection, and scheduled tasks are untouched. Tasks live in
    the global store and carry their own ``agent_id``; flipping this picker
    must never migrate or rewrite them. We persist the new binding and then set
    ``bound_agent_id`` live on the running channel; the next inbound message
    reads the updated value. This avoids the reconnect storm a restart caused
    when the user flipped the picker a few times.
    """
    from channel.channel_instances import upsert_instance

    ctype = (channel_type or "").strip().lower()
    if not ctype:
        raise ValueError("channel_type is required")
    target_id = (instance_id or "").strip() or ctype
    agent_id = (agent_id or "").strip()

    inst = upsert_instance(
        conf(),
        channel_type=ctype,
        instance_id=target_id,
        agent_id=agent_id,
        members=members,
    )

    try:
        mgr = _live_channel_manager()
        channel = mgr.get_channel(target_id) if mgr else None
        if channel is not None:
            # Live-update owner + team on the running instance. Empty owner means
            # "follow the default Agent". No restart: this only changes routing.
            channel.bound_agent_id = agent_id
            channel.members = list(inst.members or [])
            logger.info(
                f"[WebChannel] Channel '{target_id}' rebound to "
                f"'{agent_id or 'default'}' with team {inst.members or []} (no restart)"
            )
    except Exception as e:
        logger.error(
            f"[WebChannel] Failed to hot-rebind channel '{target_id}': {e}",
            exc_info=True,
        )

    return {
        "instance_id": inst.instance_id,
        "agent_id": inst.agent_id,
        "members": list(inst.members or []),
    }


def _reload_agent_runtime(service, changed_agent_ids=None) -> None:
    """Re-point the live runtime at a freshly loaded roster.

    This runs inside the roster-edit request, so it must stay cheap. The old
    implementation tore everything down - stop every scheduler, drop every
    cached session, then rebuild all of them - which grew linearly with the
    number of Agents (each rebuild reloads dozens of skills). Editing one
    Agent's name should not cost a full-fleet reload.

    Instead we reconcile incrementally:
      * swap the registry/router (always cheap),
      * start a scheduler only for Agents that gained one, stop those that
        disappeared, and leave already-running ones untouched,
      * evict only the sessions of the Agents that actually changed, so their
        next turn picks up the new name / model / persona. Everyone else keeps
        their warm cache.

    ``changed_agent_ids`` narrows the session eviction to just the edited
    Agents. When omitted we fall back to evicting nothing extra beyond the
    add/remove diff, since pure metadata edits without an id (e.g. binding
    changes) touch no cached runtime.
    """
    from agent.registry import set_agent_registry
    from agent.routing import AgentRouter, set_agent_router

    settings = service._load()
    registry = service._registry(settings)
    router = AgentRouter.from_config(settings, registry)
    set_agent_registry(registry)
    set_agent_router(router)

    from bridge.bridge import Bridge
    bridge = Bridge()
    agent_bridge = getattr(bridge, "_agent_bridge", None)
    if agent_bridge is None:
        return

    agent_bridge.agent_registry = registry
    agent_bridge.agent_router = router

    # Reconcile schedulers against what is already running, rather than
    # stopping and recreating the whole set.
    from agent.tools.scheduler.integration import init_scheduler, stop_scheduler
    live_ids = {p.id for p in registry.list(include_disabled=False)}
    previously = set(agent_bridge.scheduler_agent_ids)

    for agent_id in previously - live_ids:
        try:
            stop_scheduler(agent_id)
        except Exception as e:
            logger.warning(f"[WebChannel] stop_scheduler({agent_id}) failed: {e}")
        agent_bridge.scheduler_agent_ids.discard(agent_id)

    for profile in registry.list(include_disabled=False):
        if profile.id in previously:
            continue  # already has a running scheduler; init_scheduler is a no-op
        if init_scheduler(agent_bridge, profile.workspace, profile.id):
            agent_bridge.scheduler_agent_ids.add(profile.id)
    agent_bridge.scheduler_initialized = bool(agent_bridge.scheduler_agent_ids)

    # Drop cached runtimes only for the Agents whose definition changed, so the
    # edit takes effect on their next turn without wiping everyone's session.
    for agent_id in (changed_agent_ids or []):
        try:
            agent_bridge.clear_agent(agent_id)
        except Exception as e:
            logger.warning(f"[WebChannel] clear_agent({agent_id}) failed: {e}")


class AgentsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            return json.dumps(
                {"status": "success", **_annotate_avatar_revs(_agent_admin_service().snapshot())},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Agents API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            service = _agent_admin_service()
            revision = body.get("revision") or None
            if action == "create":
                result = service.create_agent(
                    agent_id=body.get("id", ""),
                    name=body.get("name", ""),
                    # Blank means "put it where a new one goes", which is what
                    # the console sends: it asks for a name, not a path.
                    workspace=body.get("workspace") or None,
                    clone_from=body.get("clone_from") or None,
                    avatar=body.get("avatar") or None,
                    description=body.get("description") or None,
                    skills=body.get("skills"),
                    knowledge=body.get("knowledge"),
                    knowledge_mode=body.get("knowledge_mode") or None,
                    revision=revision,
                )
            elif action == "update":
                updates = {
                    "name": body.get("name"),
                    "enabled": body.get("enabled"),
                    "make_default": bool(body.get("make_default", False)),
                    "avatar": body.get("avatar"),
                    "description": body.get("description"),
                    "model": body.get("model"),
                    "bot_type": body.get("bot_type"),
                    "revision": revision,
                }
                if "skills" in body:
                    updates["skills"] = body.get("skills")
                if "knowledge" in body:
                    updates["knowledge"] = body.get("knowledge")
                result = service.update_agent(body.get("id", ""), **updates)
            elif action == "archive":
                result = service.archive_agent(body.get("id", ""), revision=revision)
            elif action == "delete":
                result = service.delete_agent(body.get("id", ""), revision=revision)
            elif action == "set_knowledge_mode":
                # A filesystem toggle (symlink vs own dir), not a roster edit, so
                # it doesn't participate in the roster revision guard.
                result = service.set_knowledge_mode(
                    body.get("id", ""), body.get("mode", "")
                )
            elif action == "bind_channel_instance":
                # members: list => set team; omitted/None => leave team untouched
                raw_members = body.get("members", None)
                members = raw_members if isinstance(raw_members, list) else None
                result = _bind_channel_instance(
                    channel_type=body.get("channel_type", ""),
                    instance_id=body.get("instance_id", ""),
                    agent_id=body.get("agent_id", ""),
                    members=members,
                )
            else:
                return json.dumps({
                    "status": "error", "message": f"unknown action: {action}"
                })
            # Only the edited Agent needs its cached runtime dropped; a create
            # has no live sessions yet. bind_channel_instance hot-updates the
            # running channel's binding in place (see _bind_channel_instance),
            # so it neither restarts a channel nor touches the roster runtime.
            if action == "bind_channel_instance":
                return json.dumps(
                    {"status": "success", "result": result},
                    ensure_ascii=False,
                )
            changed = None
            if action in ("update", "archive", "delete", "set_knowledge_mode"):
                changed = [body.get("id", "")] if body.get("id") else None
            _reload_agent_runtime(service, changed_agent_ids=changed)
            # Hand back the fresh revision so a client making rapid successive
            # edits (e.g. ticking skill checkboxes) can chain them without a
            # full reload and without tripping the stale-roster guard.
            try:
                revision_after = service.snapshot().get("revision")
            except Exception:
                revision_after = None
            return json.dumps(
                {"status": "success", "result": result, "revision": revision_after},
                ensure_ascii=False,
            )
        except Exception as e:
            from agent.admin import StaleRosterError
            code = None
            if isinstance(e, StaleRosterError):
                web.ctx.status = "409 Conflict"
                code = "stale_roster"
            logger.error(f"[WebChannel] Agents POST error: {e}")
            return json.dumps({"status": "error", "message": str(e), "code": code})


class AgentCoreFileHandler:
    def GET(self, agent_id: str, filename: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            result = _agent_admin_service().read_core_file(agent_id, filename)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self, agent_id: str, filename: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            result = _agent_admin_service().write_core_file(
                agent_id,
                filename,
                body.get("content"),
                body.get("revision", ""),
            )
            try:
                from bridge.bridge import Bridge
                agent_bridge = getattr(Bridge(), "_agent_bridge", None)
                if agent_bridge is not None:
                    agent_bridge.clear_agent(agent_id)
            except Exception as e:
                logger.warning(
                    f"[WebChannel] Failed to evict edited agent={agent_id}: {e}"
                )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            from agent.admin import StaleAgentFileError
            if isinstance(e, StaleAgentFileError):
                web.ctx.status = "409 Conflict"
            return json.dumps({"status": "error", "message": str(e)})


# An emoji costs nothing to store or serve, so it is the default way to tell
# Agents apart; an uploaded picture sets the field to this token instead and the
# bytes live beside the other shared assets.
AVATAR_IMAGE_TOKEN = "image"
AVATAR_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _avatar_path(agent_id: str) -> Optional[str]:
    from common.state_dir import shared_root

    base = shared_root() / "avatars"
    for suffix in AVATAR_TYPES:
        candidate = base / f"{agent_id}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


def _avatar_rev(agent_id: str) -> Optional[str]:
    """A cache-busting token derived from the avatar file's mtime.

    The roster revision only hashes team.json, but replacing an avatar rewrites
    an image file without touching any field there, so the revision stays put
    and the browser keeps serving the cached picture. Keying the URL on the
    file's mtime instead means every upload changes the token and the <img>
    refetches, even after a hard reload where in-memory hints are gone.
    """
    path = _avatar_path(agent_id)
    if not path:
        return None
    try:
        return str(int(os.path.getmtime(path)))
    except OSError:
        return None


def _annotate_avatar_revs(snapshot: dict) -> dict:
    """Attach ``avatar_rev`` to every Agent that carries an uploaded image."""
    for agent in snapshot.get("agents") or []:
        if agent.get("avatar") == AVATAR_IMAGE_TOKEN:
            rev = _avatar_rev(agent.get("id", ""))
            if rev:
                agent["avatar_rev"] = rev
    return snapshot


class AgentAvatarHandler:
    def GET(self, agent_id: str):
        _require_auth()
        path = _avatar_path(agent_id)
        if not path:
            web.ctx.status = "404 Not Found"
            web.header('Content-Type', 'application/json; charset=utf-8')
            return json.dumps({"status": "error", "message": "no avatar"})
        with open(path, "rb") as handle:
            data = handle.read()
        web.header('Content-Type', AVATAR_TYPES[os.path.splitext(path)[1].lower()])
        # Content-addressed by the caller via ?v=, so it can be cached hard.
        web.header('Cache-Control', 'private, max-age=86400')
        return data

    def POST(self, agent_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common.state_dir import shared_root
            from agent.registry import get_agent_registry

            get_agent_registry().get(agent_id, require_enabled=False)
            # Read the multipart body raw. web.input() decodes it as UTF-8, which
            # dies on the first non-text byte of an image (a PNG starts with the
            # byte 0x89) with "utf-8 codec can't decode byte 0x89". rawinput hands
            # back the bytes untouched, the same path the knowledge upload uses.
            params = _raw_web_input()
            upload = params.get("avatar")
            if upload is None:
                return json.dumps({"status": "error", "message": "avatar file required"})
            filename = getattr(upload, "filename", "") or ""
            raw = _read_uploaded_file_bytes(upload)
            if not raw:
                return json.dumps({"status": "error", "message": "avatar file required"})
            if len(raw) > MAX_AVATAR_BYTES:
                return json.dumps({"status": "error", "message": "avatar exceeds 2 MiB"})
            suffix = os.path.splitext(filename)[1].lower()
            if suffix not in AVATAR_TYPES:
                return json.dumps({
                    "status": "error",
                    "message": f"unsupported image type: {suffix or 'unknown'}",
                })

            base = shared_root() / "avatars"
            base.mkdir(parents=True, exist_ok=True)
            # Drop any other extension first, so one Agent never ends up with
            # two avatar files and a resolution order deciding which one wins.
            for other in AVATAR_TYPES:
                stale = base / f"{agent_id}{other}"
                if other != suffix and stale.is_file():
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            target = base / f"{agent_id}{suffix}"
            tmp = base / f".{agent_id}{suffix}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)

            service = _agent_admin_service()
            result = service.update_agent(agent_id, avatar=AVATAR_IMAGE_TOKEN)
            # An avatar is a file plus a metadata flag; it changes nothing about
            # routing, sessions or schedulers. Skipping the full runtime reload
            # keeps the upload instant instead of tearing everything down.
            # Hand back the fresh revision so the console can patch its roster in
            # place without a full reload and without going stale on the next edit.
            revision = service.snapshot().get("revision")
            return json.dumps(
                {"status": "success", "result": result, "revision": revision},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Agent avatar upload error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _annotate_sessions_with_projects(store, result: dict, agent_id: Optional[str]) -> None:
    """Attach each session's project space, and say how to group the list.

    ``group_mode`` is decided here rather than in the browser because the client
    only ever holds one page: whether more than one space is in play is a fact
    about all sessions, not about the fifty currently on screen.

    - ``time``    one space in use (the common case) - group by 今天/昨天/更早,
                  exactly as before projects existed.
    - ``project`` several spaces in use - group by project, so multi-project
                  users can find a conversation by where it belongs.
    """
    from agent.workspace import project_store
    from common.state_dir import state_root_str

    project_map = project_store.get_project_map(agent_id)
    default_workspace = state_root_str()

    for session in result.get("sessions") or []:
        path = project_map.get(session["session_id"])
        session["project"] = (
            {"path": path, "name": project_store.display_name_for(path)}
            if path else None
        )

    # Distinct spaces across every web session, default workspace included as
    # one space when any session is still using it.
    space_paths = set()
    uses_default = False
    for sid in store.list_session_ids(channel_type="web"):
        path = project_map.get(sid)
        if path:
            space_paths.add(path)
        else:
            uses_default = True

    result["space_count"] = len(space_paths) + (1 if uses_default else 0)
    result["group_mode"] = "project" if result["space_count"] > 1 else "time"
    result["default_workspace"] = default_workspace
    # The user's chosen sidebar order of spaces (project paths + the default
    # sentinel). The client uses it to sort project groups; unspecified spaces
    # fall back after the ordered ones.
    result["project_order"] = project_store.get_order()


def _as_epoch(value) -> int:
    """Best-effort convert a session timestamp into a sortable epoch int.

    New databases store ``last_active``/``created_at`` as integer Unix
    timestamps, but a workspace carried over from an older build may still hold
    them as ``'YYYY-MM-DD HH:MM:SS'`` strings. Parsing those defensively keeps
    the merged session list from crashing the whole API (which would leave the
    web sidebar empty) just because one legacy row can't be ``int()``-ed.
    """
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    from datetime import datetime

    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return 0


def _list_sessions_across_agents(page: int, page_size: int) -> dict:
    """One page of every Agent's conversations, merged.

    Sessions are stored one database per Agent, so "all conversations" is a
    merge across files rather than a query. Each Agent is asked for as many rows
    as the requested page could possibly draw from it, because any of them can
    supply the row that sorts into that page.

    Presenting them in one list is what keeps a second Agent from feeling like a
    second account: the alternative, switching the whole console to look at
    another Agent's conversations, makes the roster a tenant selector.
    """
    from agent.memory import get_conversation_store
    from agent.registry import get_agent_registry
    from agent.workspace import project_store, session_prefs
    from common.state_dir import state_root_str

    take = max(1, page) * page_size
    merged: List[dict] = []
    total = 0
    space_paths = set()
    uses_default = False
    try:
        members_index = session_prefs.members_index()
    except Exception as e:
        # Faces are decoration; losing them must not cost the user the list.
        logger.warning(f"[WebChannel] Could not read session rosters: {e}")
        members_index = {}

    for profile in get_agent_registry().list(include_disabled=False):
        try:
            store = get_conversation_store(profile.workspace)
            chunk = store.list_sessions(channel_type="web", page=1, page_size=take)
            project_map = project_store.get_project_map(profile.id)
            session_ids = store.list_session_ids(channel_type="web")
        except Exception as e:
            # One unreadable workspace must not blank out the whole list; the
            # other Agents' conversations are still perfectly readable.
            logger.warning(
                f"[WebChannel] Skipping sessions for agent={profile.id}: {e}"
            )
            continue

        total += chunk.get("total", 0)
        badge = _agent_badge(profile)
        for session in chunk.get("sessions") or []:
            path = project_map.get(session["session_id"])
            session["agent"] = badge
            # Only a conversation with more than one Agent in it needs faces in
            # the list; a solo one reads better as a plain row, exactly as it
            # did before there was a roster.
            roster = _roster_from_members(
                profile.id, members_index.get((profile.id, session["session_id"]))
            )
            if len(roster) > 1:
                session["participants"] = roster
            session["project"] = (
                {"path": path, "name": project_store.display_name_for(path)}
                if path else None
            )
            merged.append(session)

        for sid in session_ids:
            path = project_map.get(sid)
            if path:
                space_paths.add(path)
            else:
                uses_default = True

    # One row per conversation. A session id can exist in more than one Agent's
    # store (an older client once let a conversation change hands mid-way, and
    # each side kept the turns it saw); showing it twice makes both rows light
    # up as "selected". Keep the copy holding the bulk of the conversation —
    # that's the one the user recognises — and let the newest break a tie.
    by_id: Dict[str, dict] = {}
    for session in merged:
        sid = session.get("session_id")
        kept = by_id.get(sid)
        if kept is None or (
            (int(session.get("msg_count") or 0), _as_epoch(session.get("last_active")))
            > (int(kept.get("msg_count") or 0), _as_epoch(kept.get("last_active")))
        ):
            by_id[sid] = session
    total -= len(merged) - len(by_id)
    merged = list(by_id.values())

    # Same ordering the per-Agent query applies, so a merged page looks exactly
    # like a single Agent's page does.
    merged.sort(
        key=lambda s: (
            0 if s.get("pinned") else 1,
            -_as_epoch(s.get("last_active")),
        )
    )
    offset = (max(1, page) - 1) * page_size
    result = {
        "sessions": merged[offset:offset + page_size],
        "total": total,
        "page": max(1, page),
        "page_size": page_size,
        "has_more": total > offset + page_size,
        "space_count": len(space_paths) + (1 if uses_default else 0),
        "default_workspace": state_root_str(),
        "project_order": project_store.get_order(),
    }
    result["group_mode"] = "project" if result["space_count"] > 1 else "time"
    return result


class SessionsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(
                page='1', page_size='50', agent_id='', agent='', scope=''
            )
            page = int(params.page)
            page_size = int(params.page_size)
            if (params.scope or '').strip() == 'all':
                result = _list_sessions_across_agents(page, page_size)
                return json.dumps({"status": "success", **result}, ensure_ascii=False)

            agent_id = _request_agent_id(params)
            from agent.memory import get_conversation_store
            from agent.registry import get_agent_registry
            store = get_conversation_store(
                _get_workspace_root(agent_id=agent_id)
            )
            result = store.list_sessions(
                channel_type="web",
                page=page,
                page_size=page_size,
            )
            _annotate_sessions_with_projects(store, result, agent_id)
            badge = _agent_badge(
                get_agent_registry().get(agent_id or None, require_enabled=False)
            )
            for session in result.get("sessions") or []:
                session["agent"] = badge
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Sessions API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionDetailHandler:
    def DELETE(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        logger.info(f"[WebChannel] DELETE session request: {session_id}")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            params = web.input(agent_id='')
            agent_id = _request_agent_id(params)

            # Stop any in-flight run first: a reply that lands after the delete
            # would otherwise keep burning tokens for a session nobody can see.
            try:
                from agent.protocol import get_cancel_registry
                from bridge.bridge import Bridge
                scoped = Bridge().get_agent_bridge().scoped_session_key(session_id)
                cancelled = get_cancel_registry().cancel_session(scoped)
                if cancelled:
                    logger.info(
                        f"[WebChannel] Cancelled {cancelled} in-flight request(s) "
                        f"for deleted session {session_id}"
                    )
            except Exception as e:
                logger.warning(f"[WebChannel] Cancel on delete failed: {e}")

            from agent.memory import get_conversation_store
            store = get_conversation_store(_get_workspace_root(agent_id=agent_id))
            store.clear_session(session_id)

            # Drop the session's side stores too. Left behind, a stale project
            # binding would keep inflating the "how many spaces are in use"
            # count that decides how the session list is grouped.
            try:
                from agent.workspace import project_store, session_prefs
                project_store.forget_session(session_id)
                session_prefs.forget_session(session_id)
            except Exception as e:
                logger.debug(f"[WebChannel] Session side-store cleanup skipped: {e}")

            # Also remove the Agent instance from AgentBridge if exists
            try:
                from bridge.bridge import Bridge
                ab = Bridge().get_agent_bridge()
                ab.clear_session(session_id, agent_id=agent_id)
            except Exception:
                pass

            channel = WebChannel()
            # Drop messages still waiting in the channel queue: processing them
            # after the delete would recreate the session from scratch.
            try:
                channel.cancel_session(session_id)
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to drain queue on delete: {e}")
            channel.session_queues.pop(
                channel._session_queue_key(session_id, agent_id), None
            )

            logger.info(f"[WebChannel] Session deleted: {session_id}")
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self, session_id: str):
        """Update a session's title and/or its pinned flag."""
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            body = json.loads(web.data())
            agent_id = _request_agent_id(body)
            title = (body.get("title") or "").strip()
            pinned = body.get("pinned")
            if not title and pinned is None:
                return json.dumps({"status": "error", "message": "title or pinned required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store(_get_workspace_root(agent_id=agent_id))

            found = True
            if title:
                found = store.rename_session(session_id, title)
            if pinned is not None:
                found = store.set_pinned(session_id, bool(pinned)) and found
            if not found:
                # A session only gets a row once its first message is stored, so
                # this is also what a pin on a brand-new empty chat looks like.
                return json.dumps({"status": "error", "message": "session not found"})
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session update error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _session_model_catalog() -> List[dict]:
    """Providers a session may switch to, newest-first within each provider.

    Only providers with a credential on file are offered: listing one without an
    API key would let the user pick a model that fails on the next message.
    The globally active provider is always included, even if its key lives in
    the environment rather than in config.json.
    """
    local_config = conf()
    active_bot_type = local_config.get("bot_type") or ""
    active_provider = "openai" if active_bot_type == const.CHATGPT else active_bot_type
    if local_config.get("use_linkai") and local_config.get("linkai_api_key"):
        active_provider = "linkai"
    active_model = str(local_config.get("model") or "").strip()
    catalog_map = model_catalog.get_catalog_map()
    hidden_map = model_catalog.get_hidden_map()

    catalog: List[dict] = []
    for pid, pinfo in PROVIDER_MODELS.items():
        if pid == "custom" or not pinfo.get("models"):
            continue
        key_field = pinfo.get("api_key_field")
        has_key = bool(key_field and str(local_config.get(key_field) or "").strip())
        if not has_key and pid != active_provider:
            continue
        # Overlay onto the presets, then keep only text-tagged entries — only
        # those belong in the conversation switcher. Without an overlay this is
        # just the preset model list.
        if catalog_map.get(pid) or hidden_map.get(pid):
            effective = ModelsHandler._merged_catalog(
                pid, ModelsHandler._preset_seed(pid), catalog_map, hidden_map)
            models = [e["name"] for e in effective if "text" in (e.get("capabilities") or [])]
        else:
            models = list(pinfo["models"])
        if not models:
            continue
        # The user can pin a custom model name to a built-in provider (via the
        # global config / capability "custom model" field). That model won't be
        # in the preset list, so surface it here for the active provider so the
        # chat picker can both display and re-select it.
        if pid == active_provider and active_model and active_model not in models:
            models.insert(0, active_model)
        catalog.append({
            "id": pid,
            "label": pinfo["label"],
            "models": models,
        })

    # User-defined OpenAI-compatible providers carry their own credentials, so
    # offer any that have a key on file (or are the active provider). Their model
    # list combines the provider's configured default with the globally active
    # model when this custom provider is the one in use — otherwise a custom
    # provider added without a preset model would be unselectable in chat.
    try:
        from models.custom_provider import get_custom_providers
        for cp in get_custom_providers():
            cid = cp.get("id")
            if not cid:
                continue
            pid = f"custom:{cid}"
            is_active = pid == active_provider
            # Mirror the config page's "configured" test (_custom_provider_cards):
            # a keyless-but-based endpoint (self-hosted / gateway) is valid, so
            # having an api_base counts just like having a key.
            configured = bool(str(cp.get("api_base") or "").strip()) \
                or bool(str(cp.get("api_key") or "").strip())
            if not configured and not is_active:
                continue
            entries = catalog_map.get(pid)
            if entries:
                models = [e["name"] for e in entries if "text" in (e.get("capabilities") or [])]
            else:
                models = []
                cp_model = str(cp.get("model") or "").strip()
                if cp_model:
                    models.append(cp_model)
            if is_active and active_model and active_model not in models:
                models.insert(0, active_model)
            if not models:
                # Nothing concrete to select yet (no default model and not the
                # active provider) — skip rather than render an empty group.
                continue
            name = cp.get("name") or cid
            catalog.append({
                "id": pid,
                "label": {"zh": name, "en": name},
                "models": models,
            })
    except Exception as e:
        logger.debug(f"[WebChannel] custom providers unavailable: {e}")

    return catalog


def _session_settings_state(session_id: str, agent_id: Optional[str]) -> dict:
    """Effective model + permission for a session, and what it can be changed to.

    ``source`` tells the UI whether a value is this conversation's own choice or
    inherited, so it can show "follow global" as a real, selectable state instead
    of silently duplicating the global value onto every session.

    The model resolves the same way the runtime does (see AgentLLMModel.model):
    the conversation's pin, else the owning Agent's own default model, else the
    global config. ``source`` is ``session`` / ``agent`` / ``global`` accordingly,
    and ``agent`` carries the Agent's default when it has one, so a fresh chat
    with a specialist Agent shows the model it will really answer with.
    """
    from agent.workspace import session_prefs

    local_config = conf()
    prefs = session_prefs.get_prefs(session_id, agent_id)

    global_bot_type = local_config.get("bot_type") or ""
    global_provider = "openai" if global_bot_type == const.CHATGPT else global_bot_type
    if local_config.get("use_linkai") and local_config.get("linkai_api_key"):
        global_provider = "linkai"
    global_model = local_config.get("model") or ""
    global_permission = permission_global_mode()

    # The default Agent never has a model of its own: it *is* the global choice.
    agent_default = None
    try:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
        profile = registry.get(agent_id or None, require_enabled=False)
        if profile.id != registry.default_agent_id and profile.model:
            agent_default = {
                "model": profile.model,
                "provider": profile.bot_type or global_provider,
            }
    except Exception as e:
        logger.debug(f"[WebChannel] agent default model unavailable: {e}")

    if prefs.get("model"):
        effective_model, effective_provider, source = prefs["model"], prefs.get("provider"), "session"
    elif agent_default:
        effective_model, effective_provider, source = agent_default["model"], agent_default["provider"], "agent"
    else:
        effective_model, effective_provider, source = global_model, global_provider, "global"

    return {
        "model": {
            "model": effective_model,
            "provider": effective_provider or global_provider,
            "source": source,
            "global": {"model": global_model, "provider": global_provider},
            "agent": agent_default,
            "providers": _session_model_catalog(),
        },
        "permission": {
            "mode": (
                permission_normalize_mode(prefs["permission"], global_permission)
                if prefs.get("permission") else global_permission
            ),
            "source": "session" if prefs.get("permission") else "global",
            "global": global_permission,
            "modes": list(PERMISSION_MODES),
        },
        "team": _session_team_state(prefs, agent_id),
    }


def _session_team_state(prefs: dict, agent_id: Optional[str]) -> dict:
    """Who else is on this conversation, and who could be added.

    An archived member is reported but marked unavailable rather than dropped,
    so the roster the user set is what the roster page shows.
    """
    from agent.registry import get_agent_registry

    registry = get_agent_registry()
    owner_id = registry.get(agent_id or None, require_enabled=False).id
    members = []
    for member_id in prefs.get("members") or []:
        try:
            profile = registry.get(member_id, require_enabled=False)
        except Exception:
            members.append({"id": member_id, "name": member_id, "available": False})
            continue
        members.append({
            **_agent_badge(profile),
            "available": profile.enabled and profile.id != owner_id,
        })
    return {
        "owner": _agent_badge(registry.get(owner_id, require_enabled=False)),
        "members": members,
        "candidates": [
            _agent_badge(profile)
            for profile in registry.list(include_disabled=False)
            if profile.id != owner_id
        ],
    }


class SessionSettingsHandler:
    """Per-session model and permission overrides.

    Both are stored outside the sessions table (see session_prefs) so they can be
    set before the conversation has its first message, and both fall back to the
    global config when unset.
    """

    def GET(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            params = web.input(agent='', agent_id='')
            state = _session_settings_state(
                session_id, params.agent or params.agent_id or None
            )
            return json.dumps({"status": "success", **state}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Session settings read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self, session_id: str):
        """Set or clear this session's model / permission.

        Send ``null`` for a field to drop the override and follow the global
        setting again. ``model`` and ``provider`` move together: a model without
        its provider would be routed by the global bot type.
        """
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.workspace import session_prefs
            body = json.loads(web.data() or b"{}")
            agent_id = body.get("agent") or body.get("agent_id")

            updates = {}
            if "permission" in body:
                mode = body.get("permission")
                updates["permission"] = (
                    permission_normalize_mode(mode) if mode else None
                )
            if "model" in body or "provider" in body:
                model = (body.get("model") or "").strip() or None
                provider = (body.get("provider") or "").strip() or None
                # Clearing the model clears its provider too: a pinned provider
                # with no model would route the global model to the wrong vendor.
                updates["model"] = model
                updates["provider"] = provider if model else None
            if "members" in body:
                raw = body.get("members")
                if raw is None:
                    updates["members"] = None
                elif isinstance(raw, list):
                    updates["members"] = [
                        str(item).strip() for item in raw if str(item).strip()
                    ]
                else:
                    return json.dumps({
                        "status": "error",
                        "message": "members must be a list of agent ids",
                    })

            if not updates:
                return json.dumps({
                    "status": "error",
                    "message": "permission, model, provider or members required",
                })

            session_prefs.set_prefs(session_id, agent_id, **updates)

            # Retarget the live agent so the change lands on the next message
            # without waiting for a fresh get_agent.
            try:
                from bridge.bridge import Bridge
                ab = Bridge().get_agent_bridge()
                agent = ab.get_cached_agent(session_id, agent_id)
                if agent is not None:
                    ab.apply_session_prefs(agent, session_id, agent_id)
            except Exception as e:
                logger.debug(f"[WebChannel] session prefs apply-to-agent skipped: {e}")

            logger.info(
                f"[WebChannel] Session settings updated: sid={session_id}, {updates}"
            )
            state = _session_settings_state(session_id, agent_id)
            return json.dumps({"status": "success", **state}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Session settings update error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionTitleHandler:
    def POST(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            body = json.loads(web.data())
            agent_id = _request_agent_id(body)
            user_message = body.get("user_message", "")
            assistant_reply = body.get("assistant_reply", "")
            if not user_message:
                return json.dumps({"status": "error", "message": "user_message required"})

            title = _generate_session_title(user_message, assistant_reply, session_id)

            from agent.memory import get_conversation_store
            store = get_conversation_store(_get_workspace_root(agent_id=agent_id))
            updated = store.rename_session(session_id, title)
            logger.info(f"[WebChannel] Session title set: sid={session_id}, title='{title}', db_updated={updated}")

            return json.dumps({"status": "success", "title": title}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Title generation error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class PromptOptimizeHandler:
    """Optimize a colloquial user prompt into a structured AI-ready instruction."""

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            user_input = (body.get("input") or "").strip()
            if not user_input:
                return json.dumps({"status": "error", "message": "input required"})

            context_messages = body.get("context_messages", None)

            from agent.chat.session_service import optimize_prompt
            optimized = optimize_prompt(user_input, context_messages)

            return json.dumps(
                {"status": "success", "optimized": optimized},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Prompt optimization error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionClearContextHandler:
    def POST(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            params = web.input(agent_id='')
            raw_body = web.data()
            body = json.loads(raw_body) if raw_body else {}
            agent_id = _request_agent_id(body) or _request_agent_id(params)

            from agent.memory import get_conversation_store
            store = get_conversation_store(_get_workspace_root(agent_id=agent_id))
            new_seq = store.clear_context(session_id)

            # Delete the agent instance so a fresh one is created on the next message
            try:
                from bridge.bridge import Bridge
                bridge = Bridge()
                ab = bridge.get_agent_bridge()
                ab.clear_session(session_id, agent_id=agent_id)
            except Exception:
                pass

            return json.dumps({"status": "success", "context_start_seq": new_seq})
        except Exception as e:
            logger.error(f"[WebChannel] Clear context error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionContextUsageHandler:
    def GET(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            # Live agent instances are keyed by (agent_id, session_id), so a
            # non-default agent's context is only found when its id is supplied.
            # Without this the lookup falls back to the default agent and always
            # reports available=false for e.g. content-writer sessions.
            from urllib.parse import parse_qs
            agent_id = _request_agent_id(parse_qs(web.ctx.env.get("QUERY_STRING") or ""))

            from bridge.bridge import Bridge
            bridge = Bridge()
            ab = bridge.get_agent_bridge()
            # peek_agent, not get_agent: the latter builds the agent on miss
            # (MCP + skills), and this endpoint is called on hover.
            agent = ab.peek_agent(session_id, agent_id=agent_id)
            if agent is None:
                # No live context yet — a fresh session, or one just cleared
                # (clear_context drops the instance).
                return json.dumps({"status": "success", "available": False})

            usage = agent.get_context_usage()
            usage["status"] = "success"
            return json.dumps(usage)
        except Exception as e:
            logger.error(f"[WebChannel] Context usage error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionCompactContextHandler:
    """Synchronous manual compaction (same logic as the /compact command).

    Runs compact_context() on the live agent and returns the refreshed usage so
    the frontend can redraw the pie without a separate fetch. peek_agent (not
    get_agent) so an empty session is a no-op instead of building an agent.
    """

    def POST(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            # Match the agent that owns this session (instances are keyed by
            # (agent_id, session_id)); otherwise compaction targets the wrong
            # agent and no-ops for non-default agents.
            params = web.input(agent_id='')
            raw_body = web.data()
            body = json.loads(raw_body) if raw_body else {}
            agent_id = _request_agent_id(body) or _request_agent_id(params)

            from bridge.bridge import Bridge
            bridge = Bridge()
            ab = bridge.get_agent_bridge()
            agent = ab.peek_agent(session_id, agent_id=agent_id)
            if agent is None:
                # No live context — nothing to compact.
                return json.dumps({
                    "status": "success", "ok": False, "available": False,
                    "compacted_turns": 0, "before": 0, "after": 0, "usage": None,
                })

            result = agent.compact_context()
            usage = None
            try:
                usage = agent.get_context_usage()
            except Exception:
                pass

            return json.dumps({
                "status": "success",
                "ok": bool(result.get("ok")),
                "compacted_turns": result.get("compacted_turns", 0),
                "before": result.get("before", 0),
                "after": result.get("after", 0),
                "usage": usage,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Compact context error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class HistoryHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(session_id='', page='1', page_size='20', agent_id='')
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            agent_id = _request_agent_id(params)
            from agent.memory import get_conversation_store
            store = get_conversation_store(
                _get_workspace_root(agent_id=agent_id)
            )
            result = store.load_history_page(
                session_id=session_id,
                page=int(params.page),
                page_size=int(params.page_size),
            )
            # Same workspace-relative media rewrite the live SSE path applies,
            # so images/videos survive a page reload for non-default agents.
            history_root = None
            try:
                history_root = _get_workspace_root(session_id, agent_id)
            except Exception as e:
                logger.debug(f"[WebChannel] history workspace root skipped: {e}")
            for msg in result.get("messages") or []:
                if msg.get("role") != "assistant":
                    continue
                if history_root and isinstance(msg.get("content"), str):
                    try:
                        msg["content"] = _rewrite_relative_media(
                            msg["content"], history_root
                        )
                    except Exception as e:
                        logger.debug(f"[WebChannel] history media rewrite skipped: {e}")
                _add_subagent_displays(msg.get("steps"))
                _add_delegate_displays(msg.get("steps"))
                artifacts = _artifacts_from_steps(msg.get("steps"), session_id)
                if artifacts:
                    msg["artifacts"] = artifacts
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] History API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MessageDeleteHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            data = json.loads(web.data())
            agent_id = _request_agent_id(data)
            session_id = data.get('session_id', '').strip()
            user_seq = data.get('user_seq')
            delete_user = data.get('delete_user', True)
            cascade = data.get('cascade', False)
            
            if not session_id or user_seq is None:
                return json.dumps({"status": "error", "message": "session_id and user_seq required"})
            
            # 1. Delete from database
            from agent.memory import get_conversation_store
            store = get_conversation_store(_get_workspace_root(agent_id=agent_id))
            deleted = store.delete_message_pair(session_id, int(user_seq), delete_user=delete_user, cascade=cascade)

            # 2. Sync agent's in-memory context so its next turn sees the
            # same history as the DB. Handled by the agent_bridge helper.
            try:
                from bridge.bridge import Bridge
                Bridge().get_agent_bridge().sync_session_messages_from_store(
                    session_id, agent_id=agent_id
                )
            except Exception as sync_err:
                logger.warning(f"[WebChannel] Failed to sync agent memory: {sync_err}")

            return json.dumps({"status": "success", "deleted": deleted}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Message delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class LogsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')

        log_path = os.path.join(get_data_root(), "run.log")

        def generate():
            if not os.path.isfile(log_path):
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            # Read last 200 lines for initial display
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                tail_lines = lines[-200:]
                chunk = ''.join(tail_lines)
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode('utf-8')
            except Exception as e:
                yield f"data: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n".encode('utf-8')
                return

            # Tail new lines
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(0, 2)  # seek to end
                    deadline = time.time() + 600  # 10 min max
                    while time.time() < deadline:
                        line = f.readline()
                        if line:
                            payload = json.dumps({"type": "line", "content": line}, ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode('utf-8')
                        else:
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                return
            except Exception:
                return

        return generate()


class LogsDownloadHandler:
    """Serve the full run.log as a file download for offline troubleshooting.

    The /api/logs stream only replays the last 200 lines; this returns the whole
    file so users can attach it to a bug report.
    """

    def GET(self):
        _require_auth()
        log_path = os.path.join(get_data_root(), "run.log")
        if not os.path.isfile(log_path):
            raise web.notfound()

        try:
            with open(log_path, 'rb') as f:
                data = f.read()
        except Exception as e:
            logger.error(f"[WebChannel] Log download error: {e}")
            raise web.internalerror()

        # Timestamped name so multiple downloads don't overwrite each other.
        fname = f"cowagent-{time.strftime('%Y%m%d-%H%M%S')}.log"
        web.header('Content-Type', 'text/plain; charset=utf-8')
        web.header('Content-Disposition', f'attachment; filename="{fname}"')
        web.header('Content-Length', str(len(data)))
        web.header('Cache-Control', 'no-store')
        return data


class AssetsHandler:
    def GET(self, file_path):  # 修改默认参数
        try:
            # 如果请求是/static/，需要处理
            if file_path == '':
                # 返回目录列表...
                pass

            # 获取当前文件的绝对路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(current_dir, 'static')

            full_path = os.path.normpath(os.path.join(static_dir, file_path))

            # 安全检查：确保请求的文件在static目录内
            if not os.path.abspath(full_path).startswith(os.path.abspath(static_dir)):
                logger.error(f"Security check failed for path: {full_path}")
                raise web.notfound()

            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                # Browsers routinely probe optional asset variants (e.g. a
                # .ttf fallback declared alongside .woff2 in @font-face);
                # logging these as errors floods the console with harmless
                # noise. Keep it at debug level — real misconfigurations
                # will still surface via the network panel.
                logger.debug(f"Static file not found: {full_path}")
                raise web.notfound()

            # 设置正确的Content-Type
            content_type = mimetypes.guess_type(full_path)[0]
            if content_type:
                web.header('Content-Type', content_type)
            else:
                # 默认为二进制流
                web.header('Content-Type', 'application/octet-stream')

            # Without a validator a browser has nothing to cache on, so the
            # console re-downloaded every script, stylesheet, font and logo on
            # every reload. The ETag lets it ask instead, and a hit costs one
            # header rather than the file.
            info = os.stat(full_path)
            etag = '"%x-%x"' % (info.st_mtime_ns, info.st_size)
            web.header('ETag', etag)
            # ctx fields are read defensively: this handler is also driven
            # directly, outside a live request, where ctx is empty.
            if template.is_versioned(file_path) and 'v=' in web.ctx.get('query', ''):
                # render() stamps these with the file's own mtime, so the URL
                # cannot outlive the bytes it names: a changed file is a
                # changed URL. That is what makes it safe to promise the copy
                # never goes stale -- the promise is about this URL, not about
                # this path.
                web.header('Cache-Control', 'public, max-age=31536000, immutable')
            else:
                # Everything else (vendor bundles, fonts, logos) is served off
                # an unstamped URL, so it has to be revalidated. no-cache means
                # "keep it, but ask" -- not "do not keep it".
                web.header('Cache-Control', 'no-cache')
            if web.ctx.get('env', {}).get('HTTP_IF_NONE_MATCH') == etag:
                raise web.notmodified()

            # 读取并返回文件内容
            with open(full_path, 'rb') as f:
                return f.read()

        except web.HTTPError:
            # A 304 or the 404 above, both already handled; re-raise as-is so
            # web.py returns the original status to the client.
            raise
        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)
            raise web.notfound()


def _workspace_service(session_id: str = None, agent_id: str = None):
    from agent.workspace.service import WorkspaceService
    return WorkspaceService(_get_workspace_root(session_id, agent_id))


# System assets (memory / knowledge / persona files) always live in state_root,
# never in a project dir. When a session has a project open, a relative ref to
# one of these resolves against the project and misses; we fall back to the
# system directory so preview/@ still work.
_SYSTEM_ASSET_PREFIXES = ("memory/", "memory\\", "knowledge/", "knowledge\\")
_SYSTEM_ASSET_FILES = ("MEMORY.md", "AGENT.md", "USER.md", "RULE.md")


def _is_system_asset_rel(rel_path: str) -> bool:
    """True if a relative path points at a state_root-anchored system asset."""
    p = (rel_path or "").lstrip("./")
    return p in _SYSTEM_ASSET_FILES or p.startswith(_SYSTEM_ASSET_PREFIXES)


def _system_workspace_service():
    from agent.workspace.service import WorkspaceService
    from common.state_dir import state_root_str
    return WorkspaceService(state_root_str())


def _decorate_entry(svc, entry: dict) -> dict:
    """Attach the URLs the frontend needs to preview or download an entry."""
    if entry.get("is_dir"):
        return entry
    abs_path = entry.get("abs_path") or os.path.join(svc.root, entry["path"])
    entry["abs_path"] = abs_path
    entry["raw_url"] = f"/api/file?path={quote(abs_path)}"
    entry["preview_url"] = _build_preview_url(abs_path)
    return entry


class WorkspaceTreeHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(path='', show_hidden='', session='', agent='')
            svc = _workspace_service(params.session or None, params.agent or None)
            result = svc.list_dir(params.path, show_hidden=params.show_hidden == '1')
            result["entries"] = [_decorate_entry(svc, e) for e in result["entries"]]
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Workspace tree error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class WorkspaceSearchHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(q='', limit='30', session='', agent='')
            try:
                limit = max(1, min(100, int(params.limit)))
            except (TypeError, ValueError):
                limit = 30
            svc = _workspace_service(params.session or None, params.agent or None)
            result = svc.search(params.q, limit=limit)
            result["results"] = [_decorate_entry(svc, e) for e in result["results"]]
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Workspace search error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class WorkspaceResolveHandler:
    """
    Metadata + preview/raw URLs for one entry, given a relative or absolute path.

    Directories resolve as well (the client then browses instead of previewing),
    just without the file URLs.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.protocol.artifact import classify_kind, is_previewable
            params = web.input(path='', session='', agent='')
            raw_path = (params.path or '').strip()
            if not raw_path:
                return json.dumps({"status": "error", "message": "path is required"})

            svc = _workspace_service(params.session or None, params.agent or None)
            if os.path.isabs(os.path.expanduser(raw_path)):
                abs_path = os.path.realpath(os.path.expanduser(raw_path))
                if not _is_path_allowed(abs_path):
                    return json.dumps({"status": "error", "message": "Path not allowed"})
                is_dir = os.path.isdir(abs_path)
                if not is_dir and not os.path.isfile(abs_path):
                    return json.dumps({"status": "error", "message": "File not found"})
                kind = "directory" if is_dir else classify_kind(abs_path)
                entry = {
                    "name": os.path.basename(abs_path),
                    "path": svc.to_rel(abs_path),
                    "abs_path": abs_path,
                    "is_dir": is_dir,
                    "kind": kind,
                    "previewable": (not is_dir) and is_previewable(kind),
                    "size": 0 if is_dir else os.path.getsize(abs_path),
                    "mtime": os.path.getmtime(abs_path),
                }
            else:
                try:
                    entry = svc.stat_file(raw_path)
                except FileNotFoundError:
                    # Memory/knowledge live in state_root, not the project. Retry
                    # there so their cards still preview when a project is open.
                    if _is_system_asset_rel(raw_path):
                        entry = _system_workspace_service().stat_file(raw_path)
                    else:
                        raise

            # A directory has nothing to serve; the client browses into it.
            if not entry["is_dir"]:
                entry["raw_url"] = f"/api/file?path={quote(entry['abs_path'])}"
                entry["preview_url"] = _build_preview_url(entry["abs_path"])
            return json.dumps({"status": "success", "file": entry}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Workspace resolve error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class WorkspaceMetaHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(session='', agent='')
            svc = _workspace_service(params.session or None, params.agent or None)
            return json.dumps({"status": "success", **svc.meta()}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Workspace meta error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _editable_target(raw_path: str, session_id: str = None, agent_id: str = None):
    """
    Locate a file for the preview panel's text editor: (service, rel_path).

    Narrower than `/api/workspace/resolve`, which only has to serve bytes and so
    accepts anything under the configured serve roots. Reading and writing text
    stay inside the session's workspace (its project dir or the default state
    root), with a fallback to the state root for the memory / knowledge / persona
    assets that live there even while a project is open.
    """
    svc = _workspace_service(session_id, agent_id)
    system = _system_workspace_service()
    try:
        rel = svc.to_workspace_rel(raw_path)
    except ValueError:
        # Absolute path outside the session workspace: the state root is the
        # only other place the console is allowed to edit.
        return system, system.to_workspace_rel(raw_path)
    if svc.root != system.root and _is_system_asset_rel(rel) \
            and not os.path.isfile(svc.resolve(rel)):
        return system, rel
    return svc, rel


def _is_memory_rel(rel_path: str) -> bool:
    """True if a workspace-relative path points at a memory file backed by the
    vector index (so an edit has to be re-embedded, not just written)."""
    p = (rel_path or "").lstrip("./")
    return p == "MEMORY.md" or p.startswith(("memory/", "memory\\"))


def _mark_memory_dirty(agent_id: str = None) -> None:
    """Flag the agent's memory index stale after a console edit to a memory file.

    The index is built from the file contents, so a human edit here must be
    re-embedded the same way an agent's write/edit tool triggers it — otherwise
    semantic search keeps returning the pre-edit text until something else marks
    the store dirty. Best-effort: a failure here must not fail the save.
    """
    try:
        from bridge.bridge import Bridge
        agent = Bridge().get_agent_bridge().get_agent(agent_id=agent_id or None)
        mm = getattr(agent, "memory_manager", None)
        if mm:
            mm.mark_dirty()
    except Exception as e:
        logger.warning(f"[WebChannel] Failed to mark memory index dirty: {e}")


class WorkspaceReadHandler:
    """
    Text content of one workspace file, for the preview panel's editor.

    Returns the `mtime` the client passes back on save and an `editable` flag,
    so the editor never opens a file it would be unable to write back.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(path='', session='', agent='')
            raw_path = (params.path or '').strip()
            if not raw_path:
                return json.dumps({"status": "error", "message": "path is required"})
            svc, rel = _editable_target(raw_path, params.session or None, params.agent or None)
            return json.dumps({"status": "success", **svc.read_text(rel)}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Workspace read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class WorkspaceWriteHandler:
    """
    Save edited text back to a workspace file.

    A human editing a file in the console is not an agent tool call, so the
    session's agent permission mode does not apply here; the guard is the
    workspace boundary enforced by `_editable_target`.

    `expected_mtime` carries the timestamp the editor loaded. When it no longer
    matches, the response is `code: "conflict"` so the client can offer to
    reload or overwrite rather than silently discarding the newer content -
    which the agent may well have written mid-edit.
    """

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace.service import WorkspaceConflictError

            body = json.loads(web.data() or b'{}')
            raw_path = (body.get("path") or "").strip()
            if not raw_path:
                return json.dumps({"status": "error", "message": "path is required"})
            content = body.get("content")
            if not isinstance(content, str):
                return json.dumps({"status": "error", "message": "content must be a string"})

            agent_id = body.get("agent") or None
            svc, rel = _editable_target(raw_path, body.get("session") or None, agent_id)
            try:
                result = svc.write_text(rel, content, expected_mtime=body.get("expected_mtime"))
            except WorkspaceConflictError as e:
                return json.dumps({"status": "error", "code": "conflict", "message": str(e)})

            # A memory file feeds the vector index; re-embed it on edit so search
            # doesn't keep returning the stale pre-edit text.
            if _is_memory_rel(rel):
                _mark_memory_dirty(agent_id)

            logger.info(f"[WebChannel] Workspace file saved: {result['path']} ({result['size']} bytes)")
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except PermissionError:
            return json.dumps({"status": "error", "message": "permission denied"})
        except Exception as e:
            logger.error(f"[WebChannel] Workspace write error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _project_state(session_id: str, agent_id: str = None) -> dict:
    """Assemble the project picker state: current selection + recents + root."""
    from agent.workspace import project_store
    from common.state_dir import state_root_str

    current = project_store.get_project_dir(session_id, agent_id) if session_id else None
    # Resolve the default workspace against the Agent this session belongs to,
    # so the selector hint matches the file panel's real root in multi-Agent
    # setups instead of always pointing at the default Agent's workspace.
    from common.runtime_identity import RuntimeIdentity
    default_workspace = state_root_str(RuntimeIdentity(agent_id=agent_id))
    return {
        "current": (
            {"path": current, "name": os.path.basename(current) or current}
            if current else None
        ),
        "default_workspace": default_workspace,
        "projects_root": project_store.projects_root(),
        "recents": project_store.list_recents(),
    }


class ProjectsHandler:
    """List the project picker state for a session (current + recents)."""

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(session='', agent='')
            state = _project_state(params.session or None, params.agent or None)
            return json.dumps({"status": "success", **state}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Projects list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ProjectSelectHandler:
    """Bind a session to a project directory, or clear it (project_dir=null)."""

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace import project_store
            body = json.loads(web.data() or b"{}")
            session_id = (body.get("session") or body.get("session_id") or "").strip()
            agent_id = body.get("agent") or body.get("agent_id")
            if not session_id:
                return json.dumps({"status": "error", "message": "session is required"})
            project_dir = body.get("project_dir")
            applied = project_store.set_project_dir(
                session_id, project_dir or None, agent_id
            )
            # Retarget an already-instantiated session agent immediately, so the
            # change takes effect on the next message without a fresh get_agent.
            try:
                from bridge.bridge import Bridge
                ab = Bridge().get_agent_bridge()
                agent = ab.get_cached_agent(session_id, agent_id)
                if agent is not None and getattr(agent, "apply_project_dir", None):
                    agent.apply_project_dir(applied)
            except Exception as e:
                logger.debug(f"[WebChannel] project apply-to-agent skipped: {e}")
            state = _project_state(session_id, agent_id)
            return json.dumps({"status": "success", **state}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Project select error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ProjectCreateHandler:
    """Create a new project folder under the projects root and select it."""

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace import project_store
            body = json.loads(web.data() or b"{}")
            session_id = (body.get("session") or body.get("session_id") or "").strip()
            agent_id = body.get("agent") or body.get("agent_id")
            name = (body.get("name") or "").strip()
            if not name:
                return json.dumps({"status": "error", "message": "name is required"})
            path = project_store.create_project(name)
            if session_id:
                project_store.set_project_dir(session_id, path, agent_id)
                try:
                    from bridge.bridge import Bridge
                    ab = Bridge().get_agent_bridge()
                    agent = ab.get_cached_agent(session_id, agent_id)
                    if agent is not None and getattr(agent, "apply_project_dir", None):
                        agent.apply_project_dir(path)
                except Exception as e:
                    logger.debug(f"[WebChannel] project apply-to-agent skipped: {e}")
            state = _project_state(session_id or None, agent_id)
            return json.dumps({"status": "success", "path": path, **state}, ensure_ascii=False)
        except (ValueError, FileExistsError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Project create error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ProjectOrderHandler:
    """Persist the user's chosen sidebar order of project spaces."""

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace import project_store
            body = json.loads(web.data() or b"{}")
            order = body.get("order")
            if not isinstance(order, list):
                return json.dumps({"status": "error", "message": "order must be a list"})
            saved = project_store.set_order(order)
            return json.dumps({"status": "success", "order": saved}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Project order error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ProjectManageHandler:
    """Rename (PUT) or delete (DELETE) a project record.

    Neither touches the folder on disk: a rename only sets a display name, and a
    delete only forgets the CowAgent record and unbinds any sessions (they revert
    to the default workspace). The files stay exactly where they are.
    """

    def PUT(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace import project_store
            body = json.loads(web.data() or b"{}")
            path = (body.get("path") or "").strip()
            if not path:
                return json.dumps({"status": "error", "message": "path is required"})
            name = project_store.rename_project(path, body.get("name") or "")
            return json.dumps({"status": "success", "name": name}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Project rename error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def DELETE(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace import project_store
            body = json.loads(web.data() or b"{}")
            path = (body.get("path") or "").strip()
            agent_id = body.get("agent") or body.get("agent_id")
            if not path:
                return json.dumps({"status": "error", "message": "path is required"})
            unbound = project_store.delete_project(path, agent_id)
            return json.dumps({"status": "success", "unbound": unbound}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Project delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


# Virtual path (Windows only) that expands to the list of logical drives, so
# the picker can navigate above a drive root and switch between drives.
_DRIVES_SENTINEL = "__DRIVES__"


class ProjectBrowseHandler:
    """List sub-directories of a path, for the "open project" folder picker.

    Directories only (files are irrelevant when choosing a project root). The
    starting point defaults to the projects root; the parent is included so the
    user can navigate upward.
    """

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common.utils import expand_path
            params = web.input(path='')
            raw = (params.path or '').strip()

            # On Windows, "__DRIVES__" is a virtual path listing all logical
            # drives, so the user can hop across drives from a drive root.
            if sys.platform == 'win32' and raw == _DRIVES_SENTINEL:
                import ctypes

                drives = []
                buf = ctypes.create_unicode_buffer(1024)
                length = ctypes.windll.kernel32.GetLogicalDriveStringsW(1024, buf)
                for drive in buf[:length].split('\x00'):
                    if drive:
                        drives.append({"name": drive.rstrip("\\"), "path": drive})
                return json.dumps({
                    "status": "success",
                    "path": _DRIVES_SENTINEL,
                    "parent": None,
                    "dirs": drives,
                }, ensure_ascii=False)

            # Default entry point is the user's home (~), a familiar anchor for
            # picking a project directory.
            base = os.path.realpath(expand_path(raw)) if raw else os.path.realpath(os.path.expanduser("~"))
            if not os.path.isdir(base):
                base = os.path.realpath(os.path.expanduser("~"))

            dirs = []
            try:
                with os.scandir(base) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                dirs.append({
                                    "name": entry.name,
                                    "path": os.path.join(base, entry.name),
                                })
                        except OSError:
                            continue
            except PermissionError:
                return json.dumps({"status": "error", "message": "permission denied"})

            dirs.sort(key=lambda d: d["name"].lower())
            parent = os.path.dirname(base)

            # On Windows, at a drive root (e.g. C:\) dirname returns the same
            # path, so point parent at the drives list instead of dropping it.
            if sys.platform == 'win32':
                _, tail = os.path.splitdrive(base)
                if tail in (os.sep, os.altsep, ''):
                    parent = _DRIVES_SENTINEL

            return json.dumps({
                "status": "success",
                "path": base,
                "parent": parent if parent != base else None,
                "dirs": dirs,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Project browse error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeListHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            params = web.input(agent_id='')
            svc = KnowledgeService(
                _get_workspace_root(agent_id=_request_agent_id(params))
            )
            result = svc.list_tree()
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeReadHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from pathlib import Path
            from agent.knowledge.service import KnowledgeService
            params = web.input(path='', agent_id='')
            svc = KnowledgeService(
                _get_workspace_root(agent_id=_request_agent_id(params))
            )
            result = svc.read_file(params.path)
            # Absolute directory of the doc (posix separators), so clients can
            # resolve image srcs that are relative to the doc into /api/file
            # URLs. Additive field; read_file itself stays untouched.
            rel = str(result["path"]).replace("\\", "/")
            result["dir"] = Path(svc.knowledge_dir, *rel.split("/")).parent.as_posix()
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeGraphHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            params = web.input(agent_id='')
            svc = KnowledgeService(
                _get_workspace_root(agent_id=_request_agent_id(params))
            )
            return json.dumps(svc.build_graph(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge graph error: {e}")
            return json.dumps({"nodes": [], "links": []})


class KnowledgeActionHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = body.get("action", "")
            payload = body.get("payload") or {}
            from agent.knowledge.service import KnowledgeService
            result = KnowledgeService(
                _get_workspace_root(agent_id=_request_agent_id(body))
            ).dispatch(action, payload)
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge action error: {e}")
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})


class KnowledgeImportHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            content_length = int(getattr(web.ctx, "env", {}).get("CONTENT_LENGTH") or 0)
            if content_length > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                return json.dumps({
                    "status": "error",
                    "code": 413,
                    "message": "import batch too large",
                    "payload": None,
                })
            params = _raw_web_input()
            agent_id = _request_agent_id(params)
            target_category = params.get("target_category", "")
            conflict_strategy = params.get("conflict_strategy", "skip")
            uploaded = _ensure_list(params.get("files"))
            single = params.get("file")
            if single is not None:
                uploaded.append(single)
            if not uploaded:
                return json.dumps({"status": "error", "code": 400, "message": "No files uploaded", "payload": None})
            if len(uploaded) > KnowledgeService.MAX_IMPORT_FILES:
                return json.dumps({
                    "status": "error",
                    "code": 400,
                    "message": f"too many files: max {KnowledgeService.MAX_IMPORT_FILES}",
                    "payload": None,
                })

            files = []
            total_size = 0
            for file_obj in uploaded:
                if file_obj is None:
                    continue
                filename = getattr(file_obj, "filename", "") or getattr(file_obj, "name", "")
                content = _read_uploaded_file_bytes_limited(file_obj, KnowledgeService.MAX_IMPORT_FILE_SIZE)
                total_size += len(content)
                if total_size > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                    return json.dumps({
                        "status": "error",
                        "code": 413,
                        "message": "import batch too large",
                        "payload": None,
                    })
                files.append({
                    "filename": filename,
                    "content": content,
                })

            result = KnowledgeService(
                _get_workspace_root(agent_id=agent_id)
            ).dispatch("import_documents", {
                "target_category": target_category,
                "conflict_strategy": conflict_strategy,
                "files": files,
            })
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge import error: {e}", exc_info=True)
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})


class VersionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        from cli.update_service import version_payload
        # Local metadata only — never contacts GitHub.
        return json.dumps(version_payload(), ensure_ascii=False)


class UpdateCheckHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from cli.update_service import check_for_updates, version_payload
            payload = version_payload()
            logger.info("[WebChannel] update check requested (current v%s)", payload["version"])
            result = check_for_updates(payload["version"])
            result.update({
                "install_kind": payload["install_kind"],
                "update_supported": payload["update_supported"],
                "unsupported_reason": payload["unsupported_reason"],
            })
            if result.get("up_to_date"):
                logger.info("[WebChannel] update check: already up to date (v%s)", payload["version"])
            else:
                latest = (result.get("latest") or {}).get("tag") or "?"
                logger.info("[WebChannel] update check: newer version available -> %s", latest)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.error("[WebChannel] update check failed: %s", e, exc_info=True)
            return json.dumps({"status": "error", "message": str(e)})


class UpdateStartHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from cli.update_service import UpdateError, schedule_web_update
            logger.info("[WebChannel] one-click update requested")
            status = schedule_web_update()
            return json.dumps({"status": "success", "update": status}, ensure_ascii=False)
        except UpdateError as e:
            logger.error("[WebChannel] update could not start at step '%s': %s", e.step, e.message)
            return json.dumps({
                "status": "error",
                "step": e.step,
                "message": e.message,
                "output": e.output,
            })
        except Exception as e:
            logger.error("[WebChannel] update start failed: %s", e, exc_info=True)
            return json.dumps({"status": "error", "message": str(e)})


class UpdateStatusHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        from cli.update_service import read_update_status
        return json.dumps(read_update_status(), ensure_ascii=False)

URLS = (
    '/', 'ChatHandler',
    '/chat', 'RootHandler',
    '/api/health', 'HealthHandler',
    '/auth/login', 'AuthLoginHandler',
    '/auth/check', 'AuthCheckHandler',
    '/auth/logout', 'AuthLogoutHandler',
    '/message', 'MessageHandler',
    '/upload', 'UploadHandler',
    '/uploads/(.*)', 'UploadsHandler',
    '/api/file', 'FileServeHandler',
    '/preview/(.+)', 'PreviewHandler',
    '/api/workspace/tree', 'WorkspaceTreeHandler',
    '/api/workspace/search', 'WorkspaceSearchHandler',
    '/api/workspace/resolve', 'WorkspaceResolveHandler',
    '/api/workspace/meta', 'WorkspaceMetaHandler',
    '/api/workspace/read', 'WorkspaceReadHandler',
    '/api/workspace/write', 'WorkspaceWriteHandler',
    '/api/projects', 'ProjectsHandler',
    '/api/projects/select', 'ProjectSelectHandler',
    '/api/projects/create', 'ProjectCreateHandler',
    '/api/projects/browse', 'ProjectBrowseHandler',
    '/api/projects/order', 'ProjectOrderHandler',
    '/api/projects/manage', 'ProjectManageHandler',
    '/api/voice/asr', 'VoiceAsrHandler',
    '/api/voice/tts', 'VoiceTtsHandler',
    '/poll', 'PollHandler',
    '/stream', 'StreamHandler',
    '/cancel', 'CancelHandler',
    '/v1/chat/completions', 'OpenAIChatCompletionsHandler',
    '/config', 'ConfigHandler',
    '/api/models', 'ModelsHandler',
    '/api/channels', 'ChannelsHandler',
    '/api/weixin/qrlogin', 'WeixinQrHandler',
    '/api/feishu/register', 'FeishuRegisterHandler',
    '/api/tools', 'ToolsHandler',
    '/api/skills', 'SkillsHandler',
    '/api/skills/content', 'SkillContentHandler',
    '/api/memory', 'MemoryHandler',
    '/api/memory/content', 'MemoryContentHandler',
    '/api/knowledge/list', 'KnowledgeListHandler',
    '/api/knowledge/read', 'KnowledgeReadHandler',
    '/api/knowledge/graph', 'KnowledgeGraphHandler',
    '/api/knowledge/action', 'KnowledgeActionHandler',
    '/api/knowledge/import', 'KnowledgeImportHandler',
    '/api/scheduler', 'SchedulerHandler',
    '/api/scheduler/runs/detail', 'SchedulerRunDetailHandler',
    '/api/scheduler/runs/delete', 'SchedulerRunDeleteHandler',
    '/api/scheduler/runs', 'SchedulerRunsHandler',
    '/api/scheduler/run', 'SchedulerRunHandler',
    '/api/scheduler/toggle', 'SchedulerToggleHandler',
    '/api/scheduler/update', 'SchedulerUpdateHandler',
    '/api/scheduler/delete', 'SchedulerDeleteHandler',
    '/api/scheduler/create', 'SchedulerCreateHandler',
    '/api/scheduler/recipients', 'SchedulerRecipientsHandler',
    '/api/scheduler/instances', 'SchedulerInstancesHandler',
    '/api/agents', 'AgentsHandler',
    '/api/agents/([^/]+)/avatar', 'AgentAvatarHandler',
    '/api/agents/([^/]+)/files/([^/]+)', 'AgentCoreFileHandler',
    '/api/sessions', 'SessionsHandler',
    '/api/sessions/(.*)/generate_title', 'SessionTitleHandler',
    '/api/prompt/optimize', 'PromptOptimizeHandler',
    '/api/sessions/(.*)/clear_context', 'SessionClearContextHandler',
    '/api/sessions/(.*)/context_usage', 'SessionContextUsageHandler',
    '/api/sessions/(.*)/compact_context', 'SessionCompactContextHandler',
    '/api/sessions/(.*)/settings', 'SessionSettingsHandler',
    '/api/sessions/(.*)', 'SessionDetailHandler',
    '/api/history', 'HistoryHandler',
    '/api/messages/delete', 'MessageDeleteHandler',
    '/api/logs/download', 'LogsDownloadHandler',
    '/api/logs', 'LogsHandler',
    '/api/version', 'VersionHandler',
    '/api/update/check', 'UpdateCheckHandler',
    '/api/update/start', 'UpdateStartHandler',
    '/api/update/status', 'UpdateStatusHandler',
    '/mcp/oauth/callback', 'McpOAuthCallbackHandler',
    '/assets/(.*)', 'AssetsHandler',
    # Views inside the single-page console. Each serves the same shell;
    # the frontend router reads the path and opens the view it names,
    # so a reload or a shared link lands where it says. Last in the
    # table on purpose: web.py takes the first match, so no view name
    # can ever shadow an API route above -- which is also why the
    # settings view is /settings and not /config, a path the config
    # API already owns.
    '/(?:agents|settings|skills|memory|knowledge|channels|scheduler|logs)'
    '(?:/[a-z]+)?/?', 'ChatHandler',
)


def build_app():
    """The web.py application, built against this module's namespace.

    web.py resolves the handler names in URLS by looking them up in a
    namespace dict, so the application has to be built where those names are
    in scope. That is here -- this module imports every handler -- and it is
    why WebChannel, which only runs the server, asks for the app instead of
    assembling one.
    """
    return web.application(URLS, globals(), autoreload=False)
