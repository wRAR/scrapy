# ExecutionEngine state machine (#6916) — implementation notes

Working notes for this branch; not meant to be merged (drop before merging or
move into the PR description).

## What was implemented

All steps of the #6916 plan as one change set. The four boolean flags
(`running`, `_starting`, `_stopping`, plus the implicit state in nullable
attributes) are replaced by a two-axis state machine.

### Core design (`scrapy/core/engine.py`)

- Public `EngineState` (`CREATED → STARTING → RUNNING → STOPPING → STOPPED`)
  and `SpiderState` (`NONE → OPENING → OPEN → CLOSING → CLOSED`) enums,
  exposed as read-only `engine.state` / `engine.spider_state` properties.
  Two axes because the shell lifecycle is inverted (engine starts before a
  spider is opened); a single state chain cannot represent both orders.
- `running` is now a derived read-only property; its setter warns
  `ScrapyDeprecationWarning` and does nothing. `_starting`/`_stopping` are
  removed. `paused` stays orthogonal.
- `_transition_to()` validates transitions against per-axis tables; invalid
  transitions are logged with stack info rather than raised (they would
  indicate Scrapy bugs, not user errors).
- **The lifecycle methods never wait for an operation started by someone
  else**, because they are routinely called from code that those operations
  await — a `spider_closed` handler, an `engine_stopped` handler, a pipeline's
  `close_spider()`, `_spider_closed_callback` — and waiting there deadlocks.
  `stop_async()` returns immediately if the engine is already stopping or
  stopped, and `close_spider_async()` returns immediately if the spider is
  already closing. The work that still has to happen is instead handed off
  through two "pending" attributes:
  - `_pending_close_reason`, consumed at the end of `open_spider_async()`: a
    close requested while the spider was still opening;
  - `_pending_stop`, consumed at the end of `close_spider_async()`: the rest
    of the stop sequence (`_finish_stop()`, which sends `engine_stopped`,
    transitions to STOPPED and fires `_closewait`), when the stop was
    requested while the spider was opening or closing.

  This keeps `spider_opened` → `spider_closed` → `engine_stopped` ordered in
  every path, and it means no engine method can deadlock on another one.
  Code that wants to wait for the engine to stop awaits `start_async()` (or
  `Crawler.crawl_async()`), which completes when the engine has stopped.
