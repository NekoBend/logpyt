# Device findings (PR-02)

What the recordings in this directory show, and what each fact means for the
3.0.0 design (`.docs/design-3.0.0.md`, section 9.4). Every value below is read
from the recording named in its section; "from memory" marks the statements
that are not.

## Setup

| | API 24 | API 36 |
|---|---|---|
| Android | 7.0 | 16 |
| Build | `Android/sdk_google_phone_x86_64/generic_x86_64:7.0/NYC/6696031:userdebug/dev-keys` | `google/sdk_gphone64_x86_64/emu64xa:16/BE2A.250530.026.F3/13894323:userdebug/dev-keys` |
| Serial | emulator-5556 | emulator-5554 |
| Time zone | Etc/UTC (`+0000` on every line) | Etc/UTC |

google_apis x86_64 emulator images on emuvm; adb client and server
37.0.1-15733141; the container reached the VM's adb server through an SSH
tunnel to 127.0.0.1:5037. Recorded on 2026-11-04 between 04:22 and 05:35 UTC;
R1, R5 and R6 about 40 s after boot (`/proc/uptime` 40.37 on 24, 40.44 on 36).
Each recording `<KEY>_<slug>.json` holds the argv, rc, trimmed stdout and
stderr of every step, plus the facts the tool derived from the full output.

Re-record with `python tools/device_probe.py --adb <adb> --serial <serial>
[--root] [--vm-ssh <wrapper> --disruptive]`. The live tier is
`LOGPYT_DEVICE=<serial> LOGPYT_DEVICE_API=24|36 pytest -m device tests/device`.

### What has not been run on a device

- The recordings were made by earlier revisions of `tools/device_probe.py`.
  Changes made since then alter what a new recording would hold: C10's
  filtered reader now logs an unmatched line and connects with
  `-T <clock - 1 s>` (the stored C10 used `-T <clock>`); R6 stores every uid
  token (the stored api36 fact holds only the 60 most common) and a sample
  that covers every token; C06 stores `between_list_neighbors: null` for a
  notice in the first or last row; C09 needs `--disruptive` and follows
  `--out`. None of these changes has run on a device.
- The live tier (`tests/device/`): an earlier revision gave 12 passed on each
  level on 2026-11-04 (about 05:30 UTC). The tool, the checks and the live
  test changed after that run; the revision in this PR has not been run on a
  device.

## Summary

| Check | Result on 24 and 36 | For |
|---|---|---|
| 1, R6 | U-1 (a) argv: zone and uid on every line, 0 unmatched | PR-05 (gate met) |
| 2 | epoch `-T <sec>.<usec>` is inclusive and exact to 1 us | PR-14, risk 1 closed |
| 3 | app uids print as numbers; `dumpsys package <pkg>` is the lookup that works on both | PR-11 |
| 4 | every disconnect ends `adb logcat` (rc 143 or 255, empty stderr); an absent device makes it wait | PR-12..PR-14 |
| 5 | a stalled reader gets a prefix, then EOF (rc 1); which logd branch is not shown | PR-14, risk 3 |
| 6 | `chatty ... expire N lines` on 24 only | PR-14 |
| 7 | default buffers include crash; Java crash and ANR texts confirmed on 36 only | PR-06, DECIDE below |
| 8 | `-v process` ends with two spaces and `(tag)` | PR-05 |
| 9 | a client-started server has its own session and none of our pipes (Linux only) | PR-12, risk 7 |
| 10 | `%N` missing on 24; a filtered reader prints nothing on 36 until a match | PR-13, PR-14, risk 14 |
| 11 | logwrapper bursts are identical; `log -t` writes to main; trailing space on 24 | PR-14 |
| 12 | a host `ANDROID_LOG_TAGS` filters the device output unless removed | PR-08 |

## Results per check

### Open check 1 and R6: the U-1 (a) argv (`R6_u1a_argv.json`)

- `logcat -d -v threadtime -v year -v usec -v zone -v uid` (step `dump`):
  every line matched the U-1 (a) format on both levels, 4358 lines on 24 and
  80456 on 36 (`unmatched_lines` 0), with zone `+0000` on every line. The live
  reader with the same argv (step `live`) matched too.
