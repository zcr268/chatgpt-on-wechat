import base64
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import random
import re
import time

import web

from bridge.context import *
from common import i18n
from common.log import logger
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
from channel.web.api.knowledge import (  # noqa: F401
    KnowledgeActionHandler, KnowledgeGraphHandler, KnowledgeImportHandler,
    KnowledgeListHandler, KnowledgeReadHandler,
)
from channel.web.api.logs import LogsDownloadHandler, LogsHandler  # noqa: F401
from channel.web.api.memory import (  # noqa: F401
    MemoryContentHandler, MemoryHandler,
)
from channel.web.api.skills import (  # noqa: F401
    SkillContentHandler, SkillsHandler, ToolsHandler,
)
from channel.web.api.update import (  # noqa: F401
    UpdateCheckHandler, UpdateStartHandler, UpdateStatusHandler, VersionHandler,
)
from channel.web.api.workspace import (  # noqa: F401
    ProjectBrowseHandler, ProjectCreateHandler, ProjectManageHandler,
    ProjectOrderHandler, ProjectSelectHandler, ProjectsHandler,
    WorkspaceMetaHandler, WorkspaceReadHandler, WorkspaceResolveHandler,
    WorkspaceSearchHandler, WorkspaceTreeHandler, WorkspaceWriteHandler,
)
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
    _check_auth, _is_path_allowed,
    _get_preview_secret, _get_upload_dir, _get_web_password,
    _is_password_enabled, _raw_web_input,
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


# System assets (memory / knowledge / persona files) always live in state_root,
# never in a project dir. When a session has a project open, a relative ref to


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
