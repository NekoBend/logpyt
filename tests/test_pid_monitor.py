from unittest.mock import Mock, patch

import pytest

from logpyt.streams import PidMonitor


class TestPidMonitor:
    @pytest.fixture
    def monitor(self):
        return PidMonitor(
            adb_path="adb", device_id="device123", packages=["com.example.app"]
        )

    def test_init(self, monitor):
        assert monitor.adb_path == "adb"
        assert monitor.device_id == "device123"
        assert "com.example.app" in monitor.packages
        assert monitor._pid_map == {}

    @patch("subprocess.run")
    def test_resolve_pids_success(self, mock_run, monitor):
        # Mock subprocess.run to return a successful result with PIDs
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "1234 5678\n"
        mock_run.return_value = mock_result

        pids = monitor._resolve_pids("com.example.app")

        assert pids == [1234, 5678]
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["adb", "-s", "device123", "shell", "pidof", "com.example.app"]

    @patch("subprocess.run")
    def test_resolve_pids_failure(self, mock_run, monitor):
        # Mock subprocess.run to return a failure
        mock_result = Mock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        pids = monitor._resolve_pids("com.example.app")
        assert pids == []

    @patch("subprocess.run")
    def test_resolve_pids_exception(self, mock_run, monitor):
        # Mock subprocess.run to raise an exception
        mock_run.side_effect = Exception("ADB error")

        pids = monitor._resolve_pids("com.example.app")
        assert pids == []

    def test_get_package(self, monitor):
        monitor._pid_map = {1234: "com.example.app"}
        assert monitor.get_package(1234) == "com.example.app"
        assert monitor.get_package(9999) is None

    @patch("threading.Thread")
    def test_start_stop(self, mock_thread_cls, monitor):
        mock_thread = Mock()
        mock_thread_cls.return_value = mock_thread

        monitor.start()

        mock_thread_cls.assert_called_once()
        mock_thread.start.assert_called_once()
        assert monitor._thread is mock_thread
        assert not monitor._stop_event.is_set()

        monitor.stop()
        assert monitor._stop_event.is_set()
        mock_thread.join.assert_called_once_with(timeout=1.0)

    @patch("logpyt.streams.sync.PidMonitor._resolve_pids")
    def test_run_loop_updates_map(self, mock_resolve, monitor):
        # Setup mock to return PIDs
        mock_resolve.return_value = [1234]

        # We need to run the loop for one iteration then stop
        # We can do this by setting the stop event after a short delay or
        # by mocking wait to set the event.

        # Let's mock wait to set the event so the loop exits after one pass
        def side_effect_wait(timeout):
            monitor._stop_event.set()

        monitor._stop_event.wait = Mock(side_effect=side_effect_wait)

        monitor._run()

        assert monitor._pid_map == {1234: "com.example.app"}
        mock_resolve.assert_called_with("com.example.app")
