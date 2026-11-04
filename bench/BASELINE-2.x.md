# Stream benchmark: logpyt 2.x baseline

Recorded on 2026-11-04 with `bench/stream_bench.py` on `release/3.0.0` at
bc26785, whose stream, parser and model code is still that of 2.0.0 (8810bf5).
PR-12 and PR-13 compare the 3.0 drivers against these numbers
(`.docs/design-3.0.0.md`, sections 8.2 and 10.1, risk 16).

## Machine

- Linux devbox container (Docker on a Windows PC, kernel 7.0.14-linuxkit),
  CPython 3.14.4, 32 logical CPUs (Intel Core i9-13900HX), 23 GiB RAM.
- The PC is shared with other work, so the numbers are indicative. Compare
  runs made on the same machine close together in time; a row's min..max
  spread shows how much a single run varies. Load average was 0.3 before the
  run.

## Command

From the repository root:

    uv run python -m bench.stream_bench

The defaults are `--cases throughput burst slow-burst --drivers sync async
--lines 200000 --burst-lines 10000 50000 --slow-us 20 --repeat 5`. The run took
48 s. In the devbox it ran as `uv run --frozen --no-sync python -m
bench.stream_bench`, with `UV_PROJECT_ENVIRONMENT` and `TMPDIR` pointing at a
scratch directory outside `/workspace`.

## Results

logpyt 2.0.0; Python 3.14.4 (CPython); Linux-7.0.14-linuxkit-x86_64-with-glibc2.43; 32 logical CPUs; 5 run(s) per row

| case | driver | lines | delivered/expected (median; min..max) | lines/s (median; min..max) |
|---|---|---:|---:|---:|
| throughput | sync | 200,000 | 200,000/200,000 (100.0 %; 200,000..200,000) | 225,251 (221,870..229,805) |
| throughput | async | 200,000 | 200,000/200,000 (100.0 %; 200,000..200,000) | 213,970 (210,114..216,005) |
| burst | sync | 10,000 | 10,000/10,000 (100.0 %; 10,000..10,000) | 168,848 (158,528..176,163) |
| burst | async | 10,000 | 7,168/10,000 (71.7 %; 7,168..7,168) | 118,814 (107,566..122,426) |
| burst | sync | 50,000 | 50,000/50,000 (100.0 %; 50,000..50,000) | 214,062 (207,736..217,654) |
| burst | async | 50,000 | 35,215/50,000 (70.4 %; 35,210..35,215) | 149,725 (143,418..152,030) |
| slow-burst (20 us/entry) | sync | 10,000 | 10,000/10,000 (100.0 %; 10,000..10,000) | 38,091 (36,937..38,202) |
| slow-burst (20 us/entry) | async | 10,000 | 7,168/10,000 (71.7 %; 7,168..7,168) | 34,591 (34,097..35,462) |
| slow-burst (20 us/entry) | sync | 50,000 | 23,274/50,000 (46.5 %; 18,273..28,924) | 33,328 (30,742..35,323) |
| slow-burst (20 us/entry) | async | 50,000 | 35,215/50,000 (70.4 %; 35,215..35,215) | 37,170 (37,050..37,638) |

During the sync slow-burst 50,000 row, 2.x also logged `WARNING logpyt:
LogStream callback queue is full. Dropping log entry.` five times, once per
run (each stream rate-limits that warning to one per 5 s).

## Reading the numbers

- Every row: adb writes the whole backlog at start and then stays alive, so a
  loss at adb's exit (F1-1) cannot appear here. Time runs from `start()` to the
  last delivery and includes the fake adb's start-up. On its own, the fake adb
  writes the same 200,000 lines (9.1 MB) to a pipe in 0.06 s, so the stream,
  not the harness, sets the rate.
- throughput: both 2.x drivers parse and deliver about 210,000 to 230,000
  lines/s. The queues are sized to hold the whole backlog, because 2.x drops
  once a queue is full.
- burst, the X-1 defect: with default settings and a callback that only counts,
  the async stream delivers about 70 % of a burst. Per X-1, its buffered
  `readline` never yields while data is buffered, so ingestion starves the
  dispatcher and the 1024-item default queue overflows. The sync stream
  delivered everything here: on 32 CPUs its callback thread kept up, and its
  10,000-entry default queue cannot overflow on a 10,000-line burst. The
  earlier X-1 figures in `.docs/review-findings-2.0.1.md` (async 2,048/10,000
  and 9,163/50,000; sync 10,000/10,000 and 35,596/50,000) come from a probe
  whose recorded setup (fake adb, counting callback, default settings)
  matches the burst case. Some detail that record does not give must have
  differed, because the figures differ from this table (sync 35,596 against
  50,000 of 50,000; async 2,048 against 7,168 of 10,000), so they are not
  comparable with it. Only sync 10,000/10,000 agrees.
- slow-burst: with 20 us of callback CPU per entry the consumer is slower than
  the reader on any machine. The sync stream then drops about half of a
  50,000-line burst (the spread across runs is wide, 37 % to 58 % delivered),
  while its 10,000-entry queue still holds a 10,000-line burst. The async loss
  is the same as in burst.
- lines/s on a lossy row counts only the entries delivered; it is not a
  throughput figure.
- What 3.0 must show (U-4, backpressure only): 100 % in every burst and
  slow-burst row, and throughput close to these rows (risk 16). A lossless
  slow-burst row is bounded by its consumer: at 20 us per entry, at most
  50,000 lines/s.

## Pointing the benchmark at 3.0

Only the drivers know the stream API: `SyncDriver._open`/`run` and
`AsyncDriver._open`/`_run`. Under 3.0 there is no drop policy, so `lossless`
selects nothing and both branches build the default stream. The cases, the
fake adb scripts and the report stay unchanged, so the rows stay comparable.
