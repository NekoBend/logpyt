"""`LogStream`: the thread driver of a live stream (design 2.3, 2.4, 3.3-3.6, 5.3).

`logpyt._core.machine.StreamCore` decides everything about a run (design 5.2):
its states, every timer, the reconnect budget, flow control, framing, parsing,
``where``, grouping, attribution and the outcome. This module only runs adb
and the consumer, and carries out the commands the core returns, as
`logpyt._core.protocol` documents them.

Threads of one run (design 5.3), all daemon threads:

- the engine (`_Run._engine`): takes one event at a time from the mailbox
  (`_Run._take`), hands it to `StreamCore.handle` with the monotonic time and
  carries out the commands (`_Run._carry_out`);
- per connection, a stdout and a stderr reader (`_Run._read`: an unbuffered
  ``read(65536)`` into the data inbox of at most 16 chunks, blocking while it
  is full; each closes its pipe when it ends, X-5) and an exit waiter
  (`_Run._wait_exit`: ``Popen.wait()`` returns at the exit, whatever holds the
  pipes);
- one short-lived worker per one-shot command (`_Run._run_command`);
- in push mode, one dispatcher for the run's whole lifetime
  (`_Run._dispatch`: ``for item in <the delivery iterator>: on_item(item)``).

They share one `threading.Condition`. Its lock guards the mailbox (the
core's `ControlInbox` plus the data inbox), the `Outbox` and what the engine
publishes after every ``handle()``: ``state``, ``stats`` and whether intake is
open. No user code runs under it: ``on_item`` and ``on_event`` run in the
consumer context outside it, ``where`` and ``group_by`` run inside
``handle()``, which the engine calls without it. Waits in user-facing calls
(``join``, ``wait_running``, ``__next__``, with-exit) are sliced to 0.25 s so
that Ctrl+C works on Windows (design 9.2).

The consumer side follows the contract in `logpyt._core.protocol`: one
delivery iterator (`_Run._deliver_next`) for pull and push, each step one
critical section that settles the record in hand, pops the next and reports
the outbox counts. A report waits in one slot for the engine, which posts it
into the control inbox before it takes anything else, so a request posted
after it never comes first; the consumer wakes the engine only when the report
can matter there (nothing was popped, or the outbox is at its low-water mark
while intake is closed), so a fast consumer does not wake the engine per item.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import logging
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Self, assert_never, cast, overload

from logpyt import adb
from logpyt._core.machine import StreamCore
from logpyt._core.mailbox import ControlInbox, Outbox
from logpyt._core.protocol import (
    STREAM_LOGGER,
    CallbackFailed,
    CommandDone,
    ConsumerDetached,
    ConsumerProgress,
    Data,
    Deliver,
    Diagnostic,
    DiscardUndelivered,
    Eof,
    Exited,
    Finished,
    Kill,
    KillRequest,
    RunCommand,
    Spawn,
    Spawned,
    SpawnFailed,
    Start,
    StopRequest,
    Terminate,
    Tick,
)
from logpyt.config import StreamConfig, _parse_logcat_args
from logpyt.exceptions import IncompleteCaptureError, UsageError
from logpyt.filters import _require_condition
from logpyt.groupers import GroupBy
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import StateChange, StreamState

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterable, Iterator
    from types import TracebackType
    from typing import IO, Literal

    from logpyt._core.protocol import Command, Event, Item, OutboxCounts, Pipe, Record
    from logpyt.exceptions import StreamError
    from logpyt.filters import Condition
    from logpyt.streams._events import StreamEvent, StreamStats

__all__ = ["LogStream"]

_log = logging.getLogger(STREAM_LOGGER)

# Design 5.3: one pipe read, and the data inbox's capacity in chunks.
_READ_SIZE: Final = 65536
_DATA_SLOTS: Final = 16
# Design 5.3 and 9.2: the longest wait in a user-facing call, so Ctrl+C works.
_SLICE: Final = 0.25
# Design 3.4: with-exit waits this long after it killed the stream.
_KILL_WAIT: Final = 1.0
_TERMINAL: Final = frozenset(
    {StreamState.STOPPED, StreamState.KILLED, StreamState.FAILED}
)
_TICK: Final = Tick()

# The run whose on_item or on_event is running in this context (F3-7).
_IN_CALLBACK: contextvars.ContextVar[_Run | None] = contextvars.ContextVar(
    "logpyt_stream_callback", default=None
)


@dataclass(slots=True, eq=False)
class _Consumer:
    """The one consumer of a run: the push dispatcher or a pull iterator.

    Attributes:
        owner: The thread that created a pull iterator (design 3.5), by a
            weak reference (`_thread_ref`); None for the dispatcher.
        push: Whether this is the dispatcher.
        attached: False once it detached (`_Run._detach`).
        owe_done: The record in hand was finished with; the next step settles
            it (`Outbox.done`).
        failed: Its callback raised: it takes nothing more (design A.21).
        reached_end: It saw the end of the outbox (or failed), so a pull
            iterator raises a FAILED outcome.
    """

    owner: weakref.ref[threading.Thread] | None
    push: bool
    attached: bool = True
    owe_done: bool = False
    failed: bool = False
    reached_end: bool = False


class _Run:
    """One run's mailbox, outbox, workers and consumer side (see the module docs)."""

    def __init__(
        self,
        core: StreamCore,
        config: StreamConfig,
        *,
        on_item: Callable[[Any], object] | None,
        on_event: Callable[[StreamEvent], object] | None,
    ) -> None:
        self.core = core
        self.on_item = on_item
        self.on_event = on_event
        self.cond = threading.Condition()
        self.control = ControlInbox()
        self.data: deque[Data | Eof] = deque()
        self.data_closed = False
        self.outbox = Outbox()
        self.procs: dict[int, subprocess.Popen[bytes]] = {}
        self.buffer_entries = config.buffer_entries
        self.buffer_bytes = config.buffer_bytes
        # What the engine publishes after every handle() (design 3.6).
        self.state = StreamState.IDLE
        self.stats = core.stats()
        self.intake_open = True
        self.concluded = False
        self.reached_running = False
        self.finished = False  # Finished was carried out
        self.outcome: StreamError | None = None
        self.ended = False  # the engine is gone: what join() waits for
        self.crash: BaseException | None = None
        # The consumer side.
        self.started = False
        self.kill_flag = False
        self.consumer: _Consumer | None = None
        self.consumed = False  # a consumer was attached once
        self.pending: OutboxCounts | None = None  # a report the engine has not taken
        self.posted: OutboxCounts | None = None  # the counts reported last
        self.seen = False  # join(), iteration or wait_running() raised the outcome
        self.orphaned = False  # the LogStream was garbage-collected
        # The worker threads (`_thread_ref`), for the F3-7 check.
        self.engine_thread: weakref.ref[threading.Thread] | None = None
        self.dispatcher_thread: weakref.ref[threading.Thread] | None = None

    # -- control, from any thread ------------------------------------------------------

    def start(self, *, push: bool) -> bool:
        """Start the engine (and the dispatcher); False if already started."""
        with self.cond:
            if self.started:
                return False
            self.started = True
            if push:
                self.consumer = _Consumer(owner=None, push=True)
                self.consumed = True
            self.control.post(Start())
        threading.Thread(target=self._engine, name="logpyt-engine", daemon=True).start()
        if push:
            threading.Thread(
                target=self._dispatch, name="logpyt-dispatcher", daemon=True
            ).start()
        return True

    def stop(self, flush: float | None, *, detach_own: bool = False) -> None:
        """Post a stop; detach the outbox when no consumer is attached (3.5)."""
        with self.cond:
            if detach_own:
                consumer = self.consumer
                if consumer is not None and _owned_here(consumer):
                    self._detach(consumer)
            if not self.started or self.finished:
                return  # D-6, G2
            self._post(StopRequest(flush))
            if self.consumer is None and not self.outbox.detached:
                # Nobody takes the rest: discard and count it (design 3.5).
                self.outbox.detach()
                self._post(ConsumerDetached(self._counts()))

    def kill(self, *, detach_own: bool = False) -> None:
        """Set the kill flag and post the kill where the core acts (F2-7, D-6)."""
        with self.cond:
            if detach_own:
                consumer = self.consumer
                if consumer is not None and _owned_here(consumer):
                    self._detach(consumer)
            if not self.started or self.finished:
                return
            self.kill_flag = True
            self._post(KillRequest())

    def abandon(self) -> None:
        """The LogStream was garbage-collected: kill a running run (design 3.4)."""
        late: str | None = None
        with self.cond:
            self.orphaned = True
            if self.finished:
                late = self._unseen_outcome_message()
            elif self.started:
                self.kill_flag = True
                self._post(KillRequest())
        if late is not None:
            _log.error("%s", late)

    def kill_children(self) -> None:
        """Kill every live adb child at once (interpreter shutdown: no threads)."""
        for proc in list(self.procs.values()):
            with contextlib.suppress(OSError):
                proc.kill()

    def attach(self) -> _Consumer:
        """Attach a pull iterator, or raise UsageError (design 2.3, 4.2)."""
        with self.cond:
            if not self.started:
                msg = (
                    "LogStream iterated before start(): use `with LogStream(...) as "
                    "stream: for entry in stream: ...`, or call stream.start() first"
                )
                raise UsageError(msg)
            if self.consumed:
                msg = (
                    "LogStream has one iterator per run, and it was already created; "
                    "keep that iterator, or use stream.clone() for a new run"
                )
                raise UsageError(msg)
            if self.outbox.detached:
                msg = (
                    "LogStream iterated after stop() without an attached iterator: "
                    "stop() discarded the rest (design 3.5); iterate first, then stop()"
                )
                raise UsageError(msg)
            consumer = _Consumer(owner=_thread_ref(), push=False)
            self.consumer = consumer
            self.consumed = True
            return consumer

    def detach(self, consumer: _Consumer) -> None:
        """A pull iterator was closed or dropped."""
        with self.cond:
            self._detach(consumer)

    # -- waits, from user code ------------------------------------------------------

    def check_wait(self, what: str, *, iterator: bool) -> None:
        """UsageError for a wait from the stream's own user code (F3-7, design 3.5)."""
        if (
            _IN_CALLBACK.get() is self
            or _is_current(self.engine_thread)
            or _is_current(self.dispatcher_thread)
        ):
            msg = (
                f"{what} called from a stream callback; call stream.stop() and "
                "return instead"
            )
            raise UsageError(msg)
        consumer = self.consumer
        if iterator and consumer is not None and _owned_here(consumer):
            msg = (
                f"{what} would wait for this thread's own iterator; iterate to the "
                "end, break, or call it.close() first"
            )
            raise UsageError(msg)

    def wait_ended(self, timeout: float | None, what: str) -> None:
        """Wait in slices until the engine has ended; TimeoutError on expiry."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self.cond:
            while not self.ended:
                if deadline is None:
                    self.cond.wait(_SLICE)
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    msg = (
                        f"{what} timed out after {timeout:g} s; the stream keeps "
                        "running"
                    )
                    raise TimeoutError(msg)
                self.cond.wait(min(_SLICE, left))

    def wait_running(self, timeout: float | None) -> None:
        """Wait until the first RUNNING or the run's conclusion (design A.21)."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self.cond:
            while not (self.reached_running or self.concluded or self.crash):
                left = _SLICE if deadline is None else deadline - time.monotonic()
                if left <= 0:
                    msg = (
                        f"wait_running() timed out after {timeout:g} s; the stream "
                        f"is {self.state} and keeps running"
                    )
                    raise TimeoutError(msg)
                self.cond.wait(min(_SLICE, left))
            if self.reached_running and self.crash is None:
                return
            error = self._take_error(unseen_only=False)
        if error is not None:
            raise error

    def wait_while_progressing(self, close_timeout: float) -> bool:
        """Wait until the end while the consumer takes items; False once it took
        nothing for ``close_timeout`` (design 3.4).
        """
        with self.cond:
            last = self.outbox.counts()
            since = time.monotonic()
            while not self.ended:
                now = time.monotonic()
                counts = self.outbox.counts()
                if (counts.taken_entries, counts.handled) != (
                    last.taken_entries,
                    last.handled,
                ):
                    last, since = counts, now
                elif now - since >= close_timeout:
                    return False
                self.cond.wait(min(_SLICE, close_timeout - (now - since)))
            return True

    def take_error(self, *, unseen_only: bool) -> BaseException | None:
        """The error a finished run raises (its FAILED outcome), marked seen."""
        with self.cond:
            return self._take_error(unseen_only=unseen_only)

    def _take_error(self, *, unseen_only: bool) -> BaseException | None:
        if self.crash is not None:
            return self.crash
        if self.outcome is None or (unseen_only and self.seen):
            return None
        self.seen = True
        return self.outcome

    # -- the consumer side: one delivery iterator for pull and push -----------------

    def _dispatch(self) -> None:
        """Push mode: ``for item in <the delivery iterator>: on_item(item)`` (A-4)."""
        self.dispatcher_thread = _thread_ref()
        consumer = self.consumer
        on_item = self.on_item
        assert consumer is not None  # noqa: S101  (set by start)
        assert on_item is not None  # noqa: S101  (push mode has one)
        try:
            while (item := self.deliver_next(consumer, sliced=False)) is not None:
                if not self._call(consumer, "on_item", on_item, item):
                    return
        finally:
            self.detach(consumer)

    def deliver_next(self, consumer: _Consumer, *, sliced: bool) -> Item | None:
        """The next item for ``consumer``, ``on_event`` run for the events before it.

        None at the consumer's end: the outbox ended, it was detached, or a
        callback raised (``on_event`` here, ``on_item`` before).
        """
        while True:
            record = self._step(consumer, sliced=sliced)
            if record is None:
                return None
            if isinstance(record, LogEntry | LogGroup):
                return record
            on_event = self.on_event
            if on_event is None:
                consumer.owe_done = True
            elif not self._call(consumer, "on_event", on_event, record):
                return None

    def _step(self, consumer: _Consumer, *, sliced: bool) -> Record | None:
        """Settle, pop and report in one critical section (protocol module docs)."""
        with self.cond:
            while True:
                if consumer.failed:
                    self._await_close(consumer, sliced=sliced)
                    return None
                if consumer.owe_done:
                    consumer.owe_done = False
                    self.outbox.done()
                if not consumer.attached:
                    return None
                if self.kill_flag and self.outbox.entries and not self.outbox.closed:
                    # F2-7: no item is taken after kill(). The core discards
                    # them (DiscardUndelivered, counted in undelivered: 3.4),
                    # and the events behind them still come.
                    self._report(empty=True)
                    self.cond.wait(_SLICE if sliced else None)
                    continue
                record = self.outbox.pop()
                self._report(empty=record is None)
                if record is None:
                    if self.outbox.ended:
                        consumer.reached_end = True
                        self._detach(consumer)
                        return None
                    self.cond.wait(_SLICE if sliced else None)
                    continue
                return record

    def _call(
        self,
        consumer: _Consumer,
        stage: Literal["on_item", "on_event"],
        callback: Callable[[Any], object],
        record: Record,
    ) -> bool:
        """Run one callback in the consumer context; False if it failed (2.4)."""
        token = _IN_CALLBACK.set(self)
        try:
            result = callback(record)
            if result is not None and inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                msg = (
                    f"{stage} returned an awaitable, which LogStream never awaits; "
                    "use AsyncLogStream for async callbacks"
                )
                raise TypeError(msg)
        except Exception as exc:  # noqa: BLE001  (fail fast: CallbackError)
            self._fail(consumer, stage, exc)
            return False
        except BaseException as exc:
            if consumer.push:  # SystemExit in the dispatcher thread (F3-6)
                self._fail(consumer, stage, exc)
                return False
            # Interrupted, not failed: the next step settles the record.
            consumer.owe_done = True
            raise
        finally:
            _IN_CALLBACK.reset(token)
        consumer.owe_done = True
        return True

    def _fail(
        self,
        consumer: _Consumer,
        stage: Literal["on_item", "on_event"],
        error: BaseException,
    ) -> None:
        """Post the failure; take nothing more; detach once the outbox is closed."""
        with self.cond:
            consumer.failed = True
            late = self._post(CallbackFailed(stage, error))
        if late is not None:
            _log.error("%s", late, exc_info=error)
        with self.cond:
            self._await_close(consumer, sliced=not consumer.push)

    def _await_close(self, consumer: _Consumer, *, sliced: bool) -> None:
        """After a failure: wait (lock held) until the outbox is closed, then detach."""
        while consumer.attached and not self.outbox.closed:
            self.cond.wait(_SLICE if sliced else None)
        consumer.reached_end = True
        self._detach(consumer)

    def _report(self, *, empty: bool) -> None:
        """The consumer side's report of the outbox counts (lock held).

        The engine takes it before any later request (`_take`); it is woken
        only when the report can change its course.
        """
        counts = self.outbox.counts()
        if counts is self.posted:
            return
        self.posted = counts
        if self.ended:
            self._handle_late(ConsumerProgress(counts))
            return
        self.pending = counts
        low = (
            2 * self.outbox.entries <= self.buffer_entries
            and 2 * self.outbox.nbytes <= self.buffer_bytes
        )
        if empty or (low and not self.intake_open):
            self.cond.notify_all()

    def _detach(self, consumer: _Consumer) -> None:
        """`Outbox.detach` and `ConsumerDetached` in one critical section (locked)."""
        if not consumer.attached:
            return
        consumer.attached = False
        if self.consumer is consumer:
            self.consumer = None
        self.outbox.detach()
        self._post(ConsumerDetached(self._counts()))

    def _counts(self) -> OutboxCounts:
        counts = self.outbox.counts()
        self.posted = counts
        return counts

    # -- the mailbox ------------------------------------------------------------------

    def _post(self, event: Event) -> str | None:
        """Hand ``event`` to the engine (lock held); after its end, to the core.

        Returns the ERROR message of a callback failure that came after the
        engine ended (`StreamCore.late_failure`), for the caller to log.
        """
        if self.ended:
            return self._handle_late(event)
        self.control.post(event)
        self.cond.notify_all()
        return None

    def _handle_late(self, event: Event) -> str | None:
        """The engine has ended: the core is ours under the lock (G2: accounting)."""
        if self.crash is not None or not isinstance(
            event, ConsumerProgress | ConsumerDetached | CallbackFailed
        ):
            return None
        late = (
            self.core.late_failure(event) if isinstance(event, CallbackFailed) else None
        )
        self.core.handle(event, time.monotonic())
        self.stats = self.core.stats()
        return late

    def _take(self, *, intake_open: bool, deadline: float | None) -> Event | None:
        """The engine's next event (design 5.3); None once the run is over.

        The control inbox comes first, then data while intake is open, then a
        `Tick` at the deadline. The run is over once `Finished` was carried
        out and no consumer is attached any more.
        """
        with self.cond:
            while True:
                pending = self.pending
                if pending is not None:
                    self.pending = None
                    self.control.post(ConsumerProgress(pending))
                event = self.control.take()
                if event is not None:
                    return event
                data = self.data
                if data and intake_open:
                    if len(data) >= _DATA_SLOTS:
                        self.cond.notify_all()  # a reader waits for room
                    return data.popleft()
                if self.finished and self.consumer is None:
                    self._end()
                    return None
                if deadline is None:
                    self.cond.wait()
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    return _TICK
                self.cond.wait(left)

    def _end(self) -> None:
        """The engine is done (lock held): join() returns; readers drop their data."""
        self.ended = True
        self.data_closed = True
        self.data.clear()
        self.cond.notify_all()

    # -- the engine ---------------------------------------------------------------

    def _engine(self) -> None:
        self.engine_thread = _thread_ref()
        core = self.core
        try:
            while (
                event := self._take(
                    intake_open=core.intake_open, deadline=core.next_deadline()
                )
            ) is not None:
                if isinstance(event, CallbackFailed):
                    late = core.late_failure(event)
                    if late is not None:
                        _log.error("%s", late, exc_info=event.error)
                self._carry_out(core.handle(event, time.monotonic()))
        except BaseException as exc:  # noqa: BLE001  (a bug: end the run visibly)
            self._crashed(exc)

    def _carry_out(self, commands: Iterable[Command]) -> None:
        """Carry out one ``handle()`` batch in order, then report and publish."""
        changed = False  # the batch changed the outbox counts
        for command in commands:
            match command:
                case Deliver(records=records):
                    changed |= self._deliver(records)
                case DiscardUndelivered():
                    with self.cond:
                        before = self.outbox.counts()
                        self.outbox.discard()
                        changed |= self.outbox.counts() is not before
                        self.cond.notify_all()  # a consumer waits under the kill flag
                case Diagnostic(level=level, message=message, logger=name):
                    logging.getLogger(name).log(level, "%s", message)
                case Spawn(connection=connection, argv=argv):
                    self._spawn(connection, argv)
                case Terminate(connection=connection):
                    self._signal(connection, kill=False)
                case Kill(connection=connection):
                    self._signal(connection, kill=True)
                case RunCommand():
                    threading.Thread(
                        target=self._run_command,
                        args=(command,),
                        name=f"logpyt-{command.kind}",
                        daemon=True,
                    ).start()
                case Finished(outcome=outcome):
                    self._finished(outcome)
                    changed = False
                case _:
                    assert_never(command)
        with self.cond:
            if changed:
                self.control.post(ConsumerProgress(self._counts()))
            self._publish()

    def _deliver(self, records: tuple[Record, ...]) -> bool:
        """`Deliver`: append to the outbox; whether that changed its counts."""
        with self.cond:
            before = self.outbox.counts()
            self.outbox.put(records)
            if not self.reached_running and any(
                isinstance(record, StateChange) and record.new is StreamState.RUNNING
                for record in records
            ):
                self.reached_running = True
            self.cond.notify_all()
            return self.outbox.counts() is not before

    def _finished(self, outcome: StreamError | None) -> None:
        """`Finished`: the engine side's last report first, then the end (protocol)."""
        with self.cond:
            counts = self._counts()
        # The core is terminal now: this updates only its accounting (G2).
        self.core.handle(ConsumerProgress(counts), time.monotonic())
        with self.cond:
            self.outbox.close()
            self.finished = True
            self.outcome = outcome
            self._publish()
            late = self._unseen_outcome_message() if self.orphaned else None
            self.cond.notify_all()
        if late is not None:
            _log.error("%s", late)

    def _publish(self) -> None:
        """Publish the core's state, stats and intake (lock held; design 3.6)."""
        core = self.core
        self.stats = core.stats()
        self.intake_open = core.intake_open
        state = core.state
        concluded = core.concluded
        if state is not self.state or concluded is not self.concluded:
            self.state = state
            self.concluded = concluded
            if state is StreamState.RUNNING:
                self.reached_running = True
            self.cond.notify_all()

    def _unseen_outcome_message(self) -> str | None:
        """The ERROR for a FAILED outcome nobody saw (lock held; design 3.4)."""
        if self.outcome is None or self.seen:
            return None
        self.seen = True
        return (
            "a LogStream was garbage-collected and its run ended FAILED without "
            f"anyone seeing it: {type(self.outcome).__name__}: {self.outcome}"
        )

    def _crashed(self, error: BaseException) -> None:
        """The engine failed (a logpyt bug): end the run so no wait hangs."""
        _log.error("the LogStream engine failed; the run is abandoned", exc_info=error)
        with self.cond:
            self.crash = error
            self.finished = True
            self.outbox.close()
            self._end()
        self.kill_children()

    # -- workers ---------------------------------------------------------------------

    def _spawn(self, connection: int, argv: tuple[str, ...]) -> None:
        """`Spawn`: start adb, report it, start its readers and its exit waiter."""
        try:
            # Through the module, like resolve_adb: one patch point for tests.
            proc = adb._spawn_adb(argv)  # noqa: SLF001
        except OSError as exc:
            with self.cond:
                self._post(SpawnFailed(connection, exc))
            return
        with self.cond:
            self.procs[connection] = proc
            # Posted before any reader runs, so the core sees it before the data.
            self._post(Spawned(connection, proc.pid))
        pipes: tuple[tuple[Pipe, IO[bytes] | None], ...] = (
            ("stdout", proc.stdout),
            ("stderr", proc.stderr),
        )
        for name, pipe in pipes:
            assert pipe is not None  # noqa: S101  (spawned with PIPE)
            threading.Thread(
                target=self._read,
                args=(connection, name, pipe),
                name=f"logpyt-{name}-{connection}",
                daemon=True,
            ).start()
        threading.Thread(
            target=self._wait_exit,
            args=(connection, proc),
            name=f"logpyt-exit-{connection}",
            daemon=True,
        ).start()

    def _signal(self, connection: int, *, kill: bool) -> None:
        """`Terminate` or `Kill`: the child's pid only, never its group (5.11)."""
        with self.cond:
            proc = self.procs.get(connection)
        if proc is None:
            return  # it has exited
        with contextlib.suppress(OSError):
            if kill:
                proc.kill()
            else:
                proc.terminate()

    def _read(self, connection: int, pipe_name: Pipe, pipe: IO[bytes]) -> None:
        """Read one pipe to EOF into the data inbox; close it at the end (X-5)."""
        try:
            while True:
                try:
                    chunk = pipe.read(_READ_SIZE)
                except (OSError, ValueError):
                    chunk = b""  # the pipe broke: that is its end
                if not chunk:
                    break
                if not self._put(Data(connection, pipe_name, chunk)):
                    return
            self._put(Eof(connection, pipe_name))
        finally:
            with contextlib.suppress(OSError):
                pipe.close()

    def _put(self, event: Data | Eof) -> bool:
        """Append to the data inbox, waiting while it is full; False once closed."""
        with self.cond:
            while len(self.data) >= _DATA_SLOTS and not self.data_closed:
                self.cond.wait()
            if self.data_closed:
                return False
            self.data.append(event)
            self.cond.notify_all()
            return True

    def _wait_exit(self, connection: int, proc: subprocess.Popen[bytes]) -> None:
        returncode = proc.wait()
        with self.cond:
            self.procs.pop(connection, None)
            self._post(Exited(connection, returncode))

    def _run_command(self, command: RunCommand) -> None:
        """`RunCommand`: the one-shot runner; its result or error goes back."""
        result: Any
        try:
            result = adb._run_adb(command.argv, timeout=command.timeout)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001  (the core reads the error)
            result = exc
        with self.cond:
            self._post(CommandDone(command.request, result))