- uid column on 24: 39 distinct tokens, all recorded. App uids print as
  numbers (10001 to 10070), the short AID names `logd`, `media`, `radio`,
  `root`, `wifi` as names, the other system uids as numbers (1000, 1003, 1017,
  1037, 1040, 1041, 1046, 1047).
- uid column on 36: the stored `uid_tokens` fact keeps only the 60 most common
  tokens (smallest count 14) and the stored sample stops at 200 lines, so the
  list is incomplete. Fact and sample together hold 72 tokens; the names among
  them are `drm`, `gps`, `hsm`, `lmkd`, `logd`, `media`, `radio`, `root`,
  `shell`, `uwb`, `wifi`. App uids print as numbers (lowest 10095). The counts
  come from one dump per level.
- On 36 the default buffers include `kernel`; kernel lines can have an empty
  tag or a tag with a space (step `live`), and the U-1 (a) pattern still
  splits them (`tests/test_device_fixtures.py`).

For the design: U-1 (a) holds on both levels, so PR-05's gate is met. The
design 5.8 parse rules for digits and short AID names cover every token seen;
the `u<user>_a<appid>` rule was not exercised (open item 1).

### R1: output shapes (`R1_output_shapes.json`)

- `-v threadtime` (also the default), `+ year`, `+ year + usec`, `+ epoch`,
  `+ epoch + usec` and `+ uid` print the expected shapes on both levels.
- `-v year -v UTC -v zone` prints no zone on either level: two zone-selecting
  words toggle it off again (logprint.c:258).
- `-v year -v nsec` prints 9 fraction digits on 36. On 24 `nsec` is unknown and
  taken as a time zone: `2026-11-04 04:22:22.688 +0000` (milliseconds and a
  zone). The library never passes `nsec`.

### R2 and R2x: `-T` in date form (`R2_tail_time.json`, `api24/R2x_clock_step_after_boot.json`)

- `-T 'YYYY-MM-DD hh:mm:ss.mmm'` is inclusive: all 10 reference lines at the
  boundary millisecond came back on both levels, none before it. The
  microsecond form likewise (2 boundary lines on 24, 1 on 36).
- `-T <sec>.<ms>` is accepted (rc 0); the first line returned is at T on both.
- `-T 5` (digits only) is a line count: 5 lines on both levels. (The first
  probe on 2026-11-04 saw 38 on 24.)
- R2x, on 24 about 40 s after boot: AlarmManagerService logged
  `Setting time of day to sec=1793766129` at 04:22:09.330895, and the next
  line carries 04:22:09.002035, so the clock stepped back about 0.33 s. A
  `-T 2026-11-04 04:22:09.072` read then returned 40 lines below T (of 159),
  the first of them before any boundary line, and still every boundary line
  (0 missing).

For the design: step 2 of design 5.6 (a line below `T_req` is dropped and
counted in `below_cursor`) is needed even without an outage: a clock step
after boot is enough.

### Open check 2: epoch `-T <sec>.<usec>` (`C02_epoch_usec_boundary.json`)

- Boundary: `-T <T>` returned every reference line at T as its first lines, in
  a dump and in a live reader (24: 2 lines at T = 1793767268.763707; 36: 1 line
  at 1793767479.403395). `-T <T + 1 us>` returned none of them. Inclusive and
  exact to the microsecond on both levels.
- Before the ring start: `-T 1000000000.000000` returned the same first entry
  as a dump without `-T` (24: 73518 lines in both; 36: 212111 and 212112, the
  device kept logging between the two dumps).
- The `since="now"` order of design 5.6 (steps `marker_first_<i>`,
  `marker_then_reader_<i>`): marker written, then a reader with
  `-T <clock - 1 s> -s <tag>:V`. The marker was delivered in 3 of 3 runs on
  each level.
- T newer than every entry (steps `after_newest_<i>`): T is
  `(date +%s + 1) * 1 s` and `date +%s` is the floor of the clock, so T was 0
  to 1 s ahead of the device clock when the reader connected. Three markers
  m1, m2, m3 were logged 2.5 s after the connect.
  - 24: m1 was not delivered in runs 0 and 2, and was delivered in run 1; m2
    and m3 always came. The readers connected about 0.6, 0.05 and 0.5 s before
    T (T was 04:44:51, 04:44:58 and 04:45:01). Nothing was delivered before the
    markers (`entries_before_first_marker` 0), and the reader printed nothing,
    not even the buffer divider, until they came (`first_byte_s` 2.51).
  - 36: every marker was delivered in every run. In run 0 the reader also
    delivered 15 entries with timestamps below T (`lines_below_T` 15), the
    first 0.12 s after the spawn, so they were logged after it connected: a T
    ahead of the newest entry does not filter what is logged next.

