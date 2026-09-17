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
from urllib.parse import quote

import web

from bridge.context import *
from common import i18n
from common.log import logger
from config import (
    conf,
    get_data_root,
)
# Handlers that have moved to api/. Not used here, but build_app() resolves
# the URL table against this module's globals, so every handler name has to
# be in scope here -- see build_app at the bottom of the file.
from channel.web.api.agents import (  # noqa: F401
    AgentAvatarHandler, AgentCoreFileHandler, AgentsHandler,
)
from channel.web.api.channels import (  # noqa: F401
    ChannelsHandler, FeishuRegisterHandler, WeixinQrHandler,
)
from channel.web.api.config import ConfigHandler  # noqa: F401
from channel.web.api.models import ModelsHandler  # noqa: F401
from channel.web.api.openai_compat import OpenAIChatCompletionsHandler
from channel.web.api.sessions import (  # noqa: F401
    HistoryHandler, MessageDeleteHandler, PromptOptimizeHandler,
    SessionClearContextHandler, SessionCompactContextHandler,
    SessionContextUsageHandler, SessionDetailHandler, SessionSettingsHandler,
    SessionTitleHandler, SessionsHandler,
)
from channel.web.api.scheduler import (  # noqa: F401
    SchedulerCreateHandler, SchedulerDeleteHandler, SchedulerHandler,
    SchedulerInstancesHandler, SchedulerRecipientsHandler,
    SchedulerRunDeleteHandler, SchedulerRunDetailHandler, SchedulerRunHandler,
    SchedulerRunsHandler, SchedulerToggleHandler, SchedulerUpdateHandler,
)
# Shared with WebChannel, so it lives in _common. Imported by name rather
# than as a module: the handlers below read these out of this module's
# globals, the same place web.py resolves the handler names themselves
# from, and it keeps the names patchable where the tests already patch them.
from channel.web.core._common import (
    _build_preview_url, _check_auth,
    _ensure_list, _get_preview_secret, _get_upload_dir, _get_web_password,
    _get_workspace_root, _is_password_enabled, _raw_web_input,
    _request_agent_id,
    _require_auth,
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
