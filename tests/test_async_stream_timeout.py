import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from logpyt.streams.async_stream import AsyncLogStream


@pytest.mark.asyncio
async def test_read_timeout_raises_exception():
    # Mock process
    mock_process = MagicMock()
    mock_process.stdout = AsyncMock()
    mock_process.stderr = AsyncMock()
    mock_process.returncode = 0
    mock_process.wait = AsyncMock(return_value=0)
    mock_process.terminate = MagicMock()

    # Mock readline to hang
    async def hanging_readline():
        await asyncio.sleep(1.0)
        return b"line\n"

    mock_process.stdout.readline.side_effect = hanging_readline
    mock_process.stderr.readline.return_value = b""

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process

        error_caught = asyncio.Event()
        caught_exception = None

        async def on_error(e):
            nonlocal caught_exception
            caught_exception = e
            error_caught.set()

        stream = AsyncLogStream(
            read_timeout=0.1,  # Short timeout
            on_error=on_error,
            auto_reconnect=False,
        )

        await stream.start()

        try:
            await asyncio.wait_for(error_caught.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        await stream.stop()
        await stream.join()

        assert caught_exception is not None
        assert isinstance(caught_exception, asyncio.TimeoutError)

        # Verify process was terminated
        mock_process.terminate.assert_called()