def _thread_ref() -> weakref.ref[threading.Thread]:
    """The current thread, by a weak reference to its `threading.Thread`.

    Not by its ident: the OS hands an ident out again once its thread ended,
    so a new thread could pass for an ended engine, dispatcher or iterator
    owner. A weak reference does not keep the ended thread's object (and so,
    on CPython 3.13+, its unjoined OS thread) alive.
    """
    return weakref.ref(threading.current_thread())


def _is_current(thread: weakref.ref[threading.Thread] | None) -> bool:
    """Whether ``thread`` (a `_thread_ref`) is the current thread."""
    return thread is not None and thread() is threading.current_thread()


def _owned_here(consumer: _Consumer) -> bool:
    """Whether ``consumer`` is a pull iterator this thread created (design 3.5)."""
    return not consumer.push and _is_current(consumer.owner)


def _abandon(run: _Run) -> None:
    """`weakref.finalize` of a started `LogStream`: kill its run (design 3.4).

    It may run inside any critical section (a cyclic collection), so the kill
    is posted from a new thread. At interpreter shutdown (the main thread has
    ended: weakref.finalize runs at exit) the engine may never run again, so
    the adb children are killed at once as well. A run that ended and whose
    outcome was seen (or is None) needs nothing.
    """
    if run.finished and (run.outcome is None or run.seen):
        return
    if not threading.main_thread().is_alive():
        run.kill_children()
    try:
        threading.Thread(
            target=run.abandon, name="logpyt-finalizer", daemon=True
        ).start()
    except RuntimeError:
        run.kill_children()


