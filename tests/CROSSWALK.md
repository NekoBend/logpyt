# Test crosswalk

`tests/legacy_inventory.txt` is the frozen list of the logpyt 2.x tests: the 13
test modules of 2.0.0 plus `tests/behavior/` and `tests/test_fake_adb.py`. A PR
that deletes or renames one of them adds a row to the table below in the same
PR, so no 2.x test disappears without a trace (`.docs/design-3.0.0.md`, section
8.3). `tests/test_crosswalk.py` fails when the table and the tree disagree.

The left cell is the old test id, exactly as the inventory lists it. The right
cell is the id of the test that replaces it, or one of these reasons, worded
exactly like this:

    API removed by decision <decision id>
    covered by <test id>
    duplicate of <test id>
    mocked private internals; behavior covered by <test id>

For example:

    | tests/test_stream.py::test_old | tests/conformance/test_x.py::test_new |
    | tests/test_stream.py::test_pause | API removed by decision D-7 |

Rules:

- A test id is a function-level pytest node id with `/` separators and no
  parametrize suffix: `tests/test_pid_monitor.py::TestPidMonitor::test_stop`.
  Every test id in the right cell must exist; a PR that deletes one of them
  updates the rows that name it.
- A decision id is a decision of the design: `D-n`, `A-n` or `U-n` (`D-7`,
  `A-12`, `U-4`). A defect id such as `X-1` or `F1-1` is not a decision.
- One row per old test. A row for a test that still exists is stale and fails.
- Ids are plain text, without backticks. Only the table below may contain lines
  that start with `|`; the examples above are indented code. Add rows directly
  below the last one: a blank line or the end of the file ends the table, and
  no `|` line may follow it.
- `tests/legacy_inventory.txt` is never regenerated or edited;
  `tests/test_crosswalk.py` pins its ids.

