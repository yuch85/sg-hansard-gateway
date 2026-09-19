"""Structured JSON logging with capability-token redaction (D-09/D-10).

Every authenticated request emits exactly one sanitized JSON line carrying
``token_label`` (NEVER the token) plus the spec §26 field set. A redaction
filter scrubs any value matching the capability-token shape
(``hg_[A-Za-z0-9_-]{10,}``) from every logged field — defense in depth so an
accidental ``logging.exception`` traceback cannot leak the token. Uvicorn
access logging is off by default (D-10, release-blocking): the token lives in
the URL path, so a default access log would record it.
"""

from __future__ import annotations

from urllib.parse import urlsplit
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from hansard_gateway.config import Settings, settings as _settings

#: Shape of a capability token — anything matching is scrubbed from logs (T-27-15).
_TOKEN_SHAPE_RE = re.compile(r"hg_[A-Za-z0-9_-]{10,}")

#: Default log file location (overridable per test via `log_file`; production
#: path comes from Settings.log_file / HANSARD_LOG_FILE — 2026-09-18 fix: the
#: uvicorn-CLI entrypoint never called configure_logging, so the file handler
#: was never attached to the live service).
_DEFAULT_LOG_FILE = Path("logs/hansard-gateway.log")

#: Root logger name the gateway's structured handler attaches to.
_GATEWAY_LOGGER = "hansard_gateway"

#: Uvicorn access-log logger (disabled by default, D-10).
_UVICORN_ACCESS_LOGGER = "uvicorn.access"

#: Class label for private-range (LAN) peers in the diagnosis log line
#: (27.2-REQ-01 hygiene: no live LAN IP literal in tracked files).
_LAN_CLASS_LABEL = "lan"

#: Field order for the structured request line (spec §26).
_LOG_FIELDS: tuple[str, ...] = (
    "timestamp",
    "request_id",
    "token_label",
    "route",
    "report_id",
    "query",
    "status",
    "duration_ms",
    "upstream_status",
    "cache_hit",
    "parser_version",
    # spec 7.5 field set (Phase 27.1 wave 4):
    "prefix_or_query",
    "own_referer",
    "response_size",
    # 2026-09-18 search-hop diagnosis fields (mission 005):
    "remote_addr_class",
    "user_agent",
    "content_encoding",
    "accept_encoding",
    "reached_app_code",
)


def redact(value: Any) -> Any:
    """Recursively scrub token-shaped strings from any logged value."""
    if isinstance(value, str):
        return _TOKEN_SHAPE_RE.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class _RedactingFilter(logging.Filter):
    """Logging filter that redacts token-shaped strings in every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if record.args:
            record.args = redact(record.args)
        if record.exc_info:
            record.exc_text = redact(str(record.exc_info[1]))
        return True


class _StructuredFormatter(logging.Formatter):
    """Formats a LogRecord as one compact JSON line (spec §26 fields)."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # Foreign records (crawl's %-style logging) must not crash the
            # formatter when they flow through the gateway logger (2026-09-18).
            message = str(record.msg)
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        for field in _LOG_FIELDS:
            if field == "timestamp":
                continue
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if value is not None:
                    payload[key] = value
        return json.dumps(redact(payload), default=str)


