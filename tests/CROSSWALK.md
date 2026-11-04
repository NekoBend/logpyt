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
