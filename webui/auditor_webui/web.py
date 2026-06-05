"""Flask application factory and HTTP routes."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import cast

from flask import Flask, Response, jsonify, request
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

from .codex_runner import is_session_active, start_agent_run, stop_agent_run
from .config import CONFIG
from .database import (
    create_session,
    delete_session,
    get_existing_session,
    get_settings,
    init_db,
    list_messages,
    list_sessions,
    set_setting,
    update_session,
)
from .schema import BUSY_STATUSES, JsonObject, JsonValue, row_str
from .vulnerabilities import read_vulnerabilities, unavailable_payload


def json_response(payload: Mapping[str, object], status: int = 200) -> tuple[Response, int]:
    return jsonify(dict(payload)), status


def request_json() -> JsonObject:
    payload = cast(object, request.get_json(silent=True))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    result: JsonObject = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ValueError("请求体 JSON 对象的 key 必须是字符串")
        result[key] = cast(JsonValue, value)
    return result


def current_theme() -> str:
    try:
        from .database import connect_db

        with connect_db() as conn:
            settings = get_settings(conn)
    except sqlite3.Error:
        return "light"
    return "dark" if settings.get("theme") == "dark" else "light"


def render_index() -> Response:
    target = CONFIG.static_dir / "index.html"
    html = target.read_text(encoding="utf-8")
    theme = current_theme()
    body_class = ' class="dark"' if theme == "dark" else ""
    html = html.replace("<body>", f'<body{body_class} data-theme="{theme}">', 1)
    return Response(html, mimetype="text/html")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(CONFIG.static_dir), static_url_path="/static")

    @app.after_request
    def add_no_cache(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def handle_value_error(exc: ValueError) -> ResponseReturnValue:
        return json_response({"ok": False, "error": str(exc)}, 400)

    @app.errorhandler(KeyError)
    def handle_key_error(exc: KeyError) -> ResponseReturnValue:
        message = str(exc.args[0]) if exc.args else str(exc)
        return json_response({"ok": False, "error": message}, 404)

    @app.errorhandler(404)
    def handle_not_found(_: Exception) -> ResponseReturnValue:
        return json_response({"ok": False, "error": "未知 API"}, 404)

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception) -> ResponseReturnValue:
        if isinstance(exc, HTTPException):
            return json_response({"ok": False, "error": exc.description}, exc.code or 500)
        return json_response({"ok": False, "error": str(exc)}, 500)

    @app.get("/")
    def index() -> Response:
        return render_index()

    @app.get("/api/state")
    def api_state() -> ResponseReturnValue:
        from .database import connect_db

        with connect_db() as conn:
            settings = get_settings(conn)
        return json_response({"ok": True, "settings": settings, "sessions": list_sessions()})

    @app.get("/api/sessions")
    def api_sessions() -> ResponseReturnValue:
        return json_response({"ok": True, "sessions": list_sessions()})

    @app.post("/api/sessions")
    def api_create_session() -> ResponseReturnValue:
        return json_response({"ok": True, "session": create_session(request_json())}, 201)

    @app.patch("/api/settings")
    def api_update_settings() -> ResponseReturnValue:
        payload = request_json()
        theme = payload.get("theme")
        if theme is not None:
            if theme not in {"light", "dark"}:
                raise ValueError("theme 只能是 light 或 dark")
            set_setting("theme", str(theme))
        selected = payload.get("selected_session_id")
        if selected is not None:
            set_setting("selected_session_id", str(selected))
        return json_response({"ok": True})

    @app.get("/api/sessions/<int:session_id>")
    def api_get_session(session_id: int) -> ResponseReturnValue:
        session = get_existing_session(session_id)
        return json_response({"ok": True, "session": session})

    @app.patch("/api/sessions/<int:session_id>")
    def api_update_session(session_id: int) -> ResponseReturnValue:
        return json_response({"ok": True, "session": update_session(session_id, request_json())})

    @app.delete("/api/sessions/<int:session_id>")
    def api_delete_session(session_id: int) -> ResponseReturnValue:
        get_existing_session(session_id)
        if is_session_active(session_id):
            raise ValueError("会话正在运行，不能删除")
        delete_session(session_id)
        return json_response({"ok": True})

    @app.get("/api/sessions/<int:session_id>/messages")
    def api_messages(session_id: int) -> ResponseReturnValue:
        get_existing_session(session_id)
        after = int(request.args.get("after", "0"))
        return json_response({"ok": True, "messages": list_messages(session_id, after_id=after)})

    @app.post("/api/sessions/<int:session_id>/messages")
    def api_send_message(session_id: int) -> ResponseReturnValue:
        session = get_existing_session(session_id)
        if row_str(session, "status") in BUSY_STATUSES:
            raise ValueError("当前会话已有运行中的任务")
        payload = request_json()
        content = str(payload.get("content", "")).strip()
        if not content:
            raise ValueError("消息不能为空")
        from .database import add_message

        add_message(session_id, "user", content)
        started = start_agent_run(session_id, content, source="user")
        if not started:
            raise ValueError("当前会话已有运行中的 Codex 任务")
        return json_response({"ok": True})

    @app.post("/api/sessions/<int:session_id>/stop")
    def api_stop(session_id: int) -> ResponseReturnValue:
        get_existing_session(session_id)
        return json_response({"ok": True, "stopped": stop_agent_run(session_id)})

    @app.get("/api/sessions/<int:session_id>/vulnerabilities")
    def api_vulnerabilities(session_id: int) -> ResponseReturnValue:
        session = get_existing_session(session_id)
        try:
            return json_response(read_vulnerabilities(session))
        except Exception as exc:
            return json_response(unavailable_payload(session, str(exc)))

    return app


def create_wsgi_app() -> Flask:
    init_db()
    return create_app()


def main() -> None:
    print(f"codex-auditor webui listening on http://{CONFIG.host}:{CONFIG.port}/", flush=True)
    app = create_wsgi_app()
    app.run(host=CONFIG.host, port=CONFIG.port, threaded=True, use_reloader=False)
