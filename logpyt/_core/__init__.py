"""The stream core of logpyt 3.0 (design section 5.2). Private.

Pure code: no I/O, threads, asyncio, subprocess or clock reads. A caller that
needs the time passes it in.
"""

__all__: list[str] = []
