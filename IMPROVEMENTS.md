# atlasserver — improvement backlog

Performance, testing, and feature improvements identified from a read of `main` at 98312ac.
Nothing here was a bug report — recent PRs (#103, #105, #106) already swept for correctness —
these were throughput, coverage, and capability gaps.

**Status: everything below is implemented, except F4 (estimated wait time), which was dropped
deliberately — see the note on that item.** Each entry keeps its original description so the
reasoning stays readable, with a short note on how it was resolved.

## What landed

| Item | Resolution |
|---|---|
| P1 indexes | Five composite indexes via `Task.Meta.indexes`, covering the queue scan, task list, image-request lookup and maintenance sweeps. |
| P2 serializer N+1 | `_imagerequest_task()` memoised and prefetched (`Task.prefetch_imagerequests()`), the global queue offset computed once per response, `select_related("parent_task")`. A 6-task page went from a per-task cost to **7 queries flat**. |
| P3 ETag | Rewritten to aggregate over the requesting user, checked *before* pagination, and quoted per RFC 7232. The React client now stores it and sends `If-None-Match`. An unchanged page: **4 queries, no serialisation, no body**. |
| P4 runner polling | Cancellation check throttled 1 s → 15 s; main loop backs off 0.5 s → 5 s once the queue is empty. |
| P5 queue positions | Single `bulk_update` instead of one `UPDATE` per task, still under the same lock. |
| P6 connections | `CONN_MAX_AGE=60` + `CONN_HEALTH_CHECKS`. |
| P7 filesystem stats | `localresultfile()` memoised; zip/stack URLs skip the stat for unfinished tasks. |
| P8 counts | The page count is no longer taken when pagination is disabled. |
| P9 maintenance | Batched: file cleanup per batch, then one `UPDATE`/`DELETE` per batch instead of a full-row `save()` per task. |
| P10 plot script | mtime+size keyed cache, so a redeploy still takes effect immediately. |
| T1 query counts | `TaskListQueryCountTests` pins the budget and asserts the count does not grow with page size. |
| T2 coverage | `coverage run` + report in CI; config in `pyproject.toml`. Currently 55%. |
| T3 runner tests | Command construction extracted into `build_fp_command` / `build_ssostack_command` / `remote_result_filename` and tested; plus `run_rsync` timeout and `remove_old_tasks`. |
| T4 concurrency | `QueuePositionConcurrencyTests` runs four concurrent recalculations and asserts no duplicate positions. Skipped on SQLite (no `SELECT FOR UPDATE`); runs on MySQL. |
| T5 frontend tests | The new conditional-request logic extracted to `pollcache.js` and tested with `node --test`. CI also rebuilds the bundles and fails if the committed ones are stale. |
| T6 local tests | Committed `atlasserver/settings_test.py`, SQLite by default, `ATLASSERVER_TEST_DB=mysql` for CI. The `cat settings_test.txt >> settings.py` step is gone. |
| F1 webhooks | Optional `callback_url`, POSTed on completion. SSRF-guarded: https only, no credentials in the URL, resolved address must be global, no redirects followed, short timeout, validated at submit **and** at send. |
| F2 OpenAPI | `drf-spectacular` at `/api/schema/` with Swagger UI at `/api/docs/`. |
| F3 queue endpoint | `/queuepositions.json` — two queries, a few hundred bytes. |
| F4 wait-time estimate | **Not done, by decision.** Under a round-robin queue another user's submission changes a task's start time significantly, so any estimate shown would be misleading rather than useful. |
| F5 health signal | The runner writes an atomic status snapshot; `/taskrunnerstatus.json` serves it and returns 503 when stale. |

## The shape of the problem

The queue page polls the task list **every 2 seconds** per open tab
([tasklist.jsx:588](static/js/queuepage/src/tasklist.jsx#L588)), and the task runner polls the
database **twice a second** plus **once a second per running task**. Almost every one of those
queries is an unindexed scan of the `Task` table. Nearly all the performance items below are
different faces of that one fact, which is why P1–P4 are worth doing together.

---

## Performance

### P1. `Task` has no database indexes at all

`Task` has no `Meta` class and no `db_index=True` on any field; a grep across all 59 migrations
finds no `AddIndex`, `indexes`, or `db_index`. Only the implicit primary key and the two FK indexes
(`user_id`, `parent_task_id`) exist.

Every hot query filters or sorts on unindexed columns:

| Query shape | Where | Frequency |
|---|---|---|
| `finishtimestamp IS NULL AND is_archived=0` ordered by `queuepos_relative` | [main.py:684](atlasserver/taskrunner/main.py#L684) | 2×/sec forever |
| `is_archived=0 AND user_id=?` ordered by `-timestamp, -id` | [views.py:312](atlasserver/forcephot/views.py#L312) | every poll, every user |
| `MAX(timestamp), MAX(starttimestamp), MAX(finishtimestamp), MAX(task_modified_datetime)` — whole table | [views.py:160](atlasserver/forcephot/views.py#L160) | every list *and* detail request |
| `MIN(queuepos_relative)` where unfinished | [models.py:186](atlasserver/forcephot/models.py#L186) | once **per task** serialised |
| `parent_task_id=? AND is_archived=0` | [models.py:165](atlasserver/forcephot/models.py#L165) | 3× per task serialised |
| `finishtimestamp < ?` (+ `request_type`, `is_archived`, `from_api`) | [main.py:591](atlasserver/taskrunner/main.py#L591) | 6 sweeps hourly |

Suggested composite indexes, in rough value order:

```python
class Meta:
    indexes = [
        models.Index(fields=["is_archived", "user_id", "-timestamp", "-id"]),
        models.Index(fields=["finishtimestamp", "is_archived", "queuepos_relative"]),
        models.Index(fields=["parent_task_id", "is_archived", "id"]),
        models.Index(fields=["request_type", "finishtimestamp"]),
        models.Index(fields=["timestamp"]),
    ]
```

Highest value-to-effort item on the list: one migration, no logic change. Worth running `EXPLAIN`
on the production table first to size the win and confirm the column orders.

### P2. 3–5 queries per task in the serializer (N+1)

Serialising one `Task` currently issues, unconditionally:

- `imagerequest_task_id` → [`_imagerequest_task()`](atlasserver/forcephot/models.py#L159) — 1 query
- `imagerequest_finished` → `_imagerequest_task()` **again** — 1 query
- [`get_imagerequest_url`](atlasserver/forcephot/serializers.py#L59) → reads
  `obj.imagerequest_task_id`, a **third** identical query

plus, conditionally:

- `queuepos` → a `MIN()` aggregate, for every unfinished task
  ([models.py:186](atlasserver/forcephot/models.py#L186))
- `get_parent_task_url` → `Task.objects.get(...)` for any task with a parent
  ([serializers.py:36](atlasserver/forcephot/serializers.py#L36))

At `PAGE_SIZE = 6` that is **18–30 queries per list response**, every 2 seconds, per open tab.

Fixes, roughly independent:

- Cache `_imagerequest_task()` on the instance (`functools.cached_property` or a `_cached`
  attribute) — collapses 3 queries to 1 with a two-line change.
- `minqueuepos` in `queuepos` is a **global** aggregate, identical for every task in the response,
  yet recomputed per task. Compute it once per request and pass it through serializer context.
- `prefetch_related` the `imagerequest` reverse relation and `select_related("parent_task")` on the
  viewset queryset ([views.py:202](atlasserver/forcephot/views.py#L202)) to make the whole page one
  or two queries.

### P3. The ETag is expensive, global, and the frontend never uses it

[`get_tasklist_etag`](atlasserver/forcephot/views.py#L151) runs four `MAX()` aggregates over the
**entire** `Task` table (all users, unindexed → full scan) on every list and every detail request.

Two problems:

1. **The React client never sends it back.** The line that would capture it is commented out —
   [`// etag = response.headers.get('ETag');`](static/js/queuepage/src/tasklist.jsx#L520) — and no
   `If-None-Match` header is ever set. For the frontend the whole mechanism is pure cost, and the
   `HttpResponseNotModified` branch is unreachable.
2. **It's global, not per-user.** Any other user's task changing invalidates every user's ETag, so
   even a client that *did* send the header would rarely get a 304. The minute-granularity
   timestamp component ([views.py:156](atlasserver/forcephot/views.py#L156)) caps the hit rate at
   60 s anyway.

Either finish it (scope the aggregate to `user_id`, have the client send `If-None-Match`, drop the
timestamp component — this makes the 2 s poll nearly free) or delete it. Finishing it is the bigger
win and pairs naturally with P1.

### P4. The task runner polls the database ~18×/second

Two separate loops:

- **Main loop** ([main.py:665–714](atlasserver/taskrunner/main.py#L665)): `sleep(0.5)`, then
  `queuedtasks.count()` and `.exclude(...).first()` — ~2 queries/sec, ~170k/day, each an unindexed
  scan.
- **Per-task cancellation check** ([main.py:272–285](atlasserver/taskrunner/main.py#L272)):
  `proc.communicate(timeout=1)` then `task_exists(task.id)` — **one query per second per running
  task**. With `numslots = 16` that is up to 16 queries/sec, and a single 4-hour task
  (`TASK_MAXTIME_SECONDS`) issues **14,400** queries just to ask whether it was cancelled.

Raising the cancellation check to every ~15 s (keep the 1 s `communicate` timeout for the progress
log, only hit the DB every Nth iteration) cuts that by 15× with no behavioural change a user could
notice. Backing the main loop off when the queue is empty — it already tracks `printedwaiting` —
gets most of the rest.

### P5. `calculate_queue_positions` is O(N) UPDATEs under a global lock

[views.py:67](atlasserver/forcephot/views.py#L67) holds `select_for_update()` over **every** queued
row, then issues one `Task.objects.filter(id=...).update(...)` per task
([views.py:129](atlasserver/forcephot/views.py#L129)) — N round-trips while all queued rows are
locked. It runs on every create and every delete.

`MAX_USER_TASKS = 500`, so a busy queue means hundreds of UPDATEs per submission with concurrent
submitters serialised behind the lock. The `bulk_update` alternative is already sketched in the
commented-out "method 2" lines ([views.py:98](atlasserver/forcephot/views.py#L98),
[148](atlasserver/forcephot/views.py#L148)) — finishing it collapses N statements into one.

The lock itself is deliberate and correct (the comment explains the duplicate-position race);
shortening the time it is held is the goal, not removing it.

### P6. No persistent database connections

`DATABASES` ([settings.py:126](atlasserver/settings.py#L126)) sets no `CONN_MAX_AGE`, so it
defaults to `0` — Django opens and tears down a MySQL connection on **every request**, including
all those 2-second polls. Adding `"CONN_MAX_AGE": 60, "CONN_HEALTH_CHECKS": True` is a two-line
change worth a few ms on every request. Check it interacts sanely with the mod_wsgi process model
first.

### P7. 2–5 filesystem `stat()` calls per serialised task

Each task triggers: `localresultfile()` twice (once from
[`get_result_url`](atlasserver/forcephot/serializers.py#L25), again from
[`get_pdfplot_url`](atlasserver/forcephot/serializers.py#L46)), plus
`localresultpreviewimagefile`, `localresultimagezipfile`, and `localresultimagestackfile`. The last
two have no `finishtimestamp` guard, so they stat even for queued tasks that cannot possibly have
files.

Cheap on a local SSD; not cheap if `STATIC_ROOT` is ever on network storage. Memoising
`localresultfile()` per instance and adding the `finishtimestamp` guard to the zip/stack properties
removes most of them.

### P8. Redundant `COUNT(*)` per page

[`paginate_queryset`](atlasserver/forcephot/pagination.py#L35) runs `queryset.count()`, then a
second filtered `.count()` for `pagefirsttaskposition`
([pagination.py:98](atlasserver/forcephot/pagination.py#L98) or
[106](atlasserver/forcephot/pagination.py#L106)); the HTML path adds a third
([views.py:325](atlasserver/forcephot/views.py#L325)). Three counts on unindexed columns, every
poll. P1 makes these cheap; caching `taskcount` briefly per user would make them nearly free.

### P9. Hourly maintenance sweep deletes row-by-row

[`remove_old_tasks`](atlasserver/taskrunner/main.py#L610) loops
`for task in matchingtasks: task.delete()`. Each `Task.delete()`
([models.py:216](atlasserver/forcephot/models.py#L216)) issues a query for `imagerequest_task_id`,
several `unlink()` calls, and — for finished tasks — a full-row `save()`. Six sweeps run hourly,
the widest covering 183 days. On a large archive this is a long serial burst against the same table
the runner and web server are hammering.

Batching (`bulk_update(is_archived=True)` after collecting file paths) and/or moving the sweep off
the hot hour would smooth it out.

### P10. `resultplotdatajs` re-reads the plot script from disk on every request

[views.py:871](atlasserver/forcephot/views.py#L871) reads `lightcurveplotly.min.js` from
`STATIC_ROOT` on every call. The read is deliberately outside the cache so a redeploy takes effect
immediately (the comment says so), but an mtime-keyed in-process cache would keep that property
without the per-request read. Minor — listed for completeness.

---

## Testing

### T1. No `assertNumQueries` regression tests

Nothing stops P2 from regressing the moment someone adds a `SerializerMethodField`. A single test
asserting the query count for a 6-task list response would pin it — and is the natural companion to
doing P1–P3 at all.

### T2. No coverage measurement in CI

[.github/workflows/test.yml](.github/workflows/test.yml) runs `./manage.py test` with no coverage
step, so there is no signal on what the 704 lines of tests actually reach. `coverage run
manage.py test` plus a summary in the job log is a small addition.

### T3. The task runner is largely untested

`tests.py` covers `send_email_if_needed` and `remove_task_resultfiles`. Untested: `runtask` (the
ssh command construction — including the `TEST_USERS` `tdo=1` branch and the SSOSTACK quoting that
needed a comment to get right), `do_task`, `remove_old_tasks`, `do_maintenance`, and `run_rsync`'s
timeout path. `runtask`'s command-building is pure string assembly and could be tested directly
without any ssh.

### T4. No concurrency test for queue positions

`calculate_queue_positions` has one happy-path test
([tests.py:37](atlasserver/forcephot/tests.py#L37)), but the `select_for_update` exists
specifically because concurrent recalculation produced duplicate positions — the scenario the lock
was added for is not covered. `TransactionTestCase` with two threads would pin it, and would be a
prerequisite for touching P5 safely.

### T5. No frontend tests

~1,200 lines of JSX across `tasklist.jsx`, `newrequest.jsx`, and `lightcurveplotly.js` with no test
setup, built by a hand-rolled `build.sh`. The Python side already resorts to asserting on JSX
source text ([tests.py:388](atlasserver/forcephot/tests.py#L388) greps `tasklist.jsx` for a URL
string), which is a sign the seam is in the wrong place.

### T6. Local test runs need a manual workaround

`DATABASES` is hardcoded to MySQL, so `manage.py test` fails on a machine with no MySQL and needs a
throwaway SQLite settings module. A committed `settings_test.py` (or `DATABASES` reading an env var
with a SQLite default) would remove that friction — and CI's `cat settings_test.txt >> settings.py`
append is itself a slightly fragile pattern.

---

## Features

### F1. API users cannot be notified, so they must poll

[`send_email_if_needed`](atlasserver/taskrunner/main.py#L405) returns early for `from_api` tasks, so
the only way an API client learns a task finished is to poll — which is exactly what
[apiexample.py](static/apiexample.py) demonstrates. An optional webhook callback URL (or opt-in API
email) would remove a meaningful share of total request volume. This is a feature that is also a
performance item.

### F2. No machine-readable API schema

The API is documented only by hand in `apiguide.html` — enough of a maintenance risk that there's a
test asserting its code blocks still parse ([tests.py:602](atlasserver/forcephot/tests.py#L602)).
`drf-spectacular` would generate an OpenAPI schema from the existing serializer, giving client
generation and a browsable schema for free.

### F3. No cheap "where am I in the queue" endpoint

The queue page re-fetches the full serialised task list every 2 s largely to update queue positions.
A small endpoint returning just `{taskid: queuepos}` for the current user would let the frontend
poll something ~100× cheaper, and would pair well with P3.

### F4. No estimated wait time — deliberately not implemented

`statsshortterm` already computes mean wait and run times
([views.py:658](atlasserver/forcephot/views.py#L658)), so a per-task ETA of
`queuepos × mean runtime / slots` would have been easy to add.

It was rejected: the queue is round robin, so another user submitting work re-interleaves the
queue and can move a task's start time substantially after the estimate was shown. A number that
moves around for reasons the user cannot see is worse than no number, so the queue position is
left to speak for itself.

### F5. No task-runner health signal

Nothing external can tell whether `atlastaskrunner` is alive, how many of the 16 slots are busy, or
whether the queue is stalled — the only signal is the log files under `taskrunner/logs/`. A status
file or small admin endpoint (slots busy, oldest queued task age, last maintenance run) would make
stalls visible.

---

## Verifying

- Tests: `DJANGO_SETTINGS_MODULE=atlasserver.settings_test ./manage.py test` (SQLite by default;
  set `ATLASSERVER_TEST_DB=mysql` to use the production engine, as CI does).
- Frontend tests: `npm test` in `static/js/queuepage`.
- Lint gates: `ruff check`, `ruff format --check`, `mypy`, `pylint atlasserver`.
- Query counts: `assertNumQueries` or `CaptureQueriesContext` around a `reverse("task-list")` GET,
  before and after.
- Index effect: `EXPLAIN` the queue-scan and task-list queries against the production table.