For the design: risk 1 is closed. Epoch `-T` with six fraction digits is
inclusive at the boundary on both levels, so `T_req` needs no 1 us offset. On
7.0, a reader whose T is newer than every entry can lose the next entry; the
`since="now"` order avoids it and delivered every time. A resume `T_req`
below lines already read is not in that case; a `T_req` above every entry
(for example after `logcat -c` or a reboot) is, which this recording did not
test. The 36 lines below T are what the `below_cursor` rule covers.

### Open check 3: uid column and the package-to-uid lookup (`C03_uid_lookup.json`)

- App lines carry numeric uids on both levels: 11 distinct app uid tokens on 24
  (10002 to 10066), 7 on 36 (10097 to 10183). No app uid printed as a name.
- `pm list packages -U`: on 24 it prints `Error: Unknown option: -U` and exits
  0, also with a package argument and as `cmd package`. On 36 it lists
  `package:<name> uid:<uid>` for 248 packages, and the package argument is a
  substring filter: `pm list packages -U com.android.settings` returned
  `com.android.settings uid:1000` and
  `com.android.settings.auto_generated_rro_product__ uid:10010`.
- `dumpsys package packages` prints `userId=<uid>` per package on 24 (98
  packages) and `appId=<uid>` on 36 (252 packages); on 36 every uid from
  `pm list packages -U` equals the `appId`. `dumpsys package
  com.android.settings` answers on both (`userId=1000` on 24, `appId=1000` on
  36).
- Every listed package name (98 on 24, 248 on 36) passes the F5-3 grammar of
  design 2.7 (risk 13).
- Shared uids exist on both levels: uid 10014 on 24 maps to 4 packages
  (`com.google.android.gms`, `.gsf`, `.gsf.login`, `.backuptransport`), uid
  10097 on 36 to 5.

For the design (5.8, PR-11): the uid method is usable on both levels. The
command that works on both is `dumpsys package <pkg>`, read for `userId=` (24)
or `appId=` (36). `pm list packages -U <pkg>` exists on 36 only, fails with rc
0 on 24, and matches substrings, so a caller must compare names exactly. The
output of `dumpsys package` for a package that is not installed was not
recorded.

### Open check 4: disconnects and an absent serial (`C04_disconnects.json`, `R4_absent_serial.json`)

A running `adb logcat -v threadtime`, then one action; the time is from the
start of the action to the exit of `adb logcat`.

| Action | 24 | 36 | stderr |
|---|---|---|---|
| device logcat killed (SIGTERM) | rc 143 after 0.02 s | rc 143 after 0.03 s | empty |
| `adb reconnect` | rc 255 after 0.02 s | rc 255 after 0.01 s | empty |
| adb server restart on emuvm | rc 255 after 1.32 s | rc 255 after 1.11 s | empty |
| emulator killed (`emu kill`, the unplug) | rc 255 after 0.85 s | rc 255 after 2.1 s | empty |

- After the unplug: `adb get-state` rc 1 `error: device offline`, `adb shell
  date` rc 1 `adb: device offline`, both within 4 ms; `adb logcat -d -t 1`
  waits with `- waiting for device -` (still waiting after 10 s).
- R4, a serial that was never attached (`emulator-9999`): `adb logcat` (dump
  and live) waits with `- waiting for device -` on stderr and is still running
  after 10 s; `adb get-state` gives rc 1 `error: device 'emulator-9999' not
  found` and `adb shell date` rc 1 `adb: device 'emulator-9999' not found`,
  each within 9 ms. The same on both levels.

For the design (3.2, 5.6): every disconnect kind ends `adb logcat` by itself
within about 2 s, with nothing on stderr, so the exit code is the only signal,
and only the device-logcat kill (143) differs from the rest (255). An absent or
unplugged device never ends `adb logcat`, so the state check after
`connect_timeout` is needed; `get-state` answers at once in both cases.

### Open check 5: a stalled reader under heavy logging (`C05_stalled_reader.json`)

