# PR #22 CI Fix Report

## Source failure

- GitHub Actions run: `30393673330`
- Linux full-repository discovery reported two errors:
  - concurrent delivery loser raised `delivery_state_conflict`
  - production readiness reported missing quality `FFMPEG` / `FFPROBE`

## Root causes and fixes

### Canonical delivery concurrency

The delivery flow uploaded before atomically winning the `quality_check` to
`settling` transition. Two equivalent callers could therefore upload the same
object and the transition loser could fail even though the winner was safely
finishing the exact same canonical intent.

The winner now enters `settling` before any upload. A same-intent loser performs
a bounded condition wait and returns the completed result. It may only replay
the idempotent asset-finalization step after the durable settlement and outbox
exist. It never uploads or settles independently. A different canonical intent
still raises `delivery_intent_conflict`. An active leased worker and the
reconciler retain the authority to resume a crashed `settling` delivery.

### Quality binary readiness

`LocalQualityRunner` captured `shutil.which` as a function default and did not
support quality-specific binary settings. It now resolves the current
`shutil.which` at construction time. When
`AI_EDIT_V2_QUALITY_FFMPEG_BIN` / `AI_EDIT_V2_QUALITY_FFPROBE_BIN` are absent it
discovers `ffmpeg` / `ffprobe` from the current process PATH. When either value
is explicitly supplied, only that value is checked and used; an invalid
explicit value fails closed without falling back to PATH.

## TDD evidence

- Stable five-round concurrent delivery test failed before the fix with
  `put_count 2 != 1` in every round.
- PATH discovery test failed before the fix with `ffmpeg,ffprobe` readiness
  errors.
- Invalid explicit binary test failed before the fix because readiness
  incorrectly fell back to PATH.
- Relevant delivery / quality / pipeline tests: 27 passing.
- Full AI Edit V2 discovery: 358 passing in 51.167s.
- Full repository discovery executed 1,369 tests on the local Windows host.
  Neither of the two target CI errors recurred. The run ended with 15 unrelated
  Windows-only errors and one unrelated POSIX path assertion (temporary SQLite
  handle cleanup, Bash-only ship tests, default GBK decoding, and `/tmp` path
  spelling). These are outside this Linux CI fix and were not modified.

No push, deployment, restart, or real provider call was performed.