def _kill_and_wait(run: _Run, *, detach_own: bool = False) -> None:
    """with-exit's kill: ``kill()``, then wait at most 1 s for the end (3.4).

    The kill is posted before the wait, so a second Ctrl+C there only cuts
    the wait short.
    """
    run.kill(detach_own=detach_own)
    with contextlib.suppress(TimeoutError):
        run.wait_ended(_KILL_WAIT, "the with-block exit")


def _check_callback(name: str, callback: object) -> None:
    """TypeError for a callback that is not callable or is async (design 2.3)."""
    if callback is None:
        return
    if not callable(callback):
        msg = f"{name} must be callable or None, not {type(callback).__name__}"
        raise TypeError(msg)
    call = type(callback).__call__  # an object with an async __call__
    if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(call):
        msg = (
            f"{name} is an async function, which LogStream never awaits; use "
            "AsyncLogStream, or pass a plain function"
        )
        raise TypeError(msg)


class _PullIterator[T]:
    """The pull iterator of a stream (design 3.5, A-21): a class, not a generator.

    It keeps its stream alive. ``close()``, and ``__del__`` when a ``break``
    drops the last reference, detach it at once; the rest is discarded and
    counted.
    """

    __slots__ = ("_consumer", "_done", "_handed", "_run", "_stream")

    def __init__(self, stream: LogStream[T], run: _Run, consumer: _Consumer) -> None:
        self._stream = stream
        self._run = run
        self._consumer = consumer
        self._done = False
        self._handed = False  # an item was returned; asking for the next settles it

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> T:
        if self._done:
            raise StopIteration
        consumer = self._consumer
        if self._handed:
            self._handed = False
            consumer.owe_done = True
        item = self._run.deliver_next(consumer, sliced=True)
        if item is not None:
            self._handed = True
            return cast("T", item)
        self._done = True
        if self._consumer.reached_end:
            # A FAILED run raises its outcome once, after the last item (4.2).
            error = self._run.take_error(unseen_only=False)
            if error is not None:
                raise error
        raise StopIteration

    def close(self) -> None:
        """Detach from the stream; what it did not take is discarded and counted."""
        self._done = True
        self._run.detach(self._consumer)

    def __del__(self) -> None:
        with contextlib.suppress(AttributeError):
            self.close()


