# Dataset Request Desk

An internal platform for a robotics data collection company. Clients submit
dataset requests ("200 episodes of a robot arm picking cups, by 14 November"),
operations staff fulfil them by assigning recorded episodes, and the client
accepts or rejects the delivery. It replaces the spreadsheet this was tracked
in.

Design decisions, trade-offs and what I left out are in **[NOTES.md](NOTES.md)**.

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL 16
- **Frontend:** React 18 + Vite (TypeScript), served by nginx
- **Everything runs with one command**

---

## Running it

Requires Docker with Compose. From a clean clone:

```bash
docker compose up --build
```

That brings up Postgres, runs the migrations, seeds the known robots and the
user accounts, imports the sample episode CSV, starts the API, and builds and
serves the frontend.

| What | Where |
|---|---|
| Web UI | <http://localhost:3000> |
| API | <http://localhost:8000> |
| API docs (OpenAPI) | <http://localhost:8000/api/docs> |
| Health check | <http://localhost:8000/api/health> |

A `Makefile` wraps the common commands:

```bash
make up      # build and start everything
make test    # run the test suite
make logs    # tail the API logs
make psql    # open psql against the database
make down    # stop and delete the data volume
```

### Seed logins

Created automatically on startup from `seed/users.json`. Passwords are stored
as bcrypt hashes; the plaintext below exists only in the seed file.

| Email | Password | Role |
|---|---|---|
| `admin@example.com` | `admin123` | admin |
| `ops1@example.com` | `ops123` | operator |
| `ops2@example.com` | `ops123` | operator |
| `client-a@example.com` | `client123` | client (Acme Robotics) |
| `client-b@example.com` | `client123` | client (Beta Labs) |

Log in as `client-a` and `client-b` in two browsers to see that neither can
see the other's requests.

---

## Running the tests

```bash
make test
```

or directly:

```bash
docker compose run --rm \
  -e DATABASE_URL=postgresql+psycopg://drd:drd@db:5432/drd_test \
  api pytest -q
```

Tests run against a real Postgres database (`drd_test`, created on first run),
with the schema built by running the Alembic migrations — so a migration that
has drifted from the models fails the suite. Each test runs in a transaction
that is rolled back afterwards.

CI runs on every push (`.github/workflows/ci.yml`), in three jobs: the API
suite against a real Postgres, a type-check and build of the frontend, and a
clean-clone `docker compose up` that waits for `/api/health` to report ok.
That last job runs on a different architecture and libc to the machine this
was developed on, which is the class of breakage a local build cannot catch.

The suite concentrates on the rules that would actually hurt if they broke:

| File | Covers |
|---|---|
| `tests/test_authorization.py` | every endpoint rejects anonymous callers; a client cannot see or touch another client's request; clients cannot reach operator endpoints |
| `tests/test_transitions.py` | every valid path through the workflow, every invalid edge, which role owns which edge, and the "enough episodes to deliver" precondition |
| `tests/test_assignments.py` | good/usable only, one request per episode (including at the database level), when assignment is allowed |
| `tests/test_import.py` | idempotency on re-import, every cleaning rule, every rejection reason |
| `tests/test_analytics.py` | grouping, inclusive date boundaries, median interpolation |
| `tests/test_auth.py` | login, password hashing, token forgery, deactivation |
| `tests/test_users.py` | only admins manage accounts; role changes take effect at once; an admin cannot lock everyone out |

---

## Importing episodes

The import is **idempotent**: running it twice over the same file produces the
same database, and reports the second run as unchanged rather than creating
duplicates. It reports what it imported, what it skipped and why.

From the command line:

```bash
docker compose exec api python -m app.cli import-episodes /seed/episodes.csv
```

Or upload a file as an operator:

```bash
curl -X POST http://localhost:8000/api/episodes/import \
  -H "Authorization: Bearer $TOKEN" \
  -F file=@seed/episodes.csv
```

Both return a report. Against the supplied `seed/episodes.csv`:

```json
{
  "source_file": "episodes.csv",
  "rows_read": 190,
  "created": 173,
  "updated": 0,
  "unchanged": 0,
  "skipped": 16,
  "reasons": {
    "blank_row": 1,
    "conflicting_duplicate_in_file": 2,
    "duplicate_in_file": 2,
    "future_recorded_at": 1,
    "implausible_duration": 1,
    "invalid_duration": 2,
    "invalid_quality": 1,
    "invalid_recorded_at": 1,
    "malformed_row": 1,
    "missing_duration": 1,
    "missing_episode_id": 1,
    "missing_quality": 1,
    "missing_robot_id": 1,
    "unknown_robot": 1
  },
  "sample_problems": [
    { "line": 162, "episode_id": "EP-00024", "reason": "unknown_robot",
      "detail": "'arm-99' is not a known robot" },
    { "line": 168, "episode_id": "EP-00011", "reason": "conflicting_duplicate_in_file",
      "detail": "contradicts an earlier row in this file; later row kept" }
  ]
}
```

Every row is accounted for: 173 imported + 16 skipped + 1 blank line = 190 read.
Running it a second time reports `created: 0, updated: 0, unchanged: 173`.

Every decision about how a malformed row is handled is listed in
[NOTES.md](NOTES.md).

