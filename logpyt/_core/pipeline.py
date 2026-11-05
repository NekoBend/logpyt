"""The line pipeline: framed lines in, entries or groups out (design 5.2, 5.5, 2.8).

One `Pipeline` serves one parse session. For each `FramedLine` it runs, in order:

1. decode: UTF-8, an undecodable byte becomes U+FFFD (`decode_line`); a cut
   line drops the start of the character the cap cut;
2. dividers: logcat's ``--------- beginning of <buffer>`` lines are not entries
   and are not counted as unparsed;
3. parse: ``parser.parse(text, ctx)``, or the fallback entry with
   ``parsed=False`` for a line the parser does not recognise; a cut line's
   entry gets ``truncated=True``;
4. offline attribution: an entry whose uid the ``uid_packages`` map names gets
   that package; an entry of a secondary user (uid >= 100000) whose uid the map
   does not name matches by app id, uid modulo 100000 (design 5.8);
5. ``where``: an entry it rejects goes no further;
6. ``group_by``: the entry joins the `Grouper`, and the groups it closes come
   out; `Pipeline.finish` closes the rest with reason ``"end"``.

Live attribution (holding a line until its package is known) belongs to the
core, not here: `logpyt._core.attribution` (design 5.8) sets each live line's
package, and the core builds its pipelines without ``uid_packages``, which only
offline reading (`logpyt.read_file`) passes. Exceptions from the parser and from
``where`` propagate unchanged.

Wall-clock time: the pipeline reads no clock itself. The ``ParseContext`` the
caller passes in is its only source of wall time: ``ctx.now()`` dates the lines
that carry no timestamp (brief, process, tag and raw lines, and the fallback
entries), and places the first yearless line when the context has neither a
year nor an anchor. The default `ParseContext` reads the host clock there
(F6-4); a caller that needs a fixed time passes a context whose ``now()``
returns it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from logpyt._core.framing import decode_line
from logpyt._core.grouping import Grouper
from logpyt.models import LogEntry
from logpyt.parsers._fallback import is_divider, unparsed_entry

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from logpyt._core.framing import FramedLine
    from logpyt.filters import Condition
    from logpyt.groupers import GroupBy
    from logpyt.models import LogGroup
    from logpyt.parsers import LogParser, ParseContext

__all__ = ["Pipeline"]

# AID_USER_OFFSET: a uid is user * 100000 + app id (design 5.8).
_PER_USER = 100_000


class Pipeline:
    """The stages of one parse session (see the module docs).

    Not thread-safe: one caller at a time. Every argument is trusted; the
    public entry points validate them.

    Args:
        parser: Turns a line into an entry, or None for a line it does not
            recognise.
        ctx: The session's parse context, used for every line. It is the
            pipeline's only source of wall-clock time (see the module docs).
        where: Entries it rejects are dropped; None keeps every entry.
        group_by: Group the entries; None delivers the entries themselves.
        uid_packages: uid -> package name, for offline attribution: the
            exact uid first, then, for a uid >= 100000, its app id (uid modulo
            100000), so a key per user still wins. None attributes nothing.

    """

    __slots__ = (
        "_ctx",
        "_dividers",
        "_grouper",
        "_lines",
        "_parser",
        "_rejected",
        "_truncated",
        "_uid_packages",
        "_unparsed",
        "_where",
        "_with_uid",
    )

    def __init__(
        self,
        parser: LogParser,
        ctx: ParseContext,
        *,
        where: Condition | None = None,
        group_by: GroupBy | None = None,
        uid_packages: Mapping[int, str] | None = None,
    ) -> None:
        self._parser = parser
        self._ctx = ctx
        self._where = where
        self._grouper = None if group_by is None else Grouper(group_by)
        self._uid_packages = uid_packages
        self._lines = 0
        self._dividers = 0
        self._unparsed = 0
        self._truncated = 0
        self._rejected = 0
        self._with_uid = 0

    @property
    def lines(self) -> int:
        """The lines pushed, dividers included."""
        return self._lines

    @property
    def dividers(self) -> int:
        """The divider lines among them: no entry, not unparsed."""
        return self._dividers

    @property
    def unparsed(self) -> int:
        """The lines the parser did not recognise (fallback entries)."""
        return self._unparsed

    @property
    def truncated(self) -> int:
        """The cut lines among them (``truncated=True``), dividers included."""
        return self._truncated

    @property
    def rejected(self) -> int:
        """The entries ``where`` rejected."""
        return self._rejected

    @property
    def with_uid(self) -> int:
        """The entries whose uid is known (``entry.uid`` is not None), before
        ``where``: only they can be attributed from ``uid_packages``.
        """
        return self._with_uid

    def push(self, line: FramedLine) -> Sequence[LogEntry | LogGroup]:
        """Run ``line`` through every stage; return what it delivers.

        Returns:
            Without ``group_by``: the line's entry, or nothing for a divider or
            a rejected entry. With it: the groups the entry closed, in closing
            order.

        Raises:
            TypeError: The parser returned something other than a `LogEntry`
                or None.

        """
        entry = self.entry(line)
        if entry is None:
            return ()
        if self._grouper is None:
            return (entry,)
        # Offline grouping reads no clock: no group closes as "idle".
        return self._grouper.add(entry, 0.0)

    def entry(self, line: FramedLine) -> LogEntry | None:
        """Run ``line`` through every stage up to ``where``.

        Returns:
            The entry, or None for a divider or an entry ``where`` rejects.

        Raises:
            TypeError: The parser returned something other than a `LogEntry`
                or None.

        """
        self._lines += 1
        if line.truncated:
            self._truncated += 1
        text = decode_line(line.data, cut=line.truncated)
        if is_divider(text):
            self._dividers += 1
            return None
        entry = self._parser.parse(text, self._ctx)
        if entry is None:
            self._unparsed += 1
            entry = unparsed_entry(text, self._ctx)
        elif not isinstance(entry, LogEntry):
            msg = (
                f"{type(self._parser).__name__}.parse returned "
                f"{type(entry).__name__}, not a LogEntry or None"
            )
            raise TypeError(msg)
        if entry.uid is not None:
            self._with_uid += 1
        entry = self._amend(entry, truncated=line.truncated)
        if self._where is not None and not self._where(entry):
            self._rejected += 1
            return None
        return entry

    def finish(self) -> Sequence[LogGroup]:
        """End the session: the groups still open, with reason ``"end"``."""
        if self._grouper is None:
            return ()
        return self._grouper.flush()

    def _amend(self, entry: LogEntry, *, truncated: bool) -> LogEntry:
        """``entry`` with the truncation flag and the attributed package."""
        update: dict[str, object] = {}
        if truncated:
            update["truncated"] = True
        uids = self._uid_packages
        if uids is not None and entry.uid is not None:
            package = uids.get(entry.uid)
            if package is None and entry.uid >= _PER_USER:
                # A secondary user's line matches by app id (design 5.8).
                package = uids.get(entry.uid % _PER_USER)
            if package is not None and package != entry.package:
                update["package"] = package
        # The values are checked already, so no validation is lost (A.16).
        return entry.model_copy(update=update) if update else entry
