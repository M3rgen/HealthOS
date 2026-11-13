from __future__ import annotations

import logging
import re
import sys
import sysconfig
import traceback
from pathlib import Path

from pythonjsonlogger.json import JsonFormatter

from app.core import sqlstate as driver_errors

SENSITIVE_NAMES = {"access_token", "refresh_token", "client_secret", "authorization", "healthos_ui"}
# Handlers configured by uvicorn before the app starts ("Exception in ASGI application").
SERVER_LOGGERS = ("uvicorn", "uvicorn.error")


TRACEBACK_HEADER = "Traceback (most recent call last)"
_EXCEPTION_LINE = re.compile(r"([A-Za-z_][\w.]*)(?::|$)")
# Credentials may contain punctuation; redact the rest of the line rather than
# treating commas or quotes as a safe token boundary.
_CREDENTIAL_FIELD = re.compile(
    r"(\b(?:" + "|".join(sorted(SENSITIVE_NAMES)) + r")\b[\"']?\s*[:=]\s*)[^\r\n]+",
    re.I,
)
_AUTH_SCHEME = re.compile(r"(\b(?:bearer|apikey)[ \t]+)[^\r\n]+", re.I)
# asyncio includes the task's repr in its message, independently of exc_info. Its
# exception repr can contain arbitrary health data, quotes or even literal newlines;
# discard everything after this label rather than trying to parse Python repr text.
_TASK_EXCEPTION = re.compile(r"(\bexception\s*=\s*)[\s\S]+")


def traceback_summary(message: str) -> str:
    """The text before a rendered traceback (first line) and the final exception's class."""
    head = message.split(TRACEBACK_HEADER, 1)[0].strip()
    lines = [line for line in message.splitlines() if line.strip()]
    match = _EXCEPTION_LINE.match(lines[-1]) if lines else None
    error_type = match.group(1) if match else "unknown"
    first = head.splitlines()[0] if head else "Error"
    return f"{first} (traceback omitted; error_type={error_type})"


class RedactSecrets(logging.Filter):
    """Redact credential fields and keep exception text out of every record.

    An exception's message, its traceback rendering and SQL parameters can quote raw health
    records (a database error's DETAIL, a bound ``raw_json``). A record that carries
    ``exc_info`` (``logger.exception``, a library logging a failed job or request) leaves with
    the value-free ``error_diagnostics`` fields instead. ``inline`` also appends them to the
    message, for plain-text handlers that do not render extra fields. A traceback already
    rendered into the message text is cut to its first line and the exception's class.
    Task exception representations embedded by asyncio are removed as well.
    """

    def __init__(self, inline: bool = False) -> None:
        super().__init__()
        self.inline = inline

    def filter(self, record: logging.LogRecord) -> bool:
        for name in record.__dict__:
            if name.casefold() in SENSITIVE_NAMES:
                setattr(record, name, "[REDACTED]")
        error = record.exc_info[1] if record.exc_info else None
        record.exc_info = None
        record.exc_text = None
        try:
            message = record.getMessage()
        except Exception:  # a broken format string is the handler's error to report, not ours
            message = str(record.msg)
        if TRACEBACK_HEADER in message:
            # A traceback rendered into the message itself (uvicorn logs Starlette's
            # ``format_exc()`` text of a failed lifespan this way): only its class leaves.
            record.msg = message = traceback_summary(message)
            record.args = None
        redacted = message
        for pattern in (_TASK_EXCEPTION, _CREDENTIAL_FIELD, _AUTH_SCHEME):
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        if redacted != message:
            record.msg, record.args = redacted, None
        if error is not None:
            fields = {"error_type": type(error).__name__, **error_diagnostics(error, frames=True)}
            for name, value in fields.items():
                if not hasattr(record, name):
                    setattr(record, name, value)
            if self.inline:
                details = "; ".join(f"{name}={value}" for name, value in fields.items())
                record.msg = f"{record.getMessage().rstrip()} ({details})"
                record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s %(event)s"))
    handler.addFilter(RedactSecrets())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn logs an unhandled request error with its traceback through its own handlers.
    for name in SERVER_LOGGERS:
        for server_handler in logging.getLogger(name).handlers:
            if not any(isinstance(item, RedactSecrets) for item in server_handler.filters):
                server_handler.addFilter(RedactSecrets(inline=True))
    # Transport logs include URLs, authorization headers and health/chat frames.
    # Pin the named WebSocket loggers too, even if they had an explicit DEBUG level.
    for name in ("httpx", "httpcore", "websockets", "websockets.client", "websockets.server"):
        logging.getLogger(name).setLevel(logging.WARNING)