Two readers with `-T <date +%s> -s sh:V` (only the writer's lines and buffer
dividers pass), a start marker, a writer (`logwrapper sh -c` loop), an end
marker. The control reader reads everything; the other stops reading (`fast`,
`slow`) or reads at 50 kB/s (`slow_reader`).

| Run | Level | Lines written, rate | Stall | Stalled reader | Then |
|---|---|---|---|---|---|
| fast | 24 | 150000, 9455/s | 18.9 s | first 74146, then 75854 missing | rc 1, `read: unexpected EOF!` |
| slow | 24 | 150000, 978/s | 156.3 s | first 69780, then 80220 missing | rc 1, same |
| slow_reader | 24 | 300000, 1952/s | 156.7 s | first 101762, then 198238 missing | rc 1, same |
| fast | 36 | 400000, 5545/s | 75.2 s | first 76437, then 323563 missing | rc 1, `logcat: Unexpected EOF!` |
| slow | 36 | 150000, 610/s | 248.7 s | first 71600, then 78400 missing | rc 1, same |
| slow_reader | 36 | 300000, 1255/s | 242.1 s | all 300000 | still connected |

- The control readers received every line in every run (0 missing, 0
  duplicates), and liblog reported no drop at the source. No stalled reader got
  a line after its hole: one missing range, ending at the last line written,
  and no end marker.
- `stalled_reader_rc_during_stall` is null because adb blocks writing into our
  full pipe; it says nothing about what logd did meanwhile.
- 36: the kernel buffer holds `W logd : SocketClient: write error (Try again)`
  at 04:57:28.08 in `fast` (start marker at 04:56:39.48, so 48.6 s into a
  75.2 s stall) and at 05:00:38.55 in `slow` (start 04:58:07.11, 151 s into a
  248.7 s stall). That timestamp marks when logd dropped the reader, after a
  failed socket write.
- 24: after `fast` the main buffer read `256Kb (2Mb consumed)`, more than twice
  its size: the release branch that design 5.1 reads from LogBuffer.cpp
  `prune()`. After `slow` and `slow_reader` it read 250Kb and 248Kb consumed,
  so those two runs do not show the branch. 7.0's logd logged nothing about
  readers (step `dmesg_logd`).
- Every run wrote many times the ring size during the stall, so these runs test
  the release; a stall that overflows the ring by less than twice its size
  (the skip branch, `triggerSkip_Locked`) was not run. A skip followed by a
  release would also leave one missing tail, so the recordings neither show nor
  exclude a skip.
- Whether a notice marks the loss is not observable here: the `-s sh:V` filter
  drops `chatty` lines.
- On 24 every run needed a second start marker: the first, logged 2 s after the
  readers connected with `-T <date +%s>`, was never delivered (first byte at
  7.04 s), the same loss as in check 2. On 36 one marker was always enough.

For the design (5.6 stall resume, risk 3): design 5.6 says 7.0's logd does not
disconnect a slow reader but skips entries for it. In these runs the stalled
reader was disconnected on both levels, after an unbroken prefix, with only an
EOF on stderr and rc 1; it was never told about the loss. The stall rule (kill
after `stall_timeout`, then resume with the ring check) is still required, and
a reconnect after such an EOF must run the resume check, because the EOF is the
only signal of the hole. The overwrite gate of section 9.4 (PR-14) tests the
stall rule itself.

### Open check 6: chatty notices (`C06_chatty.json`)

- 24: a writer logged 40000 lines at about 149k lines/s; liblog dropped 34177
  of them at the source (472 `liblog` events in the events buffer). The dump
  that followed has no `-b`, so it read the default buffers (main, system,
  crash); it held 3669 of the writer's lines (sequence numbers 11556 to 39663)
  and 52 `chatty` notices: 32 for uid 10014, 14 for shell, 6 for 10052.
- The 12 notices recorded in detail: tag `chatty`, level I, the pruned uid in
  the uid column, and the message
  `uid=<uid>[(<package>)] [<process or thread name>] expire <N> line[s]`, for
  example `uid=10014(com.google.android.gms) com.google.process.gapps expire 4
  lines`, `uid=10014 com.google.android.gms.persistent expire 2 lines` and
  `uid=10014(com.google.android.gms) expire 1 line` (the regex is `CHATTY` in
  `tests/device_facts.py`). All 12 are `expire` notices.