class RequestLoggerMiddleware:
    """ASGI middleware: one sanitized JSON line per authenticated request.

    Sets the request-scoped fields (request_id, token_label, route, status,
    duration_ms) on a log record and emits it through the gateway logger. The
    token is read from the path ONLY to extract its label via the auth store;
    the raw token never enters the log.
    """

    def __init__(self, app: Any, *, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        import time
        import uuid

        start = time.monotonic()
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope.get("path", "")
        route = self._route_name(path)
        token_label: Optional[str] = None
        if path.startswith("/a/"):
            token_label = self._label_for_path(path)

        status_code = 200
        body_chunks: list[bytes] = []

        async def _send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status", 200)
            elif message.get("type") == "http.response.body":
                body_chunks.append(message.get("body", b""))
            await send(message)

        await self._app(scope, receive, _send)
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        response_size = sum(len(c) for c in body_chunks)

        logger = logging.getLogger(_GATEWAY_LOGGER)
        record = logger.makeRecord(
            _GATEWAY_LOGGER, logging.INFO, __file__, 0, "request", (), None
        )
        record.request_id = request_id  # type: ignore[attr-defined]
        record.token_label = token_label  # type: ignore[attr-defined]
        record.route = route  # type: ignore[attr-defined]
        record.status = status_code  # type: ignore[attr-defined]
        record.duration_ms = duration_ms  # type: ignore[attr-defined]
        record.parser_version = self._settings.parser_version  # type: ignore[attr-defined]
        record.query = self._query_from_scope(scope)  # type: ignore[attr-defined]
        # Handlers stashed diagnostics on scope.state (report: upstream_status;
        # search: cache_hit). Absent state = public route or unhandled request.
        state = scope.get("state") or {}
        record.cache_hit = state.get("cache_hit")  # type: ignore[attr-defined]
        record.report_id = self._report_id_from_path(path)  # type: ignore[attr-defined]
        record.upstream_status = state.get("upstream_status")  # type: ignore[attr-defined]
        # spec 7.5 field set: prefix_or_query (sanitised — never the token),
        # own_referer (bool), response_size (int bytes).
        record.prefix_or_query = self._prefix_or_query(scope)  # type: ignore[attr-defined]
        record.own_referer = self._own_referer(scope)  # type: ignore[attr-defined]
        record.response_size = response_size  # type: ignore[attr-defined]
        # Search-hop diagnosis fields (mission 005, 2026-09-18): whether the
        # request arrived at origin, from what class of address, with what UA
        # and encoding negotiation, and that it reached app code (a log line
        # existing at all IS the arrival proof — Caddy sits upstream of this).
        record.remote_addr_class = self._remote_addr_class(scope)  # type: ignore[attr-defined]
        record.user_agent = self._user_agent(scope)  # type: ignore[attr-defined]
        record.content_encoding = self._content_encoding(scope)  # type: ignore[attr-defined]
        record.accept_encoding = self._accept_encoding(scope)  # type: ignore[attr-defined]
        record.reached_app_code = True  # type: ignore[attr-defined]
        logger.handle(record)

    @staticmethod
    def _route_name(path: str) -> str:
        """Map a request path to a stable route name for logs."""
        if path.startswith("/a/") and "/report/" in path:
            return "report"
        # /search may carry a query string (/search?q=…) — match the path
        # segment, not the raw tail (2026-09-18 diagnosis).
        if path.startswith("/a/") and re.search(r"/search(\?|$)", path):
            return "search"
        if path.startswith("/a/") and "/date/" in path:
            return "date"
        if path.startswith("/a/"):
            return "auth_home"
        return "public"

    @staticmethod
    def _query_from_scope(scope: dict[str, Any]) -> Optional[str]:
        """Extract the `q` query param (or None) for the log line."""
        qs = scope.get("query_string", b"")
        if not qs:
            return None
        from urllib.parse import parse_qs

        params = parse_qs(qs.decode("utf-8"))
        values = params.get("q")
        return values[0] if values else None

    def _prefix_or_query(self, scope: dict[str, Any]) -> Optional[str]:
        """The spec §7.5 'prefix or query' field: the ladder prefix when the
        path is a /nav/{prefix} route, else the raw query string. The token
        is NEVER part of this value (the path is not logged; only the
        prefix segment or the query string, neither of which contains it)."""
        path = scope.get("path", "")
        marker = "/nav/"
        idx = path.find(marker)
        if idx != -1:
            prefix = path[idx + len(marker):]
            if prefix:
                return prefix
        qs = scope.get("query_string", b"")
        return qs.decode("utf-8") if qs else None

    def _own_referer(self, scope: dict[str, Any]) -> bool:
        """True when the Referer header's host is our own public base
        (spec §7.5: 'whether the referring page was one of ours')."""
        headers = dict(scope.get("headers") or [])
        referer = headers.get(b"referer")
        if not referer:
            return False
        try:
            host = urlsplit(referer.decode("utf-8")).netloc
        except (UnicodeDecodeError, ValueError):
            return False
        own_host = urlsplit(self._settings.public_base_url).netloc
        return host == own_host

    @staticmethod
    def _report_id_from_path(path: str) -> Optional[str]:
        """Extract the report id segment from a /report/ path (if any)."""
        marker = "/report/"
        idx = path.find(marker)
        if idx == -1:
            return None
        return path[idx + len(marker):]

    @staticmethod
    def _header_value(scope: dict[str, Any], name: bytes) -> Optional[str]:
        """First value of a request header, UTF-8 decoded (None if absent)."""
        for key, value in scope.get("headers") or []:
            if key == name:
                try:
                    return value.decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 — header decode must not raise
                    return None
        return None

    @classmethod
    def _remote_addr_class(cls, scope: dict[str, Any]) -> str:
        """Classify the arrival address for the diagnosis log line.

        Caddy (RPi) fronts the app and sets X-Real-IP to the external client,
        so that header is the discriminator between origin-arriving ChatGPT
        fetches (their IP) and LAN probes (private-range / 127.0.0.1)."""
        xri = cls._header_value(scope, b"x-real-ip")
        if xri:
            if xri.startswith("192.168."):
                return f"{_LAN_CLASS_LABEL}:{xri}"
            return f"public:{xri}"
        conn = scope.get("client")
        if conn and conn[0].startswith("192.168."):
            return f"{_LAN_CLASS_LABEL}:{conn[0]}"
        return str(conn)

    @classmethod
    def _user_agent(cls, scope: dict[str, Any]) -> Optional[str]:
        """The request User-Agent, truncated for the log line."""
        ua = cls._header_value(scope, b"user-agent")
        return ua[:200] if ua else None

    @classmethod
    def _content_encoding(cls, scope: dict[str, Any]) -> Optional[str]:
        """Content-Encoding of the request (expected absent for GET)."""
        return cls._header_value(scope, b"content-encoding")

    @classmethod
    def _accept_encoding(cls, scope: dict[str, Any]) -> Optional[str]:
        """The client's Accept-Encoding negotiation string (truncated)."""
        ae = cls._header_value(scope, b"accept-encoding")
        return ae[:200] if ae else None

    @staticmethod
    def _label_for_path(path: str) -> Optional[str]:
        """Resolve the token label from the path (never logs the token)."""
        # Defer to the auth module so we never import cycles at module load.
        try:
            from hansard_gateway.auth import get_token_store

            segments = path.split("/")
            # path shape: /a/{token}/...
            if len(segments) < 3:
                return None
            token = segments[2]
            import hashlib

            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            store = get_token_store()
            for entry in store.enabled_entries():
                if entry.sha256 == digest:
                    return entry.label
        except Exception:  # noqa: BLE001 — label is best-effort, never fatal
            return None
        return None


def configure_logging(
    *, log_file: Optional[Path] = None, settings: Optional[Settings] = None
) -> None:
    """Install the structured JSON handler + redaction filter on the gateway.

    Idempotent: re-calling replaces the gateway handler but keeps any other
    handlers on the root logger. Uvicorn access logging is disabled (D-10).
    """
    cfg = settings if settings is not None else _settings
    log_path = (
        Path(log_file)
        if log_file is not None
        else Path(cfg.log_file) if cfg.log_file
        else _DEFAULT_LOG_FILE
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = _StructuredFormatter()
    redactor = _RedactingFilter()

    # Stream handler: production only (HANSARD_LOG_STREAM=1, set by the
    # systemd unit so journald/StandardOutput captures the JSON lines).
    # Tests stay silent — a handler on the package logger at import time
    # breaks pytest's capture + the redaction tests' file assertions.
    stream_on = os.environ.get("HANSARD_LOG_STREAM", "") == "1"
    handlers: list[logging.Handler] = []
    if stream_on:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        handlers.append(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    handlers.append(file_handler)

    gateway_logger = logging.getLogger(_GATEWAY_LOGGER)
    for existing in list(gateway_logger.handlers):
        gateway_logger.removeHandler(existing)
    for handler in handlers:
        gateway_logger.addHandler(handler)
    gateway_logger.setLevel(logging.INFO)
    gateway_logger.propagate = False

    # Defense in depth: redact on the root logger too so any stray
    # logging.exception in a dependency cannot leak the token.
    root = logging.getLogger()
    if not any(
        isinstance(h, logging.Handler) and h is file_handler for h in root.handlers
    ):
        root_redactor = _RedactingFilter()
        root_redactor.name = "gateway-root-redactor"
        root.addFilter(root_redactor)

    # D-10 (release-blocking): uvicorn access log OFF by default. The token is
    # in the URL path; a default access log would record it verbatim.
    access_logger = logging.getLogger(_UVICORN_ACCESS_LOGGER)
    access_logger.disabled = True
    access_logger.addHandler(logging.NullHandler())


def run(*, host: str = "0.0.0.0", port: Optional[int] = None) -> None:
    """Uvicorn entrypoint with access_log=False (D-10, release-blocking).

    The systemd unit (Plan 05) passes --no-access-log as well, but a bare
    ``uvicorn main:app`` must never write token-bearing paths to the access
    log either — hence the code-level default.
    """
    import uvicorn

    from hansard_gateway.main import create_app

    configure_logging()
    uvicorn.run(
        create_app(),
        host=host,
        port=port if port is not None else _settings.app_port,
        access_log=False,  # D-10: release-blocking
    )