To test at volume, `seed/generate_episodes.py` produces a large clean file:

```bash
python3 seed/generate_episodes.py 200000 > episodes_large.csv
```

---

## The API

All endpoints require authentication except `/api/health` and
`/api/auth/login`. Authorization is enforced by dependencies on the route —
not in the UI.

| Method | Path | Who |
|---|---|---|
| `POST` | `/api/auth/login` | anyone |
| `POST` | `/api/auth/logout` | authenticated |
| `GET` | `/api/auth/me` | authenticated |
| `POST` | `/api/requests` | client |
| `GET` | `/api/requests` | client (own only), operator, admin (all) |
| `GET` | `/api/requests/{id}` | as above |
| `POST` | `/api/requests/{id}/status` | depends on the transition |
| `POST` | `/api/requests/{id}/assignments` | operator, admin |
| `DELETE` | `/api/requests/{id}/assignments/{episode_id}` | operator, admin |
| `GET` | `/api/episodes` | operator, admin |
| `POST` | `/api/episodes/import` | operator, admin |
| `GET` | `/api/analytics?from=&to=` | operator, admin |
| `GET` | `/api/users` | admin |
| `POST` | `/api/users` | admin |
| `PATCH` | `/api/users/{id}` | admin |
| `GET` | `/api/health` | anyone |

Authentication accepts either transport: the browser uses an HttpOnly
`SameSite=Lax` cookie set at login, and `Authorization: Bearer <token>` also
works for scripts and curl.

### Logging

One structured JSON line per request on stdout, carrying the method, path,
status, duration and the authenticated user id:

```json
{"ts":"2026-10-02T14:31:07.882Z","level":"INFO","logger":"app.access",
 "message":"request","request_id":"0b0f…","method":"POST",
 "path":"/api/requests/12/status","status":200,"duration_ms":18.4,"user_id":3}
```

---

## Analytics, and what happens at 5 million episodes

`GET /api/analytics?from=&to=` returns, for a date range:

- episodes recorded per day, per robot;
- a count of requests by status, and the median hours from `submitted` to
  `delivered`;
- the top 5 task names by number of `good` episodes.

**All four are computed by Postgres.** Nothing loads rows into Python to count
them — the handler receives a handful of aggregate rows. The queries are in
`api/app/services/analytics.py`.

At the seed data's scale none of this matters. At 5 million episodes:

**Episodes per day, per robot.** A `GROUP BY` over a date range, served by
`ix_episodes_robot_recorded_at`. The cost scales with the number of rows *in
the window*, not with the table, so a 30-day window over 5M rows touches maybe
40k rows and stays in the tens of milliseconds. A query with no upper bound on
the range would degrade into a full scan, which is why the endpoint caps the
window at 366 days and defaults to 30.

**Top 5 tasks by good episodes.** There is a partial index on
`(task_name, recorded_at) WHERE quality = 'good'`, so the `bad` and `usable`
rows are never visited. With roughly 60% of episodes good, that index covers
~3M rows at 5M total, and the aggregate is an index-only scan over the slice
inside the date range. This is the query I would watch first: if the result
is wanted over *all time* rather than a window, it should become a materialised
view refreshed on a schedule, because no index makes a 3M-row aggregate fast
enough for a page load.

**Requests by status.** Bounded by the number of requests, not episodes.
Even a busy desk produces thousands of requests a year, so this stays trivial
regardless of how many episodes exist.

**Median submitted → delivered.** The one that needs care. It aggregates
`request_status_events` down to one row per request, then applies
`percentile_cont(0.5)`. A percentile cannot be computed incrementally — Postgres
must sort the whole set — so the cost grows with the number of *delivered
requests in the window*, which is small. But the inner aggregate currently
groups the whole `request_status_events` table before the date filter is
applied, which is fine at thousands of requests and wrong at millions. The fix,
if the request table ever got large, is to denormalise: store `submitted_at`
and `first_delivered_at` directly on `requests`, written at transition time,
and index `first_delivered_at`. The events table stays the audit record; the
two timestamps become the query surface. I did not do that here because it
duplicates state, and at this scale correctness-by-single-source-of-truth is
worth more than a query plan that is already fast.

More on scale, including what breaks at 10× users, is in
[NOTES.md](NOTES.md).

---

## Project layout

```
api/
  app/
    main.py              FastAPI app, request logging, error handling, /health
    config.py            settings from the environment
    db.py                engine and session
    models.py            SQLAlchemy models and database constraints
    schemas.py           Pydantic request/response validation
    security.py          bcrypt hashing, JWT issuing and verification
    deps.py              authentication and role dependencies
    cli.py               `seed` and `import-episodes` commands
    errors.py            domain errors, mapped to HTTP status codes
    routers/             HTTP layer: auth, requests, episodes, analytics
    services/
      importer.py        CSV cleaning, deduplication, idempotent upsert
      transitions.py     the status workflow and who owns each edge
      assignments.py     episode assignment rules
      analytics.py       the aggregate queries
  alembic/               migrations
  tests/
web/
  src/                   React SPA
  nginx.conf             serves the SPA, proxies /api to the API
seed/                    supplied sample data (unchanged)
```
