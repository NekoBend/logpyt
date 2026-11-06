"""`AsyncLogStream`: the asyncio driver of a live stream (design 2.3-2.4, 3.3-3.6, 5.4).

`logpyt._core.machine.StreamCore` decides everything about a run (design 5.2):
its states, every timer, the reconnect budget, flow control, framing, parsing,
``where``, grouping, attribution and the outcome. This module only runs adb
and the consumer on an event loop, and carries out the commands the core
returns, as `logpyt._core.protocol` documents them. It is the asyncio
translation of `logpyt.streams.sync`, which runs the same contract with
threads.

Tasks of one run (design 5.4), all on the loop that called ``start()``:

- the engine (`_Run._engine`): takes one event at a time (`_Run._take`),
  hands it to `StreamCore.handle` with the loop's monotonic time and carries
  out the commands (`_Run._carry_out`) in that task step, without an
  ``await``; it yields once after each chunk of data (X-1);
- per connection, a spawn task (`_Run._spawn`), then a stdout and a stderr
  reader (`_Run._read`: ``await reader.read(65536)`` into the data inbox, an
  ``asyncio.Queue(16)``, then ``await asyncio.sleep(0)``, because
  ``StreamReader.read`` does not suspend while data is buffered, X-1; each
  closes its pipe when it ends, X-5) and an exit watcher (`_Run._wait_exit`,
  which reads ``returncode`` every 50 ms: a ``Process.wait()`` begun before
  the exit returns only once the pipes close, design 5.1);
- one task per one-shot command (`_Run._run_command`), whose adb child is
  tracked from its spawn until it was reaped; when ``Finished`` is carried
  out the task is cancelled, which kills and reaps that child before
  ``join()`` can return;
- in push mode, one dispatcher for the run's whole lifetime
  (`_Run._dispatch`: ``for item in <the delivery iterator>: on_item(item)``),
  which awaits what a callback returns.

The engine's end cancels and awaits the tasks still running. Cancelled
itself (the loop shuts down), it kills adb at once, starts nothing more and
ends the run like a crash, so no wait hangs (F3-2); a run whose ``Finished``
was carried out keeps its outcome then (G4, `_Run._abandoned`). A task that
raises (a logpyt bug) ends the run as an engine crash does
(`_Run._worker_failed`, `_Run._crashed`): adb is killed and reaped, then
every wait ends and raises that exception. ``KeyboardInterrupt`` (Ctrl+C on
the loop's thread) and ``SystemExit`` (``sys.exit()``, from a SIGTERM
handler say) are neither a bug nor a callback failure, in any task of the
run: the engine (``where`` and ``group_by`` included), an I/O task, or the
dispatcher (a push callback). adb is killed at once and the run ends as for
a cancelled engine, with that exception (unless its ``Finished`` was carried
out); nothing is logged, and it leaves the loop without waiting
(`_Run._interrupted`), as asyncio raises both out of the loop. One in the
engine leaves the engine's end to a task of its own (`_Run._close_later`),
which the loop's teardown runs to its end, so the killed adb is reaped. A
first end of the run is kept: a crash that such an interrupt cut short does
not replace it (`_Run._fail_run`). Two cases in a one-shot command differ,
because adb's own code runs there (`adb._async_run_adb`): one in that
function first waits for the bounded reap of the child it killed, and one in
its pipe readers leaves the loop at once but ends the run only when the
runner next runs (and raises it once more), or through the loop's teardown,
whose waits raise the engine's ``CancelledError``. The thread driver takes
a SystemExit in its dispatcher thread for a callback failure, because a
thread would drop it (design A.23). In pull mode either one propagates from
``__anext__()``, and the next step settles the record in hand.

Everything a run shares lives on its loop, so a task step is the critical
section of the protocol (no lock): the mailbox (the core's `ControlInbox`
plus the data inbox), the `Outbox` and what the engine publishes after every
``handle()``: ``state``, ``stats``, whether intake is open. Waiters are
futures resolved by whoever changes what they wait for: the engine's
(`_Run._notify_engine`), the consumer's (`_Run._wake_consumer`) and those of
``join()``, ``wait_running()`` and with-exit (`_Run._notify_waiters`).
``stop()`` and ``kill()`` are plain functions: on the loop they act at once,
from any other thread they go through ``loop.call_soon_threadsafe`` (``kill()``
sets the consumer's kill flag before it returns, F2-7), and once the loop is
closed they do nothing. ``state``, ``stats`` and ``outcome`` may be read from
any thread.

The consumer side follows the contract in `logpyt._core.protocol`: one
delivery iterator (`_Run.deliver_next`) for pull and push, each step one task
step that settles the record in hand, pops the next and reports the outbox
counts, with no ``await`` between the pop and handing the record to
``on_event`` or to the caller. The consumer yields once per batch of
``_BATCH`` records, before the step settles (X-1), so a cancelled
``__anext__`` cancels only its wait and never loses a record.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import logging
import os
import signal
import sys
import threading
import time
import weakref
from dataclasses import dataclass, replace
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
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import StateChange, StreamState
from logpyt.streams.sync import (
    _check_callable,
    _checked_arguments,
    _checked_flush,
    _ends_run,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Callable,
        Coroutine,
        Iterable,
        Sequence,
    )
    from types import TracebackType
    from typing import Literal

    from logpyt._core.protocol import Command, Event, Item, OutboxCounts, Pipe, Record
    from logpyt.exceptions import StreamError
    from logpyt.filters import Condition
    from logpyt.groupers import GroupBy
    from logpyt.streams._events import StreamEvent, StreamStats

    type _Process = asyncio.subprocess.Process

__all__ = ["AsyncLogStream"]

_log = logging.getLogger(STREAM_LOGGER)

# Design 5.4: one pipe read, and the data inbox's capacity in chunks.
_READ_SIZE: Final = 65536
_DATA_SLOTS: Final = 16
# Design 5.4: how often the exit watcher reads returncode.
_EXIT_POLL: Final = 0.05
# X-1: records a consumer takes between two yields to the loop. One chunk of
# 64 KiB holds about 500 lines, so a batch is a fraction of one read.
_BATCH: Final = 64
# How often with-exit looks at the consumer's progress (design 3.4).
_SLICE: Final = 0.25
# Design 3.4: with-exit waits this long after it killed the stream.
_KILL_WAIT: Final = 1.0
# How long the end of a run (and a crash) waits for the adb children it
# killed to be reaped, so that none is left once join() returns.
_REAP_WAIT: Final = 1.0
# How often a crash looks whether the children it killed were reaped.
_REAP_POLL: Final = 0.01
_TERMINAL: Final = frozenset(
    {StreamState.STOPPED, StreamState.KILLED, StreamState.FAILED}
)
_TICK: Final = Tick()

# The run whose user code (on_item, on_event; the engine for where and
# group_by) runs in this context (F3-7). Tasks created there inherit it, so
# their waits raise UsageError while the run lives (`_Run.check_wait`).
_IN_CALLBACK: contextvars.ContextVar[_Run | None] = contextvars.ContextVar(
    "logpyt_async_stream_callback", default=None
)

# The tasks of every live run. asyncio keeps only weak references to tasks; a
# run whose stream was dropped must still end (and kill adb) on its own.
_LIVE: set[asyncio.Task[Any]] = set()


@dataclass(slots=True, eq=False)
class _Consumer:
    """The one consumer of a run: the push dispatcher or a pull iterator.

    Attributes:
        owner: The task that created a pull iterator (design 3.5), by a weak
            reference; None for the dispatcher.
        push: Whether this is the dispatcher.
        attached: False once it detached (`_Run._detach`).
        owe_done: The record in hand was finished with; the next step settles
            it (`Outbox.done`).
        failed: Its callback raised: it takes nothing more (design A.21).
        reached_end: It saw the end of the outbox (or failed), so a pull
            iterator raises a FAILED outcome.
        taken: Records taken since it last yielded to the loop (X-1).
        busy: A step is in progress (one ``__anext__`` at a time).
    """

    owner: weakref.ref[asyncio.Task[Any]] | None
    push: bool
    attached: bool = True
    owe_done: bool = False
    failed: bool = False
    reached_end: bool = False
    taken: int = 0
    busy: bool = False


class _Run:
    """One run's mailbox, outbox, tasks and consumer side (see the module docs)."""

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
        # Loop-bound state is made in start(), on the running loop (F2-9).
        self.loop: asyncio.AbstractEventLoop | None = None
        self.data: asyncio.Queue[Data | Eof] | None = None
        self.control = ControlInbox()
        self.data_closed = False
        self.outbox = Outbox()
        self.procs: dict[int, _Process] = {}  # logcat, until its exit was seen
        self.oneshots: set[_Process] = set()  # one-shot, until reaped
        self.workers: set[asyncio.Task[None]] = set()  # spawns, readers, watchers
        self.commands: set[asyncio.Task[None]] = set()  # one-shot commands
        self.reaping = False  # the run is over: a one-shot child is killed at once
        self.closing = False  # interpreter exit: no adb child is started any more
        # adb spawns in progress whose child is not tracked yet; interpreter
        # exit lets them finish (`_abandon`).
        self.spawning = 0
        self.failure: BaseException | None = None  # a task raised (a logpyt bug)
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
        self.ended = False  # the engine is done: what join() waits for
        self.crash: BaseException | None = None
        # The consumer side.
        self.started = False
        self.kill_flag = False
        # kill() from another thread sets the flag under it, and a consumer
        # step reads the flag and pops under it, so no pop begins after such
        # a kill() returned (F2-7). The protocol itself needs no lock.
        self.kill_lock = threading.Lock()
        self.consumer: _Consumer | None = None
        self.consumed = False  # a consumer was attached once
        self.pending: OutboxCounts | None = None  # a report the engine has not taken
        self.posted: OutboxCounts | None = None  # the counts reported last
        # join(), iteration, wait_running() or assert_complete() raised the
        # outcome (design A.23).
        self.seen = False
        # How the AsyncLogStream was dropped, for the unseen-outcome ERROR.
        self.orphaned: str | None = None
        # The engine and dispatcher tasks, and the task a callback runs in
        # while it runs (`_call`), for the F3-7 check.
        self.engine_task: weakref.ref[asyncio.Task[Any]] | None = None
        self.dispatcher_task: weakref.ref[asyncio.Task[Any]] | None = None
        self.calling: asyncio.Task[Any] | None = None
        # Wake-ups, on the loop only.
        self._engine_waiter: asyncio.Future[None] | None = None
        self._consumer_waiter: asyncio.Future[None] | None = None
        self._waiters: list[asyncio.Future[None]] = []
        self._reap_due = False  # Finished was carried out: reap the one-shots

    # -- the loop ---------------------------------------------------------------------

    def on_loop(self) -> bool:
        """Whether the caller runs on this run's (running) loop."""
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def _soon(self, callback: Callable[[], object]) -> bool:
        """Run ``callback`` on the loop from another thread; False once it is closed."""
        loop = self.loop
        if loop is None:
            return False
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:  # the loop is closed: nothing runs any more
            return False
        return True

    def _task(
        self,
        coro: Coroutine[Any, Any, None],
        name: str,
        *,
        context: contextvars.Context | None = None,
    ) -> asyncio.Task[None]:
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        task = loop.create_task(coro, name=name, context=context)
        _LIVE.add(task)
        task.add_done_callback(_LIVE.discard)
        task.add_done_callback(_retrieve)
        return task

    def _worker(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = self._task(coro, name)
        self.workers.add(task)
        task.add_done_callback(self.workers.discard)

    # -- control, from any thread -----------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop, *, push: bool) -> bool:
        """Start the engine (and the dispatcher) on ``loop``; False if started."""
        if self.started:
            return False
        self.started = True
        self.loop = loop
        self.data = asyncio.Queue(_DATA_SLOTS)
        if push:
            self.consumer = _Consumer(owner=None, push=True)
            self.consumed = True
        self.control.post(Start())
        # The engine and the dispatcher run the stream's user code; the tasks
        # they create inherit the mark (F3-7).
        context = contextvars.copy_context()
        context.run(_IN_CALLBACK.set, self)
        engine = self._task(self._engine(), "logpyt-engine", context=context)
        self.engine_task = weakref.ref(engine)
        if push:
            dispatcher = self._task(
                self._dispatch(), "logpyt-dispatcher", context=context
            )
            self.dispatcher_task = weakref.ref(dispatcher)
        return True

    def stop(self, flush: float | None, *, detach_own: bool = False) -> None:
        """Post a stop; detach the outbox when no consumer is attached (3.5)."""
        if self.loop is None:
            return  # D-6
        if self.on_loop():
            self._stop_now(flush, detach_own=detach_own)
        else:
            self._soon(functools.partial(self._stop_now, flush))

    def _stop_now(self, flush: float | None, *, detach_own: bool = False) -> None:
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
        """Set the kill flag and post the kill where the core acts (F2-7, D-6).

        From another thread the request reaches the loop later, but the flag
        is set before kill() returns: the consumer may go on first (a worker
        thread's result can wake it at once), and takes no item then. A step
        that was popping when the flag was set (`kill_lock`) began before
        kill() returned.
        """
        if self.loop is None:
            return
        if self.on_loop():
            self._kill_now(detach_own=detach_own)
        elif self._soon(self._kill_now) and self.started and not self.finished:
            with self.kill_lock:
                self.kill_flag = True

    def _kill_now(self, *, detach_own: bool = False) -> None:
        if detach_own:
            consumer = self.consumer
            if consumer is not None and _owned_here(consumer):
                self._detach(consumer)
        if not self.started or self.finished:
            return
        self.kill_flag = True
        self._post(KillRequest())

    def abandon(self, *, at_exit: bool) -> None:
        """The AsyncLogStream was dropped: kill a running run (design 3.4).

        Runs on the loop, or in place where the loop cannot act on it any
        more (`_abandon`), where only adb is killed and the outcome logged.
        ``at_exit``: its finalizer ran at interpreter exit, while the stream
        was still referenced; else it was garbage-collected. Either names the
        cause in the ERROR for an outcome nobody saw.
        """
        self.orphaned = (
            "was still referenced at interpreter exit"
            if at_exit
            else "was garbage-collected"
        )
        late: str | None = None
        if self.finished:
            late = self._unseen_outcome_message()
        elif self.started:
            if self.on_loop():
                self.kill_flag = True
                self._post(KillRequest())
            else:
                self.kill_children()
        if late is not None:
            _log.error("%s", late)

    def kill_children(self) -> None:
        """Kill every adb child not reaped yet at once: logcat and one-shot.

        It does not need the loop, so it also works at interpreter shutdown
        and once the loop is closed; the copies are made in one step each.
        """
        for proc in [*self.procs.values(), *self.oneshots]:
            _send(proc, kill=True)

    def wait_spawns(self) -> None:
        """Interpreter exit, in the main thread, while the loop runs in another
        one: let the adb spawns in progress there finish, for at most
        ``_REAP_WAIT``, so their children are tracked (design A.23).

        ``closing`` is set first, so no spawn begins any more, and one that
        finishes kills its own child. Without this wait, finalization could
        freeze the loop's thread after the child was created but before it
        was tracked, and the child would outlive the interpreter.
        """
        deadline = time.monotonic() + _REAP_WAIT
        while self.spawning and time.monotonic() < deadline:
            time.sleep(_REAP_POLL)

    def attach(self) -> _Consumer:
        """Attach a pull iterator, or raise UsageError (design 2.3, 4.2)."""
        if not self.started:
            msg = (
                "AsyncLogStream iterated before start(): use `async with "
                "AsyncLogStream(...) as stream: async for entry in stream: ...`, "
                "or call stream.start() first"
            )
            raise UsageError(msg)
        if not self.on_loop():
            msg = (
                "AsyncLogStream iterated outside the event loop that started it; "
                "iterate it in a coroutine on that loop"
            )
            raise UsageError(msg)
        if self.consumed:
            msg = (
                "AsyncLogStream has one iterator per run, and it was already "
                "created; keep that iterator, or use stream.clone() for a new run"
            )
            raise UsageError(msg)
        if self.outbox.detached:
            msg = (
                "AsyncLogStream iterated after stop() without an attached "
                "iterator: stop() discarded the rest (design 3.5); iterate "
                "first, then stop()"
            )
            raise UsageError(msg)
        task = _current_task()
        consumer = _Consumer(
            owner=None if task is None else weakref.ref(task), push=False
        )
        self.consumer = consumer
        self.consumed = True
        return consumer

    def detach(self, consumer: _Consumer) -> None:
        """A pull iterator was closed or dropped; from any thread."""
        if self.on_loop():
            self._detach(consumer)
        else:
            self._soon(functools.partial(self._detach, consumer))

    # -- waits, from user code ---------------------------------------------------------

    def check_wait(self, what: str, *, iterator: bool) -> None:
        """UsageError for a wait from the stream's own user code (F3-7, design 3.5).

        The stream's own user code runs in the engine and the dispatcher
        task, and in the task a callback runs in while it runs (`calling`):
        a wait there raises also after the run's end, as in the thread
        driver. A task created inside that code inherits the mark
        (`_IN_CALLBACK`) with its context; its waits raise only while the run
        lives, because the callback may await that task. Once the run has
        ended there is nothing to deadlock on.
        """
        task = _current_task()
        own = task is not None and (
            task is self.calling
            or _is(task, self.engine_task)
            or _is(task, self.dispatcher_task)
        )
        if own or (_IN_CALLBACK.get() is self and not self.ended):
            msg = (
                f"{what} called from a stream callback; call stream.stop() and "
                "return instead"
            )
            raise UsageError(msg)
        consumer = self.consumer
        if iterator and consumer is not None and _owned_here(consumer):
            msg = (
                f"{what} would wait for this task's own iterator; iterate to the "
                "end, break, or call it.aclose() first"
            )
            raise UsageError(msg)

    def check_loop(self, what: str) -> None:
        """UsageError for a wait awaited on another loop than the run's."""
        if not self.on_loop():
            msg = (
                f"{what} awaited outside the event loop that started the stream; "
                "await it in a coroutine on that loop"
            )
            raise UsageError(msg)

    async def _wait_until(
        self, done: Callable[[], bool], timeout: float | None
    ) -> bool:
        """Wait until ``done()``; False once ``timeout`` seconds passed first.

        Woken by `_notify_waiters`; cancelling the caller cancels only this
        wait (design 5.4).
        """
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        while not done():
            if deadline is not None and loop.time() >= deadline:
                return False
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append(waiter)
            timer = None if deadline is None else loop.call_at(deadline, _wake, waiter)
            try:
                await waiter
            finally:
                if timer is not None:
                    timer.cancel()
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
        return True

    async def wait_ended(self, timeout: float | None, what: str) -> None:
        """Wait until the engine has ended; TimeoutError on expiry."""
        if not await self._wait_until(lambda: self.ended, timeout):
            msg = f"{what} timed out after {timeout:g} s; the stream keeps running"
            raise TimeoutError(msg)

    async def wait_running(self, timeout: float | None) -> None:
        """Wait until the first RUNNING or the run's conclusion (design A.21)."""

        def done() -> bool:
            return self.reached_running or self.concluded or self.crash is not None

        if not await self._wait_until(done, timeout):
            msg = (
                f"wait_running() timed out after {timeout:g} s; the stream is "
                f"{self.state} and keeps running"
            )
            raise TimeoutError(msg)
        if self.reached_running and self.crash is None:
            return
        error = self.take_error(unseen_only=False)
        if error is not None:
            raise error

    async def wait_while_progressing(self, close_timeout: float) -> bool:
        """Wait until the end while the consumer takes items; False once it took
        nothing for ``close_timeout`` (design 3.4).
        """
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        last = self.outbox.counts()
        since = loop.time()
        while not self.ended:
            now = loop.time()
            counts = self.outbox.counts()
            if (counts.taken_entries, counts.handled) != (
                last.taken_entries,
                last.handled,
            ):
                last, since = counts, now
            elif now - since >= close_timeout:
                return False
            await self._wait_until(
                lambda: self.ended, min(_SLICE, close_timeout - (now - since))
            )
        return True

    def take_error(self, *, unseen_only: bool) -> BaseException | None:
        """The error a finished run raises (its FAILED outcome), marked seen."""
        if self.crash is not None:
            return self.crash
        if self.outcome is None or (unseen_only and self.seen):
            return None
        self.seen = True
        return self.outcome

    # -- the consumer side: one delivery iterator for pull and push -----------------

    async def _dispatch(self) -> None:
        """Push mode: ``for item in <the delivery iterator>: on_item(item)`` (A-4)."""
        # An eager task factory would run this step inside start(), which
        # runs no user code (design 3.3): yield first.
        await asyncio.sleep(0)
        consumer = self.consumer
        on_item = self.on_item
        assert consumer is not None  # noqa: S101  (set by start)
        assert on_item is not None  # noqa: S101  (push mode has one)
        try:
            while (item := await self.deliver_next(consumer)) is not None:
                if not await self._call(consumer, "on_item", on_item, item):
                    return
        except asyncio.CancelledError:
            raise  # a real cancellation
        except (KeyboardInterrupt, SystemExit) as exc:
            # Ctrl+C or sys.exit() on the loop's thread, a callback's too: not
            # a failure; the run ends with it, and it leaves the loop.
            self._interrupted(exc)
            raise
        except BaseException as exc:  # noqa: BLE001  (_call takes the callbacks')
            self._worker_failed(exc)
        finally:
            self.detach(consumer)

    async def deliver_next(self, consumer: _Consumer) -> Item | None:
        """The next item for ``consumer``, ``on_event`` run for the events before it.

        None at the consumer's end: the outbox ended, it was detached, or a
        callback raised (``on_event`` here, ``on_item`` before).
        """
        if consumer.busy:
            msg = (
                "an AsyncLogStream iterator is awaited by two tasks at once; "
                "await its __anext__() from one task at a time"
            )
            raise UsageError(msg)
        consumer.busy = True
        try:
            while True:
                record = await self._step(consumer)
                if record is None:
                    return None
                if isinstance(record, LogEntry | LogGroup):
                    return record
                on_event = self.on_event
                if on_event is None:
                    consumer.owe_done = True
                elif not await self._call(consumer, "on_event", on_event, record):
                    return None
        finally:
            consumer.busy = False

    async def _step(self, consumer: _Consumer) -> Record | None:
        """Settle, pop and report in one task step (protocol module docs).

        The once-per-batch yield (X-1) comes before the settle, so a
        cancellation there loses nothing: the record in hand is settled by
        the next step.
        """
        if consumer.taken >= _BATCH:
            consumer.taken = 0
            await asyncio.sleep(0)
        outbox = self.outbox
        while True:
            if consumer.failed:
                await self._await_close(consumer)
                return None
            if consumer.owe_done:
                consumer.owe_done = False
                outbox.done()
            if not consumer.attached:
                return None
            with self.kill_lock:  # an off-loop kill() sets the flag under it
                skip = self.kill_flag and outbox.entries > 0 and not outbox.closed
                record = None if skip else outbox.pop()
            if skip:
                # F2-7: no item is taken after kill(). The core discards
                # them (DiscardUndelivered, counted in undelivered: 3.4),
                # and the events behind them still come.
                self._report(empty=True)
                await self._consumer_wait(consumer)
                continue
            self._report(empty=record is None)
            if record is None:
                if outbox.ended:
                    consumer.reached_end = True
                    self._detach(consumer)
                    return None
                await self._consumer_wait(consumer)
                continue
            consumer.taken += 1
            return record

    async def _call(
        self,
        consumer: _Consumer,
        stage: Literal["on_item", "on_event"],
        callback: Callable[[Any], object],
        record: Record,
    ) -> bool:
        """Run one callback in the consumer context and await what it returns;
        False if it failed (design 2.4, F3-6).

        A ``CancelledError`` while the task is not being cancelled
        (``Task.cancelling() == 0``) is the callback's own failure. A real
        cancellation, Ctrl+C (``KeyboardInterrupt``) and ``sys.exit()``
        (``SystemExit``) propagate and leave the record to the next step; on
        the dispatcher the last two end the run (`_dispatch`). On the
        dispatcher any other ``BaseException`` is a callback failure, as in
        the thread driver (design A.23).
        """
        token = _IN_CALLBACK.set(self)
        calling, self.calling = self.calling, _current_task()
        try:
            result = callback(record)
            if result is not None and inspect.isawaitable(result):
                await result
        except asyncio.CancelledError as exc:
            task = _current_task()
            if task is not None and task.cancelling():
                consumer.owe_done = True  # interrupted: the next step settles it
                raise
            await self._fail(consumer, stage, exc)
            return False
        except Exception as exc:  # noqa: BLE001  (fail fast: CallbackError)
            await self._fail(consumer, stage, exc)
            return False
        except (KeyboardInterrupt, SystemExit):
            consumer.owe_done = True  # interrupted, not failed
            raise
        except BaseException as exc:
            if consumer.push:
                await self._fail(consumer, stage, exc)  # A.23
                return False
            consumer.owe_done = True  # interrupted, not failed
            raise
        finally:
            self.calling = calling
            _IN_CALLBACK.reset(token)
        consumer.owe_done = True
        return True

    async def _fail(
        self,
        consumer: _Consumer,
        stage: Literal["on_item", "on_event"],
        error: BaseException,
    ) -> None:
        """Post the failure; take nothing more; detach once the outbox is closed."""
        consumer.failed = True
        late = self._post(CallbackFailed(stage, error))
        if late is not None:
            _log.error("%s", late, exc_info=error)
        await self._await_close(consumer)

    async def _await_close(self, consumer: _Consumer) -> None:
        """After a failure: wait until the outbox is closed, then detach."""
        while consumer.attached and not self.outbox.closed:
            await self._consumer_wait(consumer)
        consumer.reached_end = True
        self._detach(consumer)

    async def _consumer_wait(self, consumer: _Consumer) -> None:
        """Wait until the engine puts, discards or closes, or a detach."""
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        waiter: asyncio.Future[None] = loop.create_future()
        self._consumer_waiter = waiter
        consumer.taken = 0  # it suspends here
        try:
            await waiter
        finally:
            if self._consumer_waiter is waiter:
                self._consumer_waiter = None

    def _wake_consumer(self) -> None:
        waiter = self._consumer_waiter
        if waiter is not None:
            _wake(waiter)

    def _report(self, *, empty: bool) -> None:
        """The consumer side's report of the outbox counts (design A.21).

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
            self._notify_engine()

    def _detach(self, consumer: _Consumer) -> None:
        """`Outbox.detach` and `ConsumerDetached` in one task step."""
        if not consumer.attached:
            return
        consumer.attached = False
        if self.consumer is consumer:
            self.consumer = None
        self.outbox.detach()
        self._post(ConsumerDetached(self._counts()))
        self._wake_consumer()

    def _counts(self) -> OutboxCounts:
        counts = self.outbox.counts()
        self.posted = counts
        return counts

    # -- the mailbox ------------------------------------------------------------------

    def _post(self, event: Event) -> str | None:
        """Hand ``event`` to the engine; after its end, to the core.

        Returns the ERROR message of a callback failure that came after the
        engine ended (`StreamCore.late_failure`), for the caller to log.
        """
        if self.ended:
            return self._handle_late(event)
        self.control.post(event)
        self._notify_engine()
        return None

    def _handle_late(self, event: Event) -> str | None:
        """The engine has ended: the core is the consumer's (G2: accounting)."""
        if self.crash is not None or not isinstance(
            event, ConsumerProgress | ConsumerDetached | CallbackFailed
        ):
            return None
        late = (
            self.core.late_failure(event) if isinstance(event, CallbackFailed) else None
        )
        self.core.handle(event, _now(self.loop))
        self.stats = self.core.stats()
        return late

    def _notify_engine(self) -> None:
        waiter = self._engine_waiter
        if waiter is not None:
            _wake(waiter)

    def _notify_waiters(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            _wake(waiter)

    async def _take(self) -> Event | None:
        """The engine's next event (design 5.4); None once the run is over.

        The control inbox comes first, then data while intake is open, then a
        `Tick` at the deadline. The run is over once `Finished` was carried
        out and no consumer is attached any more, or once a task failed (the
        engine then ends it: `_crashed`).
        """
        loop = self.loop
        data = self.data
        assert loop is not None  # noqa: S101  (started)
        assert data is not None  # noqa: S101
        core = self.core
        while True:
            if self.failure is not None:
                return None
            pending = self.pending
            if pending is not None:
                self.pending = None
                self.control.post(ConsumerProgress(pending))
            event = self.control.take()
            if event is not None:
                return event
            if not data.empty() and core.intake_open:
                return data.get_nowait()  # a reader waiting for room is woken
            if self.finished and self.consumer is None:
                self._end()
                return None
            deadline = core.next_deadline()
            if deadline is not None and deadline <= loop.time():
                return _TICK
            waiter: asyncio.Future[None] = loop.create_future()
            self._engine_waiter = waiter
            timer = None if deadline is None else loop.call_at(deadline, _wake, waiter)
            try:
                await waiter
            finally:
                self._engine_waiter = None
                if timer is not None:
                    timer.cancel()

    def _end(self) -> None:
        """The engine is done: join() returns; readers drop their data."""
        self.ended = True
        self.data_closed = True
        data = self.data
        while data is not None and not data.empty():
            data.get_nowait()
        self._notify_waiters()
        self._wake_consumer()

    # -- the engine ---------------------------------------------------------------

    async def _engine(self) -> None:
        # An eager task factory would run this step inside start(), which
        # never waits for adb (design 2.3): yield first.
        await asyncio.sleep(0)
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        core = self.core
        interrupted = False
        try:
            while (event := await self._take()) is not None:
                if isinstance(event, CallbackFailed):
                    late = core.late_failure(event)
                    if late is not None:
                        _log.error("%s", late, exc_info=event.error)
                self._carry_out(core.handle(event, loop.time()))
                if self._reap_due:
                    self._reap_due = False
                    await self._reap_commands()
                if isinstance(event, Data):
                    await asyncio.sleep(0)  # X-1: one chunk per task step
            # A failed task logged its error (`_worker_failed`); an
            # interrupted one ended the run itself (`_interrupted`).
            if self.failure is not None and not self.ended:
                await self._crashed(self.failure)
        except asyncio.CancelledError as exc:
            # The loop shuts down (or someone cancelled the engine): kill adb
            # now, start nothing more (F3-2), and end every wait.
            self._abandoned(exc)
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            # Ctrl+C or sys.exit() on the loop's thread (in where or group_by,
            # say): not a bug, and not swallowed. It leaves the loop without
            # an await; the engine's end goes on in a task of its own.
            interrupted = True
            self._interrupted(exc)
            self._close_later()
            raise
        except BaseException as exc:  # noqa: BLE001  (a bug: end the run visibly)
            _log.error(
                "the AsyncLogStream engine failed; the run is abandoned", exc_info=exc
            )
            await self._crashed(exc)
        finally:
            if not interrupted:  # else `_close_later` does it
                await self._close_tasks()

    def _carry_out(self, commands: Iterable[Command]) -> None:
        """Carry out one ``handle()`` batch in order, then report and publish.

        The `Deliver` that ends the run (its last record is the terminal
        `StateChange`; `Finished` comes right after it) is put into the
        outbox together with the outcome (`_finished`), so whoever sees the
        terminal change also sees the terminal state and the outcome.
        """
        changed = False  # the batch changed the outbox counts
        final: tuple[Record, ...] = ()  # the Deliver that ends the run
        for command in commands:
            match command:
                case Deliver(records=records) if _ends_run(records):
                    final = records
                case Deliver(records=records):
                    changed |= self._deliver(records)
                case DiscardUndelivered():
                    before = self.outbox.counts()
                    self.outbox.discard()
                    changed |= self.outbox.counts() is not before
                    self._wake_consumer()  # a consumer waits under the kill flag
                case Diagnostic(level=level, message=message, logger=name):
                    logging.getLogger(name).log(level, "%s", message)
                case Spawn(connection=connection, argv=argv):
                    self._worker(
                        self._spawn(connection, argv), f"logpyt-spawn-{connection}"
                    )
                case Terminate(connection=connection):
                    self._signal(connection, kill=False)
                case Kill(connection=connection):
                    self._signal(connection, kill=True)
                case RunCommand():
                    task = self._task(
                        self._run_command(command), f"logpyt-{command.kind}"
                    )
                    self.commands.add(task)
                    task.add_done_callback(self.commands.discard)
                case Finished(outcome=outcome):
                    self._finished(outcome, final)
                    final = ()
                    changed = False
                case _:
                    assert_never(command)
        assert not final  # noqa: S101  (the core emits Finished right after it)
        if changed:
            self.control.post(ConsumerProgress(self._counts()))
        self._publish()

    def _deliver(self, records: tuple[Record, ...]) -> bool:
        """`Deliver`: append to the outbox; whether that changed its counts.

        The state is published first (design 3.6 item 5: it may run ahead
        of the events, never behind them), so a consumer that takes one of
        these records, and a `wait_running` it wakes, read the state that
        record led to or a later one.
        """
        self._publish(records)
        before = self.outbox.counts()
        self._put_records(records)
        return self.outbox.counts() is not before

    def _put_records(self, records: tuple[Record, ...]) -> None:
        """Append to the outbox and wake the consumer."""
        self.outbox.put(records)
        if not self.reached_running and any(
            isinstance(record, StateChange) and record.new is StreamState.RUNNING
            for record in records
        ):
            self.reached_running = True
            self._notify_waiters()
        self._wake_consumer()

    def _finished(self, outcome: StreamError | None, final: tuple[Record, ...]) -> None:
        """`Finished`: the run's last records, the engine side's last report,
        then the end (protocol), in one task step; the engine then kills and
        reaps the one-shot adb children still running (`_reap_commands`).

        The core is terminal, so ``handle()`` here only updates its
        accounting (G2) and runs no user code. A consumer therefore takes the
        terminal `StateChange` only once ``state``, ``stats`` and ``outcome``
        are final, and ``assert_complete()`` never sees a terminal state
        without its outcome.
        """
        if final:
            self._put_records(final)
        self.core.handle(ConsumerProgress(self._counts()), _now(self.loop))
        self.outbox.close()
        self.finished = True
        self.outcome = outcome
        self._publish()
        late = self._unseen_outcome_message() if self.orphaned else None
        self.reaping = True
        self._reap_due = bool(self.commands)
        self._wake_consumer()
        self._notify_waiters()
        if late is not None:
            _log.error("%s", late)

    async def _reap_commands(self) -> None:
        """Kill and reap the one-shot commands still running at the end (A.23).

        Their results would be ignored (G2, G5). Cancelling the runner kills
        and reaps its child; this waits at most ``_REAP_WAIT`` for that.
        """
        tasks = list(self.commands)
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks, timeout=_REAP_WAIT)

    def _publish(self, records: tuple[Record, ...] = ()) -> None:
        """Publish the core's state, stats and intake (design 3.6).

        A terminal state is published only once ``outcome`` is set
        (`_finished`). Before that, in a batch that ends the run, ``state``
        is the state the last change in ``records`` led to, and
        ``concluded`` keeps its value, so ``wait_running()`` never returns
        for a run whose outcome is not published yet.
        """
        core = self.core
        self.stats = core.stats()
        self.intake_open = core.intake_open
        state = core.state
        concluded = core.concluded
        if state in _TERMINAL and not self.finished:
            changes = [
                r.new
                for r in records
                if isinstance(r, StateChange) and r.new not in _TERMINAL
            ]
            state = changes[-1] if changes else self.state
            concluded = self.concluded
        if state is not self.state or concluded is not self.concluded:
            self.state = state
            self.concluded = concluded
            if state is StreamState.RUNNING:
                self.reached_running = True
            self._notify_waiters()

    def _unseen_outcome_message(self) -> str | None:
        """The ERROR for a FAILED outcome nobody saw (design 3.4)."""
        if self.outcome is None or self.seen:
            return None
        self.seen = True
        return (
            f"an AsyncLogStream {self.orphaned} and its run ended FAILED without "
            f"anyone seeing it: {type(self.outcome).__name__}: {self.outcome}"
        )

    async def _crashed(self, error: BaseException) -> None:
        """The engine or a task failed (a logpyt bug): end the run so no wait
        hangs (design A.23).

        adb is killed first and reaped (bounded by ``_REAP_WAIT``), so no adb
        child is left when join() returns; only then does every wait end.
        ``stats`` stays the snapshot the core published last, with ``failed``
        set: a crashed capture is never ``complete`` (design 4.4), while
        ``state`` is not changed (no driver invents a state, 5.2).
        """
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        self.reaping = True
        self.kill_children()
        tasks = [*self.workers, *self.commands]
        for task in tasks:
            task.cancel()  # a one-shot runner kills and reaps its child
        deadline = loop.time() + _REAP_WAIT
        try:
            if tasks:
                await asyncio.wait(tasks, timeout=_REAP_WAIT)
            while loop.time() < deadline and (
                any(proc.returncode is None for proc in self.procs.values())
                or self.oneshots
            ):
                await asyncio.sleep(_REAP_POLL)
        finally:
            self._fail_run(error)

    def _abandoned(self, error: BaseException) -> None:
        """The engine was cancelled: kill adb at once and end the run (F3-2).

        A run whose `Finished` was carried out keeps its outcome, state and
        stats, which are final (G4, design A.21): the engine was only waiting
        for its one-shot children to be reaped or for its consumer to detach.
        The consumer's reports it had not taken yet go to the core for the
        accounting, as after the end (`_handle_late`). Any other run ends
        like a crash, with ``error``.
        """
        self.reaping = True
        self.closing = True  # a spawn in flight kills its own child
        self.kill_children()
        if self.ended:
            return
        if not self.finished:
            self._fail_run(error)
            return
        pending, self.pending = self.pending, None
        if pending is not None:
            self.control.post(ConsumerProgress(pending))
        while (event := self.control.take()) is not None:
            late = self._handle_late(event)
            if late is not None and isinstance(event, CallbackFailed):
                _log.error("%s", late, exc_info=event.error)
        self._end()

    def _interrupted(self, error: BaseException) -> None:
        """Ctrl+C (``KeyboardInterrupt``) or ``sys.exit()`` (``SystemExit``) in
        a task of the run, on the loop's thread, a push callback's included:
        it leaves the loop at once, as asyncio raises both out of it, and it
        is neither a logpyt bug nor a callback failure, so nothing is logged.

        adb is killed now, nothing is started any more, the run's other I/O
        tasks are cancelled, and every wait ends: as for a cancelled engine
        (`_abandoned`), with ``error``. Nothing here awaits. The engine, once
        the loop runs it again, takes no event and only cleans up.
        """
        current = _current_task()
        for task in [*self.workers, *self.commands]:
            if task is not current:
                task.cancel()
        self._abandoned(error)
        if self.failure is None:
            self.failure = error
        self._notify_engine()

    def _fail_run(self, error: BaseException) -> None:
        """End a crashed run: every wait ends and raises ``error``.

        The first end of the run is kept, as in `_abandoned`: a push
        callback's interrupt (`_interrupted`) that ended the run while a
        crash reaped adb stays what join() raises once the crash's reap is
        over, so join() raises the same object every time (design 4).
        """
        if self.ended:
            return
        self.crash = error
        self.stats = replace(self.stats, failed=True)
        self.finished = True
        self.outbox.close()
        self._end()

    async def _close_tasks(self) -> None:
        """The engine's end: cancel and await the tasks still running (5.4).

        A reader of a retired connection whose pipe a grandchild holds ends
        here (design 5.10), and closes that pipe. A cancelled one-shot runner
        reaps its child first. A logcat child the end killed (the engine was
        cancelled) is waited for, at most ``_REAP_WAIT``, until asyncio has
        reaped it, so its transport closes before the loop does.
        """
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        tasks = [*self.workers, *self.commands]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        deadline = loop.time() + _REAP_WAIT
        while loop.time() < deadline and any(
            proc.returncode is None for proc in self.procs.values()
        ):
            await asyncio.sleep(_REAP_POLL)

    def _close_later(self) -> None:
        """The engine's end (`_close_tasks`) after an interrupt in the engine,
        which leaves the loop without an await: in a task of its own.

        Nothing else awaits the logcat child that `_interrupted` killed, so
        without it the child would stay unreaped once the loop closes (on
        POSIX a zombie, its pidfd and transport open). The task runs its
        first step at once (``eager_start``), so it is inside its ``try``
        before the loop's teardown cancels what is left, as ``asyncio.run``
        does next; a task cancelled before its first step never runs. It is
        made with the `asyncio.Task` constructor, so it bypasses a custom
        task factory, which cannot be asked for an eager start: the task is
        the library's own and runs no user code.
        """
        loop = self.loop
        assert loop is not None  # noqa: S101  (started)
        task = asyncio.Task(
            self._close_after_interrupt(),
            loop=loop,
            name="logpyt-close",
            eager_start=True,
        )
        if not task.done():
            _LIVE.add(task)
            task.add_done_callback(_LIVE.discard)
        task.add_done_callback(_retrieve)

    async def _close_after_interrupt(self) -> None:
        """`_close_tasks` for `_close_later`; cancelled (the loop's teardown),
        it finishes first, bounded as it is, then propagates the cancellation.
        """
        try:
            await self._close_tasks()
        except asyncio.CancelledError:
            await self._close_tasks()
            raise

    def _worker_failed(self, error: BaseException) -> None:
        """A task raised (a logpyt bug): the engine ends the run.

        The engine takes no event after this (`_take`) and crashes the run
        with ``error`` (`_crashed`), so join(), iteration and
        assert_complete() raise it, as for an engine crash (design A.23).
        It is logged first, so the ERROR is out before any wait ends.
        """
        task = _current_task()
        name = "?" if task is None else task.get_name()
        if self.failure is None and not self.ended:
            _log.error(
                "the AsyncLogStream task %s failed; the run is abandoned",
                name,
                exc_info=error,
            )
            self.failure = error
            self._notify_engine()
        else:
            _log.error(
                "the AsyncLogStream task %s failed after the run ended or was "
                "abandoned",
                name,
                exc_info=error,
            )

    # -- tasks ------------------------------------------------------------------------

    async def _spawn(self, connection: int, argv: tuple[str, ...]) -> None:
        """`Spawn`: start adb, report it, start its readers and its exit watcher."""
        try:
            # Counted before ``closing`` is read: interpreter exit sets it,
            # then waits for the spawns counted (`_abandon`).
            self.spawning += 1
            try:
                if self.closing:
                    msg = "the interpreter is exiting: adb is not started"
                    raise OSError(msg)
                # Through the module, like resolve_adb: one patch point for tests.
                proc = await adb._async_spawn_adb(argv)  # noqa: SLF001
                self.procs[connection] = proc
            except OSError as exc:
                self._post(SpawnFailed(connection, exc))
                return
            finally:
                self.spawning -= 1
            # Posted before any reader runs, so the core sees it before the data.
            self._post(Spawned(connection, proc.pid))
            if self.closing:
                _send(proc, kill=True)
            pipes: tuple[tuple[Pipe, asyncio.StreamReader | None], ...] = (
                ("stdout", proc.stdout),
                ("stderr", proc.stderr),
            )
            for name, reader in pipes:
                assert reader is not None  # noqa: S101  (spawned with PIPE)
                self._worker(
                    self._read(connection, name, reader, proc),
                    f"logpyt-{name}-{connection}",
                )
            self._worker(self._wait_exit(connection, proc), f"logpyt-exit-{connection}")
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._interrupted(exc)
            raise
        except BaseException as exc:  # noqa: BLE001  (a bug: end the run visibly)
            self._worker_failed(exc)

    def _signal(self, connection: int, *, kill: bool) -> None:
        """`Terminate` or `Kill`: the child's pid only, never its group (5.11)."""
        proc = self.procs.get(connection)
        if proc is not None:
            _send(proc, kill=kill)

    async def _read(
        self,
        connection: int,
        pipe_name: Pipe,
        reader: asyncio.StreamReader,
        proc: _Process,
    ) -> None:
        """Read one pipe to EOF into the data inbox; close it at the end (X-5)."""
        try:
            while True:
                try:
                    chunk = await reader.read(_READ_SIZE)
                except (OSError, ValueError):
                    chunk = b""  # the pipe broke: that is its end
                if not chunk:
                    break
                if not await self._put(Data(connection, pipe_name, chunk)):
                    return
                # X-1: read() returns at once while data is buffered.
                await asyncio.sleep(0)
            await self._put(Eof(connection, pipe_name))
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._interrupted(exc)
            raise
        except BaseException as exc:  # noqa: BLE001  (a bug: end the run visibly)
            self._worker_failed(exc)
        finally:
            _close_pipe(proc, 1 if pipe_name == "stdout" else 2)

    async def _put(self, event: Data | Eof) -> bool:
        """Append to the data inbox, waiting while it is full; False once closed."""
        data = self.data
        assert data is not None  # noqa: S101  (started)
        if self.data_closed:
            return False
        await data.put(event)
        self._notify_engine()
        return True

    async def _wait_exit(self, connection: int, proc: _Process) -> None:
        """Report adb's exit, told by ``returncode`` (design 5.1, 5.4)."""
        try:
            while (returncode := proc.returncode) is None:
                await asyncio.sleep(_EXIT_POLL)
            self.procs.pop(connection, None)
            self._post(Exited(connection, returncode))
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._interrupted(exc)
            raise
        except BaseException as exc:  # noqa: BLE001  (a bug: end the run visibly)
            self._worker_failed(exc)

    async def _run_command(self, command: RunCommand) -> None:
        """`RunCommand`: the one-shot runner; its result or error goes back.

        Its adb child is in ``oneshots`` from its spawn until the runner has
        reaped it, so interpreter exit can kill it; one spawned after the
        run's end is killed at once. Cancelled, the runner kills and reaps it.
        """
        spawned: list[_Process] = []

        async def spawn(argv: Sequence[str]) -> _Process:
            self.spawning += 1  # before ``closing`` is read, as in `_spawn`
            try:
                if self.closing:
                    msg = "the interpreter is exiting: adb is not started"
                    raise OSError(msg)
                proc = await adb._async_spawn_adb(argv)  # noqa: SLF001
                spawned.append(proc)
                self.oneshots.add(proc)
            finally:
                self.spawning -= 1
            if self.reaping or self.closing:
                _send(proc, kill=True)
            return proc

        result: Any
        try:
            try:
                result = await adb._async_run_adb(  # noqa: SLF001
                    command.argv, timeout=command.timeout, spawn=spawn
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  (the core reads the error)
                result = exc
            finally:
                self.oneshots.difference_update(spawned)
            self._post(CommandDone(command.request, result))
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._interrupted(exc)  # _async_run_adb killed and reaped its child
            raise
        except BaseException as exc:  # noqa: BLE001  (a bug: end the run visibly)
            self._worker_failed(exc)


def _wake(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


def _retrieve(task: asyncio.Task[Any]) -> None:
    """Done-callback of a run's task: mark its exception retrieved.

    A task of a run ends with an exception only for ``KeyboardInterrupt``
    and ``SystemExit`` (`_Run._interrupted`, a push callback's included),
    which asyncio raises out of the loop as it ends the task; asyncio would
    otherwise report it once more, as 'Task exception was never retrieved',
    when the task is collected. Everything else is caught and ends the run
    visibly.
    """
    if not task.cancelled():
        task.exception()


def _now(loop: asyncio.AbstractEventLoop | None) -> float:
    assert loop is not None  # noqa: S101  (started)
    return loop.time()


def _current_task() -> asyncio.Task[Any] | None:
    """The running task, or None outside one (and without a running loop)."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _is(task: asyncio.Task[Any], ref: weakref.ref[asyncio.Task[Any]] | None) -> bool:
    return ref is not None and ref() is task


def _owned_here(consumer: _Consumer) -> bool:
    """Whether ``consumer`` is a pull iterator this task created (design 3.5)."""
    if consumer.push or consumer.owner is None:
        return False
    task = _current_task()
    return task is not None and consumer.owner() is task


def _send(proc: _Process, *, kill: bool) -> None:
    """Terminate or kill ``proc`` (its pid only, design 5.11) unless it has exited.

    On POSIX the signal goes to the pid with ``os.kill``: asyncio's
    ``Process.kill`` polls the child first (``Popen.send_signal``), which can
    reap it before asyncio's child watcher and make that report exit status
    255. Until the watcher has set ``returncode`` the child is not reaped by
    asyncio, so its pid is still ours. On Windows terminate is a kill (5.11).
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            if kill:
                proc.kill()
            else:
                proc.terminate()
        else:
            os.kill(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)


def _close_pipe(proc: _Process, fd: int) -> None:
    """Close one pipe of an adb child (X-5); its EOF has usually closed it already.

    asyncio has no public way to reach a child's pipes: `asyncio.subprocess
    .Process` keeps its transport as ``_transport`` (CPython 3.12 to 3.14),
    whose ``get_pipe_transport`` is public. Closing the read pipe of a
    retired connection also lets asyncio close the process transport once
    the child has exited.
    """
    transport = getattr(proc, "_transport", None)
    if not isinstance(transport, asyncio.SubprocessTransport):
        return
    pipe = transport.get_pipe_transport(fd)
    if pipe is not None:
        pipe.close()


def _abandon(run: _Run) -> None:
    """`weakref.finalize` of a started `AsyncLogStream`: kill its run (design 3.4).

    It may run in any thread, inside any step of the loop (a cyclic
    collection), so ``abandon`` is posted with ``loop.call_soon_threadsafe``
    while the loop is not closed. There the kill goes through the core: on a
    running loop at once, and on a stopped one (between two
    ``run_until_complete`` calls) as the first thing the loop runs when it
    runs again, before it reads the end of the adb killed here, so a run
    that reconnects starts nothing more and ends KILLED. Where the loop may
    not run it soon (it is stopped, or the interpreter is exiting) adb is
    killed here at once too. Once the loop is closed, and for a finished run
    whose loop does not run, ``abandon`` runs in place: it kills adb and
    logs an outcome nobody saw. A run that ended and whose outcome was seen
    (or is None) needs nothing.

    At interpreter exit (the main thread has ended: weakref.finalize runs at
    exit, for a stream still referenced) no adb child is started any more,
    and every adb child of the run is killed, one-shot commands' included,
    also for a run that ended. When the loop runs in another thread, which
    still runs while atexit handlers do, a spawn in progress there is let
    finish first (`_Run.wait_spawns`), so its child is tracked, as the
    thread driver does (design A.23).
    """
    at_exit = not threading.main_thread().is_alive()
    needed = not (run.finished and (run.outcome is None or run.seen))
    if not (needed or at_exit):
        return
    loop = run.loop
    running = loop is not None and loop.is_running()
    if at_exit:
        run.closing = True
    # At exit only a running loop can still act on the post. For a finished
    # run ``abandon`` only logs, which needs no loop: it is posted only to a
    # loop that runs, and not at exit.
    post = needed and (
        (running and not run.finished) if at_exit else (running or not run.finished)
    )
    abandon = functools.partial(run.abandon, at_exit=at_exit)
    posted = post and run._soon(abandon)  # noqa: SLF001  (the run's own helper)
    if at_exit and running and threading.current_thread() is threading.main_thread():
        run.wait_spawns()
    if at_exit or not running or not posted:
        run.kill_children()  # at exit, of an ended run too (a one-shot child)
    if needed and not posted:
        abandon()


def _keep_context(error: BaseException, handled: BaseException | None) -> None:
    """Chain ``handled`` to ``error`` as its ``__context__`` (with-exit, 3.4).

    Python gives an exception raised while another one is handled that one
    as its ``__context__``, as for Ctrl+C in the thread driver's exit. A
    cancellation that asyncio throws into a wait of the exit is chained only
    to what a coroutine it passes through handles at that moment; once the
    exit awaited its kill, the re-raised cancellation passes no such frame
    any more and has none (observed on CPython 3.14). This sets it whatever
    the version does. A ``__context__`` it has is kept, and no cycle is made.
    """
    if handled is None or error.__context__ is not None:
        return
    seen: set[int] = set()
    link: BaseException | None = handled
    while link is not None and id(link) not in seen:
        if link is error:
            return  # it would be a cycle
        seen.add(id(link))
        link = link.__context__
    error.__context__ = handled


async def _kill_and_wait(run: _Run, *, detach_own: bool = False) -> None:
    """with-exit's kill: ``kill()``, then wait at most 1 s for the end (3.4).

    The kill is posted before the wait, so a second cancellation there only
    cuts the wait short.
    """
    run.kill(detach_own=detach_own)
    with contextlib.suppress(TimeoutError):
        await run.wait_ended(_KILL_WAIT, "the with-block exit")


class _PullIterator[T]:
    """The pull iterator of a stream (design 3.5, A-21): a class, not an async
    generator.

    It keeps its stream alive. ``aclose()``, and ``__del__`` when a ``break``
    drops the last reference, detach it at once; the rest is discarded and
    counted.
    """

    __slots__ = ("_consumer", "_done", "_handed", "_run", "_stream")

    def __init__(
        self, stream: AsyncLogStream[T], run: _Run, consumer: _Consumer
    ) -> None:
        self._stream = stream
        self._run = run
        self._consumer = consumer
        self._done = False
        self._handed = False  # an item was returned; asking for the next settles it

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> T:
        if self._done:
            raise StopAsyncIteration
        consumer = self._consumer
        if self._handed:
            self._handed = False
            consumer.owe_done = True
        item = await self._run.deliver_next(consumer)
        if item is not None:
            self._handed = True
            return cast("T", item)
        self._done = True
        if consumer.reached_end:
            # A FAILED run raises its outcome once, after the last item (4.2).
            error = self._run.take_error(unseen_only=False)
            if error is not None:
                raise error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        """Detach from the stream; what it did not take is discarded and counted."""
        self._done = True
        self._run.detach(self._consumer)

    def __del__(self) -> None:
        with contextlib.suppress(AttributeError):
            self._done = True
            self._run.detach(self._consumer)


class AsyncLogStream[T]:
    """A live ``adb logcat`` stream run by asyncio tasks (design 2.3).

    ``T`` is `LogEntry`, or `LogGroup` when ``group_by`` is given. A stream runs
    once: iterate it with ``async for`` (pull), or pass ``on_item`` (push, one
    dispatcher task); ``on_event`` receives the `StateChange` and `Gap`
    events in order with the items, in the consumer context. The callbacks
    may be plain or async functions: what they return is awaited when it is
    awaitable. ``where`` and ``group_by`` run in the engine task, on the
    loop. Any of them raising an `Exception` ends the run with
    `CallbackError`. ``KeyboardInterrupt`` and ``SystemExit`` are no
    failure: raised in the engine or by a push callback, one kills adb and
    leaves the event loop; a run that had not finished ends with it
    (``join()`` raises it), and one that had keeps its outcome. Raised by
    ``on_event`` in pull mode, it propagates from the iteration, which can
    go on.

    The stream lives on the event loop that runs ``start()`` (``async with``
    starts it). ``stop()`` and ``kill()`` may be called from any thread;
    ``state``, ``stats`` and ``outcome`` may be read from any thread.

    Args:
        config: The configuration; None uses ``StreamConfig()``.
        where: The condition each entry must match; None delivers every entry.
        group_by: Deliver `LogGroup` items instead of entries.
        on_item: Push mode: called with each item on the dispatcher task.
        on_event: Called with each event in the consumer context.

    Raises:
        TypeError: An argument has the wrong type.
        ValueError: ``group_by`` cannot fit the outbox, or ``where.packages()``
            names something that is not a package name.
        FileNotFoundError: ``config.adb_path`` is None and no adb is found.

    """

    @overload
    def __init__(
        self: AsyncLogStream[LogEntry],
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
        self: AsyncLogStream[LogGroup],
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
        config, where = _checked_arguments(config, where, group_by)
        _check_callable("on_item", on_item)
        _check_callable("on_event", on_event)
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
        """An immutable snapshot of the run's counters (design 2.5, 4.4).

        After a logpyt bug ended the run (join() raises it), ``failed`` is set,
        so ``complete`` is False.
        """
        return self._run.stats

    @property
    def outcome(self) -> StreamError | None:
        """The latched error of a run that ended FAILED; None otherwise."""
        return self._run.outcome

    # -- control -----------------------------------------------------------------------

    def start(self) -> None:
        """Start the run on the running loop and return at once (design 3.3).

        It never runs user code and never waits for adb.

        Raises:
            UsageError: No event loop runs in this thread, the loop is a
                selector loop on Windows, or the stream was started before
                (use `clone`).

        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            msg = (
                "AsyncLogStream.start() needs a running event loop: call it in a "
                "coroutine, or use `async with AsyncLogStream(...) as stream:`"
            )
            raise UsageError(msg) from None
        if sys.platform == "win32" and isinstance(loop, asyncio.SelectorEventLoop):
            msg = (
                "AsyncLogStream.start() needs the proactor event loop on Windows "
                "(adb runs as a subprocess, which a selector loop cannot start); "
                "use asyncio.run() with the default event loop policy"
            )
            raise UsageError(msg)
        run = self._run
        if not run.start(loop, push=self._on_item is not None):
            state = run.state
            what = "a started" if state is StreamState.IDLE else f"a {state}"
            msg = (
                f"AsyncLogStream.start() called on {what} stream; a stream runs "
                "once - use stream.clone()"
            )
            raise UsageError(msg)
        weakref.finalize(self, _abandon, run)

    def stop(self, *, flush: float | None = None) -> None:
        """Ask the run to stop; from any thread, task or callback; idempotent.

        With ``flush``, a marker is written to the device log and the stream
        reads until it, for at most ``flush`` seconds of open intake (design
        2.3). Before ``start()``, after the end, and once the loop is closed
        it does nothing.

        Raises:
            UsageError: ``flush`` is given and ``logcat_args`` hides the marker.
            TypeError: ``flush`` is not a number.

        """
        self._run.stop(_checked_flush(flush, self._flush_hidden_by))

    def kill(self) -> None:
        """Kill adb at once and discard undelivered items; idempotent (design 3.4)."""
        self._run.kill()

    async def join(self, timeout: float | None = None) -> None:
        """Wait for the end of delivery; raise the outcome of a FAILED run.

        Returns once the run ended and its consumer finished (or detached).
        Cancelling the caller cancels only this wait.

        A task created inside a callback inherits the callback's context, and
        with it the mark that makes this raise UsageError while the run goes
        on, because the callback may await that task (F3-7). Once the run has
        ended there is nothing to deadlock on, and such a task may await it.
        To wait for the run from such a task while it goes on, create it with
        an empty context:
        ``asyncio.create_task(coro, context=contextvars.Context())``.

        Raises:
            StreamError: The run ended FAILED (the same object every time).
            KeyboardInterrupt: Ctrl+C in a task of the run ended it (as
                ``SystemExit`` from ``sys.exit()`` there does).
            TimeoutError: ``timeout`` seconds passed; the stream keeps running.
            UsageError: Awaited from the stream's own callbacks (also after
                the end), from a task created inside them while the run goes
                on, from the task that owns an attached iterator, before
                ``start()``, or on another event loop while the run goes on.

        """
        run = self._run
        run.check_wait("join()", iterator=True)
        if not run.started:
            msg = (
                "join() called before start(); start the stream, or use "
                "`async with stream:`"
            )
            raise UsageError(msg)
        if not run.ended:
            run.check_loop("join()")
            await run.wait_ended(timeout, "join()")
        error = run.take_error(unseen_only=False)
        if error is not None:
            raise error

    async def wait_running(self, timeout: float | None = None) -> None:
        """Wait until a connection is RUNNING (adb printed), or the run concluded.

        A task created inside a callback may await it once the run has
        ended, as `join` explains.

        Raises:
            StreamError: The run concluded FAILED before it was RUNNING.
            KeyboardInterrupt: Ctrl+C in a task of the run ended it (as
                ``SystemExit`` from ``sys.exit()`` there does).
            TimeoutError: ``timeout`` seconds passed; the stream keeps running.
            UsageError: Awaited from the stream's own callbacks (also after
                the end), from a task created inside them while the run goes
                on, before ``start()``, or on another event loop while the
                run goes on.

        """
        run = self._run
        run.check_wait("wait_running()", iterator=False)
        if not run.started:
            msg = "wait_running() called before start(); start the stream first"
            raise UsageError(msg)
        if not run.ended:
            run.check_loop("wait_running()")
        await run.wait_running(timeout)

    def assert_complete(self) -> None:
        """Fail a CI test whose capture may miss device lines (design 4.4).

        Raises:
            UsageError: The run has not ended; await ``join()`` first.
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
        """A fresh IDLE stream: same config, adb path, where, group_by and callbacks.

        It may run on another event loop than this one.
        """
        twin = object.__new__(type(self))
        twin._setup(  # noqa: SLF001  (an AsyncLogStream sets up its own twin)
            self._config,
            self._adb_path,
            self._where,
            self._group_by,
            self._on_item,
            self._on_event,
        )
        return twin

    # -- iteration and async with ------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[T]:
        """Pull mode: the one iterator of this run (design 2.4, 3.5).

        Raises:
            UsageError: The stream has ``on_item``, was not started, is not on
                this event loop, or was iterated before (or stop() discarded
                what no iterator took).

        """
        if self._on_item is not None:
            msg = (
                "AsyncLogStream with on_item cannot be iterated: its dispatcher "
                "takes the items; leave out on_item to iterate"
            )
            raise UsageError(msg)
        return _PullIterator(self, self._run, self._run.attach())

    async def __aenter__(self) -> Self:
        """Start the stream and return it."""
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop and join; how depends on how the body ended (design 3.4).

        An iterator this task owns is detached first. A clean body waits
        without a cap and raises an outcome nobody saw. A body that raised an
        `Exception` waits while the consumer takes items, kills once it took
        nothing for ``close_timeout``, and keeps the body's exception, with the
        outcome added as a note. A `GeneratorExit` (an async generator around
        the block was closed early) takes the `Exception` path. Any other
        exception (a cancellation, ``KeyboardInterrupt``, ``SystemExit``)
        kills, then waits at most 1 s. An exception that interrupts the exit's
        own wait (a cancellation there) kills too before it propagates: adb
        does not outlive the block (design 9.2). It keeps as its
        ``__context__`` the exception the block was handling, as in the thread
        driver: the body's, or for a clean body the one an ``except`` clause
        around the block handles. A run that ended FAILED before the kill took
        effect is not killed (kill() after the end does nothing, design A.23):
        the items it left are still delivered.
        """
        run = self._run
        if exc is None:
            handled = sys.exception()  # an except clause around the block
            run.check_wait("the with-block exit", iterator=False)
            try:
                run.stop(None, detach_own=True)
                await run.wait_ended(None, "the with-block exit")
            except BaseException as interrupt:
                _keep_context(interrupt, handled)
                await _kill_and_wait(run, detach_own=True)
                raise
            error = run.take_error(unseen_only=True)
            if error is not None:
                raise error
            return
        if not isinstance(exc, Exception | GeneratorExit):
            await _kill_and_wait(run, detach_own=True)  # cancelled, Ctrl+C, ...
            return
        try:
            run.stop(None, detach_own=True)
            progressing = await run.wait_while_progressing(self._config.close_timeout)
        except BaseException as interrupt:
            _keep_context(interrupt, exc)
            await _kill_and_wait(run, detach_own=True)
            raise
        if not progressing:
            await _kill_and_wait(run)
        error = run.take_error(unseen_only=True)
        if error is not None and error is not exc:
            exc.add_note(
                f"logpyt: the stream ended {run.state} too: "
                f"{type(error).__name__}: {error}"
            )
            _log.error(
                "the AsyncLogStream failed while its with-block raised %s",
                type(exc).__name__,
                exc_info=error,
            )
