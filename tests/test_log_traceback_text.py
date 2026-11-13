"""A traceback rendered into a log message's text leaves without its values (R1 D3).

uvicorn logs a failed lifespan as Starlette's ``traceback.format_exc()`` text, without
``exc_info``: the exception's message (a database DETAIL, a bound value) would otherwise pass
the filter that only cleans records carrying ``exc_info``.
"""

import asyncio
import io
import json
import logging
import traceback
from contextlib import asynccontextmanager

import pytest

from app.core.logging import RedactSecrets, configure_logging, traceback_summary

MARKER = "synthetic-health-value-7f3a"


def rendered_traceback():
    try:
        raise ValueError(f"DETAIL: Key (raw_json)=({MARKER}) already exists.")
    except ValueError:
        return traceback.format_exc()


def filtered(message, *args, inline=False):
    record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, message, args, None)
    assert RedactSecrets(inline=inline).filter(record)
    return record.getMessage()


@pytest.mark.parametrize("inline", [False, True])
def test_rendered_traceback_is_cut_to_its_exception_class(inline):
    text = rendered_traceback()
    assert MARKER in text
    for message, args in ((text, ()), ("Exception in ASGI application\n%s", (text,))):
        result = filtered(message, *args, inline=inline)
        assert MARKER not in result and "raw_json" not in result
        assert "traceback omitted; error_type=ValueError" in result
    assert filtered("Exception in ASGI application\n%s", text).startswith(
        "Exception in ASGI application (traceback omitted"
    )


def test_messages_without_a_traceback_are_unchanged():
    assert filtered("Waiting for application startup.") == "Waiting for application startup."
    assert filtered("%s rows", 3) == "3 rows"


@pytest.mark.parametrize("inline", [False, True])
def test_task_exception_repr_without_exc_info_is_redacted(inline):
    message = "Task exception was never retrieved\nfuture: <Task exception=%s>"
    result = filtered(message, f"ValueError('{MARKER}')\n{MARKER}", inline=inline)
    assert MARKER not in result
    assert result.startswith("Task exception was never retrieved\nfuture: <Task exception=")
    assert "[REDACTED]" in result


async def test_asyncio_default_handler_keeps_task_exception_text_out_of_both_log_formats(capsys):
    async def failing():
        raise ValueError(MARKER)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactSecrets(inline=True))
    logger = logging.getLogger("asyncio")
    configure_logging()
    logger.addHandler(handler)
    task = asyncio.create_task(failing())
    await asyncio.wait({task})
    try:
        asyncio.get_running_loop().default_exception_handler(
            {
                "message": "Task exception was never retrieved",
                "exception": task.exception(),
                "future": task,
            }
        )
    finally:
        logger.removeHandler(handler)
    output = capsys.readouterr().out
    assert MARKER not in output + stream.getvalue()
    assert "Task exception was never retrieved" in stream.getvalue()
    assert "error_type=ValueError" in stream.getvalue()
    record = json.loads(output)
    assert record["error_type"] == record["error_chain"] == "ValueError"
    assert "test_log_traceback_text:failing:" in record["error_frames"]
    assert record["message"].endswith("exception=[REDACTED]")


def test_traceback_summary_keeps_only_code_identifiers():
    assert traceback_summary("Traceback (most recent call last):\n  x\nsqlalchemy.exc.X: v") == (
        "Error (traceback omitted; error_type=sqlalchemy.exc.X)"
    )
    assert traceback_summary("Traceback (most recent call last):\n  File 'a'") == (
        "Error (traceback omitted; error_type=unknown)"
    )


async def test_failed_lifespan_under_uvicorn_logs_no_exception_text():
    """The real path: Starlette's lifespan failure message through uvicorn's LifespanOn."""
    import uvicorn
    from starlette.applications import Starlette
    from uvicorn.lifespan.on import LifespanOn

    @asynccontextmanager
    async def lifespan(_app):
        raise RuntimeError(f"startup query failed: {MARKER}")
        yield

    config = uvicorn.Config(Starlette(lifespan=lifespan), lifespan="on", log_config=None)
    config.load()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactSecrets(inline=True))
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        await LifespanOn(config).startup()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    output = stream.getvalue()
    assert "Application startup failed" in output
    assert "traceback omitted; error_type=RuntimeError" in output
    assert MARKER not in output and "Traceback" not in output