- Their timestamps (04:21:49 to 04:21:51, during boot, in a dump taken at
  04:38) each lie between those of their neighbours in list order: a notice
  carries a time at the position of the lines it replaces. When logd wrote them
  is not recorded.
- A `-T` read from 1 s before the first notice returned all 52 notices.
- 36: an 800000-line flood (about 92k lines/s; liblog dropped 205040) made the
  ring prune (the oldest writer line left is number 529658), and the dump held
  no notice. From memory, logd stopped writing chatty notices in Android 12;
  not checked against the source here.

For the design (5.6 chatty rules, PR-14): the `expire N lines` text is
confirmed on 24 and absent on 36, so `device_reported` is a signal of 7.0-era
logd only. Notices come back in a resume overlap, so the rule that a notice
with timestamp `<= hw` is a replay is needed. A writer that outruns logd loses
lines inside liblog, which shows only as `liblog` events in the events buffer:
a loss the library does not see in the buffers it reads.

### Open check 7: default buffers, crash and ANR lines (`C07_crash_anr.json`)

- Default buffers (`logcat -g`): main, system, crash on 24; main, system,
  crash, kernel on 36.
- Test app: `com.android.settings`, which runs as the system uid 1000 (C03).
- Native crash (`kill -SEGV <pid>`), both levels: the crash buffer holds
  `F libc : Fatal signal 11 (SIGSEGV), ...` from the app's pid with uid 1000,
  then `F DEBUG` tombstone lines, among them
  `pid: <pid>, tid: <tid>, name: ndroid.settings  >>> com.android.settings <<<`,
  from another pid: uid 1045 on 24; on 36 crash_dump64 (pid 5539) with uid
  1000. Uid 1000 is also the app's uid, so on 36 the recording cannot tell
  "the app's uid" from "another uid". 36 also logs `crash_dump64` and
  `tombstoned` lines in main. The system buffer holds
  `I ActivityManager: Process com.android.settings (pid <pid>) has died` on
  both (36 appends `: fg  TOP`).
- Java crash (`am crash com.android.settings`), 36: the crash buffer holds one
  entry (every line has the same timestamp, pid and tid):
  `E AndroidRuntime: FATAL EXCEPTION: main`,
  `E AndroidRuntime: Process: com.android.settings, PID: 5576`, the exception
  and its stack. Pid 5576 is the app's pid, with uid 1000: the app logs its own
  Java crash, from its own pid and uid. 24 has no `am crash` (it printed the am
  usage), and starting the app with an unknown fragment produced no
  `E AndroidRuntime` line: no Java crash was recorded on 24.
- ANR (SIGSTOP, then key events), 36: the system buffer holds
  `E ActivityManager: ANR in com.android.settings (com.android.settings/.Settings)`
  from system_server (pid 669, uid 1000), followed by `Reason:` and `Parent:`
  lines with the same header, and earlier
  `I WindowManager: ANR in Window{... com.android.settings/...}` at level I.
  24 logged `InputDispatcher` lines (`spent 72194.1ms processing the last
  input event`) and no `ANR in` line.

For the design: design 6.4 holds (no `-b` is injected; the crash lines are in
the crash buffer, which both levels read by default). For design 2.7
(`CrashCondition` and `AnrCondition` with `package=`, PR-06), the
`Process: <name>, PID: <pid>` line and the `ANR in <name> (` line are confirmed
on 36 with the 2.x default tag and level (AndroidRuntime/E,
ActivityManager/E); the WindowManager `ANR in Window{` line is at level I, so
the default level does not match it. A native crash logs neither line; its
package appears only in the DEBUG `>>> <pkg> <<<` line, which design 2.7 does
not plan to match (DECIDE below). Unverified: the Java crash and the ANR on 24,
and the crash lines of an app with an app uid (open item 3).

### Check 8: `-v process` (`C08_process_format.json`)

Every line is `<level>( <pid>) <message>  (<tag>)` on both levels, for example
`I( 1599) Killing 2271:com.google.android.deskclock/u0a64 (adj 906): empty #18  (ActivityManager)`:
two spaces, then the tag in parentheses at the end (S-parsers-models-12). The
message itself can contain parentheses, so the tag is the last parenthesized
group after two spaces.

### Check 9: the adb server's process group (`host/C09_server_process_group.json`)

