"""The log record."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python

from .._json import extract_json

__all__ = ["LogEntry", "LogLevel"]

LogLevel = Literal["V", "D", "I", "W", "E", "F"]


class LogEntry(BaseModel):
    """One log line, split into its fields.

    Entries are immutable, and the constructor validates strictly: a field
    must be given as its own type (an ``int`` pid, a ``datetime`` timestamp);
    nothing is coerced. Entries are not hashable (``meta`` is a dict), as in
    2.x: ``hash()`` raises TypeError.

    The API of an entry is its fields plus :meth:`json_payload`,
    :meth:`to_dict` and :meth:`to_json`. That it is a pydantic model, and the
    ``model_*`` methods, are not guaranteed within 3.x (design 2.6). In
    particular pydantic's ``model_copy(update=...)`` and ``copy.replace()`` do
    not validate the fields they change.

    Attributes:
        timestamp: When the line was logged. Timezone-aware when the line
            carries its UTC offset (``-v zone``) or epoch seconds (``-v epoch``,
            in UTC); naive device-local time otherwise, such as yearless
            ``-v threadtime`` files. Lines without any timestamp (brief,
            process, tag, raw, and unrecognised lines) carry the naive host
            time at parsing. ``-v monotonic`` prints seconds since boot in the
            epoch column, so such a line gets that many seconds after the
            epoch: a 1970 timestamp that is not the moment it was logged.
        pid: Process id; 0 when the format has none.
        tid: Thread id; 0 when the format has none.
        uid: The uid from the ``-v uid`` column, a name mapped to its number
            (design 5.8); None when the column is absent, blank or a name that
            logpyt does not know.
        uid_raw: The ``-v uid`` column as printed (a number or a name); None
            when absent or blank.
        level: Priority: V, D, I, W, E or F.
        tag: The tag, without logcat's padding; empty when the format has none.
        message: The message. Only the separator after the tag is removed;
            leading whitespace (``"\\tat ..."``) and trailing spaces stay.
        raw: The line as read, without its terminator.
        package: The app package the line belongs to, set by package
            attribution; None when not attributed.
        parsed: False for the fallback entry of a line the parser did not
            recognise: its message and raw are the whole line.
        truncated: True when the line was longer than ``max_line_bytes``:
            ``raw`` and ``message`` come from its first ``max_line_bytes`` bytes.
        meta: Free-form data for the caller; empty unless the caller sets it.
            A dict passed in is stored as is, not copied.

    """

    model_config = ConfigDict(frozen=True, strict=True)

    # frozen=True would make pydantic hash every field, and meta (a dict)
    # cannot be hashed; say so to type checkers too.
    __hash__: None = None

    timestamp: datetime
    pid: int
    tid: int
    uid: int | None = None
    uid_raw: str | None = None
    level: LogLevel
    tag: str
    message: str
    raw: str
    package: str | None = None
    parsed: bool = True
    truncated: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)

    def json_payload(self) -> Any | None:  # noqa: ANN401  (arbitrary parsed JSON)
        """Return the first JSON object or array in the message.

        The search is bounded (see ``logpyt.utils.extract_json``) and is not
        cached: every call reads the current message.

        Returns:
            The parsed object or array, or None when the message holds none.

        """
        return extract_json(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict of Python values."""
        return self.model_dump()

    def to_json(self, *, indent: int | None = None, ensure_ascii: bool = False) -> str:
        """Return the fields as a JSON object.

        Args:
            indent: Indentation of the JSON text; None for one line.
            ensure_ascii: Escape non-ASCII characters as ``\\uXXXX`` when True.

        Returns:
            The JSON text. A ``meta`` value JSON cannot represent is written as
            its ``repr()``.

        """
        try:
            data = self.model_dump(mode="json", fallback=repr)
        except ValueError:  # e.g. bytes in meta that are not UTF-8
            data = self.model_dump(mode="json", exclude={"meta"})
            data["meta"] = _jsonable(self.meta)
        return json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)


def _jsonable(value: Any) -> Any:  # noqa: ANN401  (any meta value)
    """``value`` as JSON-ready data: what pydantic writes, else its ``repr()``.

    Containers are walked, so one bad value inside a nested dict or list costs
    only that value.
    """
    if isinstance(value, dict):
        return {
            key if isinstance(key, str) else repr(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    try:
        return to_jsonable_python(value, fallback=repr)
    except ValueError:
        return repr(value)