class LogStream[T]:
    """A live ``adb logcat`` stream run by threads (design 2.3).

    ``T`` is `LogEntry`, or `LogGroup` when ``group_by`` is given. A stream runs
    once: iterate it (pull), or pass ``on_item`` (push, one dispatcher thread);
    ``on_event`` receives the `StateChange` and `Gap` events in order with the
    items, in the consumer context. ``where`` and ``group_by`` run in the
    engine thread. Any of them raising ends the run with `CallbackError`.

    Args:
        config: The configuration; None uses ``StreamConfig()``.
        where: The condition each entry must match; None delivers every entry.
        group_by: Deliver `LogGroup` items instead of entries.
        on_item: Push mode: called with each item on the dispatcher thread.
        on_event: Called with each event in the consumer context.

    Raises:
        TypeError: An argument has the wrong type, or a callback is async
            (use `AsyncLogStream`).
        ValueError: ``group_by`` cannot fit the outbox, or ``where.packages()``
            names something that is not a package name.
        FileNotFoundError: ``config.adb_path`` is None and no adb is found.

    """

    @overload
    def __init__(
        self: LogStream[LogEntry],
        config: StreamConfig | None = None,
        /,
        *,
        where: Condition | None = None,
        group_by: None = None,
        on_item: Callable[[LogEntry], object] | None = None,
        on_event: Callable[[StreamEvent], object] | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self: LogStream[LogGroup],
        config: StreamConfig | None = None,
        /,
        *,
        where: Condition | None = None,
        group_by: GroupBy,
        on_item: Callable[[LogGroup], object] | None = None,
        on_event: Callable[[StreamEvent], object] | None = None,
    ) -> None: ...
    def __init__(
        self,
        config: StreamConfig | None = None,
        /,
        *,
        where: Condition | None = None,
        group_by: GroupBy | None = None,
        on_item: Callable[[LogEntry], object]
        | Callable[[LogGroup], object]
        | None = None,
        on_event: Callable[[StreamEvent], object] | None = None,
    ) -> None:
        if config is None:
            config = StreamConfig()
        elif not isinstance(config, StreamConfig):
            msg = f"config must be a StreamConfig or None, not {type(config).__name__}"
            raise TypeError(msg)
        if where is not None:
            where = _require_condition(where, "where")
        if group_by is not None and not isinstance(group_by, GroupBy):
            msg = f"group_by must be a GroupBy or None, not {type(group_by).__name__}"
            raise TypeError(msg)
        _check_callback("on_item", on_item)
        _check_callback("on_event", on_event)
        adb_path = config.adb_path if config.adb_path is not None else adb.resolve_adb()
        self._setup(config, adb_path, where, group_by, on_item, on_event)

    def _setup(
        self,
        config: StreamConfig,
        adb_path: str,
        where: Condition | None,
        group_by: GroupBy | None,
        on_item: Callable[[Any], object] | None,
        on_event: Callable[[StreamEvent], object] | None,
    ) -> None:
        core = StreamCore(config, adb_path=adb_path, where=where, group_by=group_by)
        self._config = config
        self._adb_path = adb_path
        self._where = where
        self._group_by = group_by
        self._on_item = on_item
        self._on_event = on_event
        self._flush_hidden_by = _parse_logcat_args(
            config.logcat_args
        ).marker_hidden_by()
        self._run = _Run(core, config, on_item=on_item, on_event=on_event)

    # -- properties --------------------------------------------------------------------

    @property
    def config(self) -> StreamConfig:
        """The configuration of this stream."""
        return self._config

    @property
    def state(self) -> StreamState:
        """The state, as the engine published it after its last step (design 3.6)."""
        return self._run.state

    @property
    def stats(self) -> StreamStats:
        """An immutable snapshot of the run's counters (design 2.5, 4.4)."""
        return self._run.stats

    @property
    def outcome(self) -> StreamError | None:
        """The latched error of a run that ended FAILED; None otherwise."""
        return self._run.outcome

    # -- control -----------------------------------------------------------------------

    def start(self) -> None:
        """Start the run and return at once; it never runs user code (design 3.3).

        Raises:
            UsageError: The stream was started before (use `clone`).

        """
        run = self._run
        if not run.start(push=self._on_item is not None):
            state = run.state
            what = "a started" if state is StreamState.IDLE else f"a {state}"
            msg = (
                f"LogStream.start() called on {what} stream; a stream runs once "
                "- use stream.clone()"
            )
            raise UsageError(msg)
        weakref.finalize(self, _abandon, run)

    def stop(self, *, flush: float | None = None) -> None:
        """Ask the run to stop; from any thread or callback; idempotent.

        With ``flush``, a marker is written to the device log and the stream
        reads until it, for at most ``flush`` seconds of open intake (design
        2.3). Before ``start()`` and after the end it does nothing.

        Raises:
            UsageError: ``flush`` is given and ``logcat_args`` hides the marker.
            TypeError: ``flush`` is not a number.

        """
        if flush is not None:
            if isinstance(flush, bool) or not isinstance(flush, int | float):
                msg = f"flush takes seconds as a number or None, not {flush!r}"
                raise TypeError(msg)
            if self._flush_hidden_by:
                msg = (
                    "stop(flush=...) needs the flush marker, which "
                    f"{' and '.join(self._flush_hidden_by)} in logcat_args hides; "
                    "call stop() without flush"
                )
                raise UsageError(msg)
        self._run.stop(None if flush is None else float(flush))

    def kill(self) -> None:
        """Kill adb at once and discard undelivered items; idempotent (design 3.4)."""
        self._run.kill()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the end of delivery; raise the outcome of a FAILED run.

        Returns once the run ended and its consumer finished (or detached).

        Raises:
            StreamError: The run ended FAILED (the same object every time).
            TimeoutError: ``timeout`` seconds passed; the stream keeps running.
            UsageError: Called from the stream's own callbacks, from the thread
                that owns an attached iterator, or before ``start()``.

        """
        run = self._run
        run.check_wait("join()", iterator=True)
        if not run.started:
            msg = (
                "join() called before start(); start the stream, or use `with stream:`"
            )
            raise UsageError(msg)
        run.wait_ended(timeout, "join()")
        error = run.take_error(unseen_only=False)
        if error is not None:
            raise error

    def wait_running(self, timeout: float | None = None) -> None:
        """Wait until a connection is RUNNING (adb printed), or the run concluded.

        Raises:
            StreamError: The run concluded FAILED before it was RUNNING.
            TimeoutError: ``timeout`` seconds passed; the stream keeps running.
            UsageError: Called from the stream's own callbacks or before
                ``start()``.

        """
        run = self._run
        run.check_wait("wait_running()", iterator=False)
        if not run.started:
            msg = "wait_running() called before start(); start the stream first"
            raise UsageError(msg)
        run.wait_running(timeout)

    def assert_complete(self) -> None:
        """Fail a CI test whose capture may miss device lines (design 4.4).

        Raises:
            UsageError: The run has not ended; call ``join()`` first.
            StreamError: The run ended FAILED.
            IncompleteCaptureError: ``stats.incomplete`` names a reason.

        """
        run = self._run
        state = run.state
        if run.crash is None and state not in _TERMINAL:
            msg = f"assert_complete() on a {state} stream; call join() first"
            raise UsageError(msg)
        error = run.take_error(unseen_only=False)
        if error is not None:
            raise error
        reasons = run.stats.incomplete
        if reasons:
            msg = f"the capture is incomplete: {', '.join(reasons)}"
            raise IncompleteCaptureError(msg, reasons=reasons)

    def clone(self) -> Self:
        """A fresh IDLE stream: same config, adb path, where, group_by and callbacks."""
        twin = object.__new__(type(self))
        twin._setup(  # noqa: SLF001  (a LogStream sets up its own twin)
            self._config,
            self._adb_path,
            self._where,
            self._group_by,
            self._on_item,
            self._on_event,
        )
        return twin

    # -- iteration and with ------------------------------------------------------------

    def __iter__(self) -> Iterator[T]:
        """Pull mode: the one iterator of this run (design 2.4, 3.5).

        Raises:
            UsageError: The stream has ``on_item``, was not started, or was
                iterated before (or stop() discarded what no iterator took).

        """
        if self._on_item is not None:
            msg = (
                "LogStream with on_item cannot be iterated: its dispatcher takes "
                "the items; leave out on_item to iterate"
            )
            raise UsageError(msg)
        return _PullIterator(self, self._run, self._run.attach())

    def __enter__(self) -> Self:
        """Start the stream and return it."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop and join; how depends on how the body ended (design 3.4).

        An iterator this thread owns is detached first. A clean body waits
        without a cap and raises an outcome nobody saw. A body that raised an
        `Exception` waits while the consumer takes items, kills once it took
        nothing for ``close_timeout``, and keeps the body's exception, with the
        outcome added as a note. A `GeneratorExit` (a generator around the
        block was closed early) takes the `Exception` path: it is how a
        caller's ``break`` reaches the block, not an interrupt. Any other
        exception kills. An exception that interrupts the exit's own wait
        (Ctrl+C) kills too before it propagates, so neither adb nor the
        consumer outlives the block (design 9.2).
        """
        run = self._run
        if exc is None:
            run.check_wait("the with-block exit", iterator=False)
            try:
                run.stop(None, detach_own=True)
                run.wait_ended(None, "the with-block exit")
            except BaseException:
                _kill_and_wait(run, detach_own=True)
                raise
            error = run.take_error(unseen_only=True)
            if error is not None:
                raise error
            return
        if not isinstance(exc, Exception | GeneratorExit):
            _kill_and_wait(run, detach_own=True)  # KeyboardInterrupt, SystemExit, ...
            return
        try:
            run.stop(None, detach_own=True)
            progressing = run.wait_while_progressing(self._config.close_timeout)
        except BaseException:
            _kill_and_wait(run, detach_own=True)
            raise
        if not progressing:
            _kill_and_wait(run)
        error = run.take_error(unseen_only=True)
        if error is not None and error is not exc:
            exc.add_note(
                f"logpyt: the stream ended {run.state} too: "
                f"{type(error).__name__}: {error}"
            )
            _log.error(
                "the LogStream failed while its with-block raised %s",
                type(exc).__name__,
                exc_info=error,
            )