- The `_stopping` band-aid is gone. The races from the issue are now defined
  behavior, each with a test in `tests/test_engine_lifecycle.py`:
  - close from an `engine_started` handler (race 1);
  - close completing before `start_async()` (races 2/3): `start_async()`
    detects the closed spider, sends `engine_started` + `engine_stopped` and
    finishes the shutdown instead of silently returning half-started;
  - double/late closes (races 4/6): `close_spider_async()` is idempotent —
    returns after CLOSED, and during CLOSING;
  - spider-less start (path 7) and stop-from-within-request-processing
    (path 8; the can't-cancel-current-asyncio-task special case is preserved).
- Fixed along the way:
  - a spider-less stop (the shell flow) now closes the downloader, which
    previously leaked;
  - a stop requested while the spider is closing no longer emits
    `engine_stopped` before `spider_closed` (master) and no longer deadlocks;
  - a stop requested while the spider is opening no longer emits
    `engine_stopped` before `spider_opened`/`spider_closed`;
  - `_closewait` is fired after the STOPPED transition, not before: with a
    non-asyncio reactor, firing it resumes `start_async()` synchronously, so
    `crawl_async()` used to return with the engine still in STOPPING.
  - `start_async()` creates `_closewait` before sending `engine_started` and
    always awaits it, so neither it nor `crawl_async()` completes before the
    engine is STOPPED. Before the 2026-09-05 review pass it returned right
    after `_stop()` when the spider was CLOSING at that point (e.g. a
    CloseSpider-extension close scheduled while an `engine_started` handler
    was running) or OPENING, i.e. with the engine still STOPPING and the
    close still running.
  - a `Crawler.stop_async()` issued before the engine started (from a
    `spider_opened` handler, or Ctrl-C during a slow spider open) no longer
    makes the crawl run in full and then hang forever (also on master: it
    cleared `crawling`, so the spider-closed callback later did nothing and
    `engine.stop_async()` was never called). See "Integration" below.
  - the scheduler is built before the OPENING transition, so a failing
    `Scheduler.from_crawler()` is reported as is instead of being masked by
    `RuntimeError("Engine slot not assigned")` from the cleanup close (also
    masked on master).

### Deviations from the plan doc

- **No "started"/"stopped" events (plan §4.4), and no `_Event` class.** The
  plan asked for awaitable `wait_until_running()`/`wait_until_stopped()`
  methods. They were implemented first and then dropped:
  - `wait_until_stopped()` only existed because `stop_async()` cannot wait for
    a stop that is already under way. With the hand-off design above nothing
    needs it: `start_async()`/`Crawler.crawl_async()` complete when the engine
    has stopped, and `Crawler.stop_async()` still awaits the whole stop
    sequence whenever it is the call that starts it.
  - `wait_until_running()` had one real user, the reactorless
    `scrapy shell` init. It now waits for the `engine_started` signal with a
    local future instead (`scrapy/commands/shell.py`), which is what the shell
    actually needs; the engine gains no public API for it.
  - Both are footguns of the same family as the waits they replaced:
    `wait_until_running()` from an `engine_started` handler, or
    `wait_until_stopped()` from an `engine_stopped` handler, deadlocks.

  Cost of dropping them: there is no longer a deterministic "the engine has
  reached RUNNING" synchronization point. Waiting for `engine_started`
  resumes while the engine may still be STARTING (`send_catch_log_async()`
  suspends), which is fine for the shell but means tests that need a started
  engine use the `start_engine()` helper in `tests/test_engine_lifecycle.py`
  and avoid asserting RUNNING at that exact moment.
- **A close requested during OPENING does not abort the open mid-way**; it is
  recorded in `_pending_close_reason` and performed right after the open
  finishes. Aborting would run the close sequence over half-opened components
  and log spurious errors; completing keeps the `spider_opened` →
  `spider_closed` signal order. If `open_spider_async()` itself fails, the
  spider is marked OPEN and the pending close is performed before the
  exception is re-raised, so that a pending stop cannot be lost.

### Known trade-off

When a stop is requested while a spider close is already in flight,
`stop_async()` (and therefore `Crawler.stop_async()`) returns before the
engine has finished stopping — the in-flight close finishes the stop
afterwards. `CrawlerProcess.stop()` (Ctrl-C) can therefore stop the reactor a
little before the tail of that close has run. This matches master, where
`close_spider_async()` returns as soon as `_slot.closing` fires; the
alternative (waiting) is what deadlocks. `CrawlerProcess.join()`, i.e. the
normal end of a crawl, is unaffected, because it waits for `crawl_async()`.

### Integration

- `Crawler.stop_async()` routes on `engine.state` instead of `engine.running`:
  it stops a started engine, and on a never-started one it closes the spider
  (if any), so that the `start_async()` call that `crawl_async()` makes next
  finishes the shutdown right away. It still does nothing when `crawling` is
  false, which is what keeps `scrapy shell` alive when the MemoryUsage
  extension "stops the crawler" there
  (`test_memusage_limit_stops_crawler_without_spider` depends on it).
- The engine's spider-closed callback is `Crawler._spider_closed()` instead
  of `stop_async()`: it clears `crawling` and stops the engine whenever it was
  started, regardless of `crawling`, so that an earlier `stop_async()` can no
  longer disarm it (the hang above).
- `scrapy/commands/shell.py` replaces the "may wait until #6916" comment with
  a local future resolved by the `engine_started` signal; the shell.py
  architecture notes are updated. (The dead `KEEP_ALIVE` setting had already
  been dropped on master.) The
  `_start_request_processing` keyword stays private for now — the state
  machine models the shell path officially, and a public rename can be
  discussed separately.
- `docs/topics/api.rst` documents the new API (`versionadded:: 2.18`):
  `state`, `spider_state`, `EngineState`, `SpiderState`.

### Verification

Engine/crawler/shell/memusage/crawl suites pass on all three stacks (asyncio
reactor, default reactor, reactorless); `mypy scrapy` and `mypy tests` clean;
pylint 10/10; ruff check+format clean.

## Review pass after the rebase (2026-09-05)

The manual rebase over c285f4cb1 (CloseSpider during startup, shipped in
2.18.0) was checked hunk by hunk and is correct: the CloseSpider collection in
`open_spider_async()` is intact inside the new `except BaseException` wrapper,
and `Crawler.crawl()`/`crawl_async()` keep the `except CloseSpider` route to
`close_async(reason=...)`. The one rebase casualty was `docs/topics/api.rst`:
the `scheduler` member added by 53eb8d60b was dropped from the `:members:`
list; restored.

Behavior fixes made in this pass are listed under "Fixed along the way" and
"Integration" above (`start_async()` completing early, the pre-start
`Crawler.stop_async()` hang, the masked scheduler error). Simplifications:

- `_stop()` and `close_async()` no longer duplicate the "spider is OPENING →
  record a pending reason / CLOSING → return" logic; they call
  `close_spider_async()`, which already implements it. As a side effect a
  `close_async(reason=...)` issued while the spider is opening now uses that
  reason instead of a hard-coded `"shutdown"`.
- `start_async()` has a single exit through `await self._closewait`.
- `open_spider_async()` creates the slot outside the try block, so the
  "partially opened spider" handled by the except branch always has a slot.
- `versionadded`/`versionchanged` markers use the `VERSION` placeholder (the
  branch predates the 2.18.0 release).

Not changed, worth a decision before merging:

- c285f4cb1 makes `open_spider_async()` raise `CloseSpider` and lets the
  crawler close the spider. The `_pending_close_reason` mechanism could absorb
  it (record the reason, close at the end of the open, let `start_async()`
  finish the shutdown), which would remove the `except CloseSpider` branches
  from both crawler methods and make the shell path consistent. It would also
  make that crawl send `engine_started`/`engine_stopped`, as the race-2 path
  already does, so it is a (small) behavior change against 2.18.0; left alone.
- `Crawler.stop_async()` is a no-op in `scrapy shell` (which never sets
  `crawling`), so the MemoryUsage extension's "Shutting down Scrapy" there
  shuts nothing down. Pre-existing; the memusage test pins it.
- The "known trade-off" above (Ctrl-C during an in-flight close stops the
  reactor before the close finishes) could be addressed in `CrawlerProcess`
  by waiting for the crawl Deferreds instead of the `stop_async()` results;
  that is #7455 territory.

Verification of this pass: the engine, crawler, shell, closespider, memusage
and engine-loop test files pass on all three stacks (asyncio reactor, default
reactor, reactorless); the full `tests/` run on the asyncio stack has only
the mitmproxy failures (no `mitmdump` locally); `mypy scrapy` + the touched
test files, pylint 10/10 and pre-commit (ruff check/format) are clean.

## Merging in stages

The branch splits into four ordered PRs; the lifecycle test file partitions
along the same lines, so the split is mostly mechanical. The hard constraint
is that the stop and close-spider rework must land together (PR 2).

Standalone fixes that need no state machine and could go first, in any order,
even against master today (implemented as three commits on branch
`engine-standalone-fixes`, worktree `../scrapy-standalone-fixes`, 2026-09-14;
this branch should be rebased on them once they land, dropping the
corresponding hunks and the `test_scheduler_creation_error` /
`test_spiderless_stop_closes_downloader` duplicates in the lifecycle tests):

- build the scheduler before setting `engine.spider` in `open_spider_async()`
  (a failing `from_crawler()` is otherwise masked by "Engine slot not
  assigned");
- close the downloader on a spider-less stop (the shell leak);
- the reactorless shell waiting for `engine_started` with a local future.

1. **Foundation, no behavior change.** Enums, transition tables,
   `_transition_to()` with log-only validation, `state`/`spider_state`,
   transitions recorded inside the existing methods, `running` derived from
   state with the deprecated setter, `_starting`/`_stopping` replaced by state
   checks with the same semantics (the band-aid becomes "return if no longer
   STARTING"). Docs for the enums and properties, incl. the restored
   `scheduler` member. Tests: state progression, shell signal order,
   deprecation, the misuse tests (double open, stop not started, close never
   opened).
2. **Stop and close-spider, the real behavior change.** Idempotent
   `stop_async()`/`close_spider_async()`, the never-wait rule with
   `_pending_stop`, `_stop()`/`_finish_stop()`, `engine_stopped` after
   `spider_closed`, `_closewait` fired after the STOPPED transition, the
   "engine already stopped" check in `open_spider_async()`,
   `Crawler.stop_async()` routing on state, and the #7455 stop-mode
   plumbing that is on master already (`mode` keywords, escalation, the
   `_fast_stop_downloader()` hooks, the process-level `join()` wait; see
   below). Carries the `versionchanged` notes. Tests: idempotent stop, double
   close, close while closing, stop while closing, stop from a
   `spider_closed` handler, open after stop, the rewritten re-entrant stop
   tests.
3. **Close during open.** `_pending_close_reason`,
   `_close_spider_if_pending()`, the `except BaseException` branch that marks
   a failed open as OPEN and honours a pending close. Tests: close and stop
   during open, scheduler creation error. Can also come after PR 4.
4. **Start and crawler.** Band-aid removed, closed/closing spider detected
   in `start_async()`, `_closewait` created before `engine_started`,
   spider-less start defined, `close_async()` routed on state,
   `Crawler._spider_closed()` and the pre-start stop handling; the shell.py
   comment update. Tests: close from an `engine_started` handler, close
   before start, close in progress at start, spider-less start, the two
   crawler-stop tests, `TestCloseAsync`.

Intermediate states are safe but not fully fixed: after PR 2 alone, a close
that completes before `start_async()` still hits master's silent half-start;
after PR 3 alone, a `close_async()` during open still ends the crawl without
`engine_stopped`. Neither is a regression against master. The real work of
the split is writing the intermediate versions of `start_async()` and
`stop_async()` for PRs 1 and 2 and re-running the three stacks per stage.

## Relation to PR #7455 (fast crawler stops)

Status: merged into master as a648c05da on 2026-09-14; the branch was rebased
onto it on 2026-09-14 (conflicts in `scrapy/core/engine.py` and
`scrapy/crawler.py` only). The stop *mode* is orthogonal state ("how to
stop"), not lifecycle state ("where in the lifecycle"), so the state machine
did not change; #7455's hunks were placed as follows.

- Absorbed mechanically: `_stop_mode`/`_downloader_fast_stopped` are plain
  attributes next to the enums; `_normalize_stop_mode`/`_max_stop_mode` run
  at the top of `stop_async()`/`close_spider_async()` (after the CREATED /
  NONE misuse checks, so that `stop_async(mode="force")` on a never-started
  engine still raises the `ValueError`); the `mode` keywords are on the new
  signatures; `_fast_stop_downloader()` and the fast-cancel filter in
  `_handle_downloader_output()` are unchanged.
- The `_fast_stop_downloader()` hooks: `close_spider_async()` OPEN branch
  (before `self._slot.close()`, as in #7455) and CLOSING early-return branch
  (the old "slot already closing" branch); `stop_async()`'s STOPPING/STOPPED
  early return escalates and, if the mode is now fast while the spider is OPEN
  or CLOSING, runs the fast stop inline before returning. Nothing in the
  OPENING branch: a pending close consumed by `open_spider_async()` reads the
  engine-level mode when it runs. `_stop()` calls
  `close_spider_async(reason="shutdown")` without a mode: the escalation is
  idempotent and the engine-level mode already holds.
- **Re-entrant `stop_async()`.** #7455 escalated, fast-stopped the
  downloader, then awaited `_closewait`; this branch returns right after the
  fast stop (never-wait rule). The wait moved to the process level, where it
  is safe by construction: `CrawlerProcessBase._graceful_stop_reactor()` and
  `_fast_stop_reactor()` yield a new abstract `_join_dfd()` (`join()`, or
  `deferred_from_coro(join())` for `AsyncCrawlerProcess`) after
  `_stop_dfd(mode)` and before `_stop_reactor()`; the third signal (kill)
  still stops the reactor via `callLater`. The reactorless path already
  waited on `join()` (`_shutdown_reactorless()`, main task = `join()`).
  This is what keeps `test_shutdown_fast_no_stop` passing ("dropping
  downloader requests" must be followed by "Spider closed (shutdown)"), and it
  also closes the "known trade-off" above for plain Ctrl-C: the reactor no
  longer stops before a close's tail (feed export uploads, `spider_closed`
  handlers) has run.
- `Crawler.stop_async(mode)` keeps #7455's mode and force-callback plumbing
  and its "fast or force proceed even when `crawling` is false" rule, drops
  the `graceful and not engine.running` early return and the
  `except RuntimeError ... if str(exc) != "Engine not running"` string-match
  in favour of routing on `engine.state` (CREATED → close the spider if any,
  with the mode; else `engine.stop_async(mode=mode)`), and the spider-closed
  callback is `_spider_closed()` (mode-less: the escalated mode is
  engine-level).
- Tests adapted: `test_stop_async_reentrant_fast_waits_for_closewait` →
  `test_stop_async_reentrant_fast_drops_downloads` (STOPPING/CLOSING via
  `engine.state`, asserts the inline fast stop and no wait);
  `test_stop_async_reentrant_graceful_without_spider_or_closewait` →
  `test_stop_async_reentrant_graceful_is_noop`;
  `test_crawler_graceful_stop_non_running_engine_is_noop` →
  `test_crawler_graceful_stop_created_engine_is_noop` (DummyEngine exposes
  `state`/`spider_state`); the other DummyEngine tests gained `state =
  EngineState.RUNNING`; `test_crawler_stop_async_ignores_engine_not_running_runtime_error`
  deleted (the workaround it pinned is gone). #7455's
  `tests/test_engine_close_spider.py` fast-close tests and the subprocess
  shutdown tests pass unchanged.

Verification of the rebase (2026-09-14): engine, crawler, crawler-subprocess,
core-downloader, shell, closespider, memusage and engine-loop test files pass
on all three stacks; the full `tests/` run on the asyncio stack has only the
mitmproxy failures; `mypy scrapy` + the touched test files, pylint 10/10 and
pre-commit are clean. ("Unclosed client session" messages in reactorless runs
come from master's aiohttp handler, not from this branch.)