| old test id | replacement test id or reason |
|---|---|
| tests/test_filters.py::test_empty_conditions_are_no_constraint | tests/test_filters.py::test_empty_collection_raises |
| tests/test_filters.py::test_filter_all_empty_criteria_passes_every_entry | tests/test_filters.py::test_filter_empty_collection_raises |
| tests/test_filters.py::test_filter_empty_criterion_does_not_match_missing_field | tests/test_filters.py::test_filter_empty_collection_raises |
| tests/test_filters.py::test_filter_empty_level_is_no_constraint | tests/test_filters.py::test_filter_empty_collection_raises |
| tests/test_filters.py::test_filter_empty_message_contains_is_no_constraint | tests/test_filters.py::test_filter_empty_collection_raises |
| tests/test_filters.py::test_filter_empty_package_is_no_constraint | tests/test_filters.py::test_filter_empty_collection_raises |
| tests/test_filters.py::test_filter_empty_tag_is_no_constraint | tests/test_filters.py::test_filter_empty_collection_raises |
| tests/test_filters.py::test_filter_non_empty_criterion_still_filters | tests/test_filters.py::test_filter_empty_collection_raises |
| tests/test_parsers.py::test_log_parser_parse_common_override | tests/test_parsers.py::test_custom_parser_implements_the_protocol |
| tests/test_parsers.py::test_log_parser_parse_stderr | API removed by decision D-9 |
| tests/test_parsers.py::test_log_parser_parse_stdout | tests/test_parsers.py::test_unrecognised_line_becomes_unparsed_entry |
| tests/test_utils.py::test_adb_connect_already_connected | tests/test_adb.py::test_adb_connect_succeeds |
| tests/test_utils.py::test_adb_connect_failure | tests/test_adb.py::test_adb_connect_failure_raises_adb_error |
| tests/test_utils.py::test_adb_connect_success | tests/test_adb.py::test_adb_connect_succeeds |
| tests/test_utils.py::test_adb_connect_timeout | tests/test_adb.py::test_adb_connect_timeout_kills_the_child |
| tests/test_utils.py::test_async_adb_connect_decodes_invalid_utf8 | tests/test_adb.py::test_async_runner_decodes_invalid_utf8_with_replacement |
| tests/test_utils.py::test_async_adb_connect_failure | tests/test_adb.py::test_async_adb_connect_failure_raises_adb_error |
| tests/test_utils.py::test_async_adb_connect_success | tests/test_adb.py::test_async_adb_connect_succeeds |
| tests/test_utils.py::test_async_adb_connect_timeout | tests/test_adb.py::test_async_adb_connect_timeout_kills_the_child |
| tests/test_utils.py::test_async_wait_for_device_error | tests/test_adb.py::test_async_wait_for_device_raises_adb_error |
| tests/test_utils.py::test_async_wait_for_device_error_decodes_invalid_utf8 | tests/test_adb.py::test_async_runner_decodes_invalid_utf8_with_replacement |
| tests/test_utils.py::test_async_wait_for_device_success | tests/test_adb.py::test_async_wait_for_device_passes_the_serial |
| tests/test_utils.py::test_async_wait_for_device_timeout | tests/test_adb.py::test_async_wait_for_device_timeout_kills_the_child |
| tests/test_utils.py::test_list_devices_custom_timeout | mocked private internals; behavior covered by tests/test_adb.py::test_list_devices_timeout_kills_the_child |
| tests/test_utils.py::test_list_devices_error | tests/test_adb.py::test_list_devices_raises_adb_error_on_failure |
| tests/test_utils.py::test_list_devices_filter | tests/test_adb.py::test_list_devices_filters_by_kind |
| tests/test_utils.py::test_list_devices_property_value_with_space | tests/test_adb.py::test_list_devices_keeps_a_property_value_with_a_space |
| tests/test_utils.py::test_list_devices_success | tests/test_adb.py::test_list_devices_parses_the_long_listing |
| tests/test_utils.py::test_list_devices_timeout | tests/test_adb.py::test_list_devices_timeout_kills_the_child |
| tests/test_utils.py::test_resolve_adb_in_android_home | tests/test_adb.py::test_resolve_adb_falls_back_to_the_sdk_directories |
| tests/test_utils.py::test_resolve_adb_in_path | tests/test_adb.py::test_resolve_adb_returns_absolute_path |
| tests/test_utils.py::test_resolve_adb_not_found | tests/test_adb.py::test_resolve_adb_raises_when_no_adb_is_found |
| tests/test_utils.py::test_resolve_adb_path_candidate_must_be_executable_file | tests/test_adb.py::test_resolve_adb_skips_a_file_that_is_not_executable |
| tests/test_utils.py::test_wait_for_device_error | tests/test_adb.py::test_wait_for_device_raises_adb_error |
| tests/test_utils.py::test_wait_for_device_success | tests/test_adb.py::test_wait_for_device_passes_the_serial |
| tests/test_utils.py::test_wait_for_device_timeout | tests/test_adb.py::test_wait_for_device_timeout_kills_the_child |
| tests/test_groupers.py::test_grouper_emit_mode_entry | tests/test_grouping.py::test_group_is_a_sequence_of_its_entries |
| tests/test_windowed_grouper_lazy.py::test_windowed_grouper_emit_mode_entry_flattens_on_flush | tests/test_grouping.py::test_group_is_a_sequence_of_its_entries |
| tests/test_readers.py::test_log_file_reader_basic_reading | tests/test_readers.py::test_read_file_yields_parsed_and_unparsed_entries |
| tests/test_readers.py::test_log_file_reader_error_handling | tests/test_readers.py::test_parser_exception_propagates |
| tests/test_readers.py::test_log_file_reader_filtering | tests/test_readers.py::test_where_filters_entries |
| tests/test_readers.py::test_log_file_reader_parse_error_logging_sanitizes_and_caps_preview | tests/test_readers.py::test_unparsed_line_becomes_fallback_entry |
| tests/test_readers.py::test_log_file_reader_parse_error_logging_throttled | tests/test_readers.py::test_parser_exception_propagates |
| tests/behavior/test_offline_behavior.py::test_reader_filter_by_yields_only_matching_entries | tests/behavior/test_offline_behavior.py::test_reader_where_yields_only_matching_entries |
| tests/behavior/test_package_filter_behavior.py::test_file_reader_package_filter_yields_nothing | tests/behavior/test_package_filter_behavior.py::test_read_file_package_condition_needs_a_uid_map |
| tests/behavior/test_package_filter_behavior.py::test_read_file_package_condition_yields_nothing | tests/behavior/test_package_filter_behavior.py::test_read_file_package_condition_needs_a_uid_map |
| tests/test_pid_monitor.py::TestPidMonitor::test_get_package | mocked private internals; behavior covered by tests/core/test_attribution.py::test_first_lookup_before_reading_attributes_the_backlog |
| tests/test_pid_monitor.py::TestPidMonitor::test_init | API removed by decision D-15 |
| tests/test_pid_monitor.py::TestPidMonitor::test_resolve_pids_exception | mocked private internals; behavior covered by tests/core/test_attribution.py::test_a_failed_lookup_releases_the_lines_that_waited_for_it |
| tests/test_pid_monitor.py::TestPidMonitor::test_resolve_pids_failure | mocked private internals; behavior covered by tests/core/test_attribution.py::test_a_failed_lookup_releases_the_lines_that_waited_for_it |
| tests/test_pid_monitor.py::TestPidMonitor::test_resolve_pids_success | mocked private internals; behavior covered by tests/conformance/test_attribution.py::test_package_lines_are_attributed_by_uid_lookup |
| tests/test_pid_monitor.py::TestPidMonitor::test_run_loop_adaptive_backoff_when_unchanged | API removed by decision D-8 |
| tests/test_pid_monitor.py::TestPidMonitor::test_run_loop_large_package_set_limits_resolve_calls_per_cycle | mocked private internals; behavior covered by tests/core/test_attribution.py::test_relookups_are_one_at_a_time_and_rate_limited |
| tests/test_pid_monitor.py::TestPidMonitor::test_run_loop_partial_resolve_preserves_unpolled_packages | mocked private internals; behavior covered by tests/core/test_attribution.py::test_each_package_waits_its_own_interval |
| tests/test_pid_monitor.py::TestPidMonitor::test_run_loop_resets_backoff_on_change | API removed by decision D-8 |
| tests/test_pid_monitor.py::TestPidMonitor::test_run_loop_updates_map | mocked private internals; behavior covered by tests/core/test_attribution.py::test_a_reinstall_under_a_new_uid_is_looked_up_again |
| tests/test_pid_monitor.py::TestPidMonitor::test_start_stop | API removed by decision D-15 |
| tests/test_pid_monitor.py::TestPidMonitor::test_update_pid_map_applies_add_remove_and_change | mocked private internals; behavior covered by tests/core/test_attribution.py::test_a_mapped_package_found_uninstalled_has_no_uid_again |
| tests/test_pid_monitor.py::TestPidMonitor::test_update_pid_map_no_change_keeps_map_object | API removed by decision D-8 |
| tests/test_stream.py::test_auto_reconnect | tests/conformance/test_reconnect.py::test_reconnect_after_a_nonzero_exit |
| tests/test_stream.py::test_join_timeout_honored_when_callback_thread_blocked | tests/conformance/test_lifecycle.py::test_join_timeout_raises_timeout_error_while_the_consumer_is_busy |
| tests/test_stream.py::test_join_waits_for_callback_thread_completion | tests/conformance/test_callbacks.py::test_join_returns_after_the_last_callback |
| tests/test_stream.py::test_queue_full_warning_rate_limited | API removed by decision U-4 |
| tests/test_stream.py::test_read_loop_caps_readline_size | mocked private internals; behavior covered by tests/core/test_framing.py::test_oversized_line_keeps_following_lines |
| tests/test_stream.py::test_read_timeout | tests/conformance/test_reconnect.py::test_read_timeout_without_reconnect_fails |
| tests/test_stream.py::test_stop_does_not_hang_when_callback_queue_is_full | tests/conformance/test_lifecycle.py::test_never_iterated_pull_stop_join_returns |
| tests/test_stream.py::test_stream_callbacks | tests/conformance/test_callbacks.py::test_push_delivers_every_line_in_order |
| tests/test_stream.py::test_stream_context_manager | tests/conformance/test_lifecycle.py::test_with_block_stops_and_joins |
| tests/test_stream.py::test_stream_error_callback | tests/conformance/test_lifecycle.py::test_spawn_failure_fails_and_is_never_retried |
| tests/test_stream.py::test_stream_grouper_flushes_buffered_entries_on_stdout_eof | tests/conformance/test_callbacks.py::test_group_by_delivers_groups_and_flushes_at_the_end |
| tests/test_stream.py::test_stream_grouper_only_groups_stdout_not_stderr | API removed by decision D-9 |
| tests/test_stream.py::test_stream_join_raises_exception | tests/conformance/test_lifecycle.py::test_reader_crash_raises_from_every_wait |
| tests/test_stream.py::test_stream_join_reraises_same_error_on_second_call | tests/conformance/test_lifecycle.py::test_nonzero_exit_without_reconnect_fails |
| tests/test_stream.py::test_stream_kill_transitions_to_killed_and_join_raises | tests/conformance/test_lifecycle.py::test_kill_discards_backlog_and_join_is_prompt |
| tests/test_stream.py::test_stream_lifecycle | tests/conformance/test_lifecycle.py::test_start_iterate_stop |
| tests/test_stream.py::test_stream_non_zero_exit_is_raised_from_join | tests/conformance/test_lifecycle.py::test_nonzero_exit_without_reconnect_fails |
| tests/test_stream.py::test_stream_package_filter_drops_non_matching_entries | tests/conformance/test_attribution.py::test_package_lines_are_attributed_by_uid_lookup |
| tests/test_stream.py::test_stream_package_resolution | tests/conformance/test_attribution.py::test_package_lines_are_attributed_by_uid_lookup |
| tests/test_stream.py::test_stream_parse_error_propagates_to_join | API removed by decision U-1 |
| tests/test_stream.py::test_stream_pause_suspends_dispatch_and_resume_restores_it | API removed by decision D-7 |
| tests/test_stream.py::test_stream_raising_on_start_callback_does_not_break_teardown | tests/conformance/test_callbacks.py::test_on_event_raising_on_connecting_fails_cleanly |
| tests/test_stream.py::test_stream_startup_failure_is_raised_from_join | tests/conformance/test_lifecycle.py::test_missing_adb_fails_at_the_clock_read |
| tests/test_stream.py::test_stream_stderr_callback_receives_entry | tests/conformance/test_lifecycle.py::test_adb_stderr_is_logged_and_kept_in_the_error |
| tests/test_stream.py::test_sync_queue_overflow_policy_drop_oldest_is_configurable | API removed by decision U-4 |
| tests/behavior/test_sync_stream_behavior.py::test_a_zero_adb_exit_makes_join_return_normally | tests/behavior/test_sync_stream_behavior.py::test_a_zero_adb_exit_is_the_outcome |
| tests/behavior/test_sync_stream_behavior.py::test_kill_makes_join_raise_killed_error | tests/behavior/test_sync_stream_behavior.py::test_kill_ends_killed_and_join_returns |
| tests/behavior/test_sync_stream_behavior.py::test_stderr_output_reaches_only_stderr_callback | tests/behavior/test_sync_stream_behavior.py::test_stderr_lines_are_logged_not_delivered |