Measured on emuvm (Linux), the adb server host, never in the container:
`adb kill-server`, then `adb devices` with piped stdout and stderr, twice
(client in the probe's process group, then in a new session). Both times the
client exited at once (rc 0, `* daemon not running; starting now at tcp:5037`,
`* daemon started successfully`), and the server it started had parent 1, its
own session and process group (`pgid == sid == pid`, not the client's group),
stdin `/dev/null`, stdout and stderr on adb's log file (`/tmp/adb.1000.log`),
and none of the client's pipes.

For the design (5.11, risk 7): on Linux, a server our client starts shares
neither the client's process group nor its pipes. Killing the pid only (5.11)
stays, as it is correct either way. Windows was not measured (open item 5).

### Check 10: device clock and first byte (`C10_clock_first_byte.json`)

- `date +%s` works on both levels. `date +%s.%N` prints `1793766304.N` on 24
  (no `%N`) and nanoseconds on 36 (`1793766309.369942584`). The device clock
  was 4.2 s ahead of the host clock on both.
- `adb shell true` took 5 to 6 ms on 24 and 8 to 9 ms on 36. A reader without a
  filter printed its first byte 8 to 12 ms (24) and 15 to 19 ms (36) after the
  spawn.
- A reader whose `-s <tag>:V` filter matches nothing (step `quiet_filter`; this
  recording used `-T <date +%s>`): on 36 it printed nothing (0 bytes) until the
  marker, then the divider and the marker together, 20 ms after the marker was
  written. On 24 it printed `--------- beginning of main` at once (first byte
  after 46 ms), and the marker 20 ms after it was written.
- 7.0 prints one `beginning of <buffer>` divider per buffer as soon as logd
  delivers an entry of that buffer at or after T, whatever the filter: C12's
  `tags_silent` dump with `ANDROID_LOG_TAGS=*:S` printed `beginning of main` and
  `beginning of system` and no line. When T is newer than every entry, logd
  delivers nothing, logcat prints nothing, and the next entry can be lost
  (C02 `after_newest`, C05 start markers). So the 7.0 divider comes at once
  only when an entry at or after T exists; this recording had one (the device
  was busy, 3 minutes after boot). The current tool logs an unmatched line
  before it connects with `-T <clock - 1 s>`, so that entry always exists.

For the design (3.2, 5.6, risk 14): a filtered stream on a quiet 36 device
prints nothing, so the state check after `connect_timeout` is what moves it to
RUNNING. On 24 the divider can come at once, but only when logd has an entry to
deliver, and it is not a log line. The clock read uses whole seconds
(`date +%s`) on both levels, because 24 has no `%N`; the device clock, never
the host clock, is the base for `-T`.

### Check 11: logwrapper and `log -t` (`C11_logwrapper_log_t.json`)

- `logwrapper <command>` logs at level I with uid shell and the command name as
  tag (`echo`, `sh`). `logwrapper sh -c 'for i in 1 ... 8; do echo
  lp-burst-<id>; done'` logged 8 byte-identical lines with one pid and tid.
- The gaps between the 8 burst lines: [4, 0, 2, 1, 3, 1, 0] us on 24 (two
  same-microsecond pairs), [38, 4, 3, 3, 2, 3, 204] us on 36 (none).
- `log -t logpyt <token>` wrote to main only (0 lines in system and crash on
  both). On 24 its message ends with a space (`lp-mark-5fdadbfa `), on 36 it
  does not; logwrapper lines have no trailing space on either level.
- `logwrapper echo` exited with rc 129 on 24 (0 on 36) and still logged its
  line.

For the design (5.6 markers, 9.4 resume gate): markers are matched after
stripping trailing spaces (24). `log -t` writes to main, so `since="now"`
without main in the buffer set stays rejected. Note for PR-14: on 36 an 8-line
burst had no same-microsecond pair, so the gate's rule that at least one burst
had a collision may need longer or more bursts on 36.

### Check 12: `ANDROID_LOG_TAGS` (`C12_android_log_tags.json`)

`adb logcat -d -t 300 -v threadtime`, with the variable in the host
environment of the adb client:

- `*:S`: 0 lines on both levels (24 still printed two dividers).
- `logpyt:V *:S`: only the 2 `logpyt` lines.
- removed from the environment: 663 lines with 61 tags on 24, 816 with 51 on
  36.
- `*:S`, plus a `*:V` filterspec on the command line: the same as removed;
  the filterspec wins.

For the design (5.6, PR-08): the host value reaches the device logcat and
filters its output whenever the command line has no filterspec, so the child
environment must drop it, as `tools/device_probe.py` does.

### R3: `adb shell pidof` (`R3_pidof_shell.json`)

On both levels, `adb shell pidof 'com.example.none; echo
INJECTED_VIA_SEMICOLON'` printed `INJECTED_VIA_SEMICOLON` (rc 0);
`pidof 'system_server zygote64'` (one argument) printed two pids; an unknown
name gave rc 1 and no output. The device shell parses the argument (F5-3).

### R4: an absent serial (`R4_absent_serial.json`)

See check 4.

### R5: rings and the startup backlog (`R5_ring_backlog.json`)

- Rings (`logcat -g`): 256Kb for main, system and crash on 24; 2 MiB on 36
  (`-b all`: radio, events, stats and security 256 KiB, kernel 2 MiB).
- Backlog about 40 s after boot. 24: 4358 lines (643 kB); 34.2% share their
  microsecond with another line (largest group 25) and 81.9% their millisecond
  (largest 378); 9 groups of byte-identical lines at one microsecond (largest
  2). 36: 80429 lines (12.3 MB); 63.7% share a microsecond (largest group 98)
  and 92.9% a millisecond (largest 164); 2518 identical groups (largest 5).

For the design (5.6): the dedupe needs occurrence counts per microsecond, and
byte-identical lines at one timestamp are common in a fresh backlog (X-1).

## Open items

1. `u0_aN` uid names on 36 (design 9.4 check 3, 5.8) were not exercised. On
   this image the app ids below 10 belong to emulation and RRO overlay
   packages (uid 10000 `com.android.internal.emulation.pixel_4` to 10009; for
   example 10007 `com.android.phone.auto_generated_characteristics_rro`), and
   none of them logged; the lowest app uid in any 36 recording is 10095. The
   5.8 parse rule for `u<user>_a<appid>` stays from memory.
2. The full list of uid tokens in a 36 dump: the stored R6 fact was cut at 60
   tokens. Re-record R6 with the current tool.
3. Crash lines of an app with an app uid (>= 10000): the uid of its
   crash_dump, DEBUG and tombstoned lines is unverified, because the test app
   has a system uid. Also unverified: the Java crash and the ANR on 24 (neither
   was induced).
4. C05: the skip branch of 7.0's logd (a stall that overflows the ring by less
   than twice its size) was not run, and a notice cannot be seen through the
   `-s sh:V` filter. The overwrite gate of PR-14 tests the stall rule itself.
5. C09 on Windows: whether an adb server that our client starts inherits the
   client's pipe handles. From memory, adb starts its server there with
   CreateProcess, so fork and setsid do not apply, and handle inheritance is
   the open question.
6. C10 with the current filtered-reader order, and the live tier as it is in
   this PR: not run on a device.
7. Not probed: an adb client/server version restart; a TCP device
   (`adb connect`) reconnect (deferred by design 6.4); `dumpsys package` for a
   package that is not installed; a reader whose `-T` is above every entry after
   `logcat -c` or a reboot; physical user builds (risk 17).

## DECIDE for PR-06: native crashes and `CrashCondition(package=)`

Design 2.7 plans `package=` matching on the Java crash's
`Process: <name>, PID: <pid>` line (AndroidRuntime/E) and on `ANR in <name>`
(ActivityManager/E); both are confirmed on 36. A native crash logs neither: its
package appears only in `F DEBUG : pid: <pid>, tid: <tid>, name: <thread>  >>>
<pkg> <<<` (C07, both levels), which no PR plans to match.

- (a) Keep design 2.7 and document that `CrashCondition(package=)` misses
  native crashes. No cost.
- (b) With `package=`, also match the DEBUG line (tag DEBUG, level F, the same
  `<name>` rule). One more pattern in PR-06, tested on these recordings.
- (c) A separate `NativeCrashCondition`. A new public name.

Recommendation: (b), because a crash filter that silently misses native
crashes surprises the CI-capture scenario, and the line is recorded on both
levels. The choice is the user's; PR-06's crash matching waits for it.
