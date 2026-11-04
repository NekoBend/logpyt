"""The stream core of logpyt 3.0 (design section 5.2). Private.

Pure code: no I/O, threads, asyncio, subprocess or clock reads of its own. A
caller that needs the time passes it in: the monotonic time as an argument
(``now``), the wall-clock time through the ``ParseContext`` it gives the line
pipeline. For wall time the core calls that context's ``now()`` and nothing
else; the default context reads the host clock there (``logpyt._core.pipeline``).
"""

__all__: list[str] = []