MAX_CHAIN = 6
MAX_FRAMES = 12
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_STDLIB = Path(sysconfig.get_paths()["stdlib"]).resolve()


def error_diagnostics(exc: BaseException, *, frames: bool = False) -> dict[str, str]:
    """Value-free failure diagnostics for log records: class names, SQLSTATE and frames.

    Exception text, arguments and SQL parameters can carry raw health records, so only
    the cause/context chain's class names and the database's five-character code leave;
    with ``frames``, also where it was raised (``error_frames``).
    """
    names: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(names) < MAX_CHAIN:
        names.append(type(current).__name__)
        # MAX_CHAIN also bounds a cyclic chain.
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    result = {"error_chain": " < ".join(names)}
    if code := driver_errors.extract(exc):
        result["sqlstate"] = code
    if frames and (where := error_frames(exc)):
        result["error_frames"] = where
    return result


def error_frames(exc: BaseException) -> str:
    """Where ``exc`` passed, outermost first: ``module:function:line`` per frame.

    Only code locations from ``traceback.extract_tb``: never local variables, the source
    line or the exception's message. The application's own frames are kept, plus the
    innermost one (where a library raised it); ``...`` marks the frames left out between.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    keep = [index for index, item in enumerate(frames) if _own(item.filename)]
    if frames and (not keep or keep[-1] != len(frames) - 1):
        keep.append(len(frames) - 1)
    keep = keep[-MAX_FRAMES:]
    parts: list[str] = []
    for position, index in enumerate(keep):
        if index and (position == 0 or keep[position - 1] != index - 1):
            parts.append("...")
        item = frames[index]
        parts.append(f"{_module(item.filename)}:{item.name}:{item.lineno}")
    return " > ".join(parts)


def _own(filename: str) -> bool:
    path = _resolved(filename)
    return path.is_relative_to(_SOURCE_ROOT) and "site-packages" not in path.parts


def _resolved(filename: str) -> Path:
    # Through symbolic links (/tmp on macOS), as _SOURCE_ROOT is; "<frozen ...>" stays as is.
    path = Path(filename)
    return path.resolve() if path.is_absolute() else path


def _module(filename: str) -> str:
    """A dotted module name without the installation path (no user or host names)."""
    path = _resolved(filename)
    if "site-packages" in path.parts:
        parts = path.parts[path.parts.index("site-packages") + 1 :]
    elif _own(filename):
        parts = path.relative_to(_SOURCE_ROOT).parts
    elif path.is_relative_to(_STDLIB):
        parts = path.relative_to(_STDLIB).parts
    else:
        parts = (path.name,)
    return ".".join(parts).removesuffix(".py").removesuffix(".__init__")


def error_text(exc: BaseException) -> str:
    """A value-free one-line failure summary for status rows: class chain and SQLSTATE."""
    diagnostics = error_diagnostics(exc)
    sqlstate = diagnostics.get("sqlstate")
    return diagnostics["error_chain"] + (f" (SQLSTATE {sqlstate})" if sqlstate else "")


def event(logger: logging.Logger, name: str, **fields: object) -> None:
    logger.info(name, extra={"event": name, **fields})
