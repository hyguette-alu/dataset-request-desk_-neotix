# Notes

## 1. Design

### Data model

```
users ──< requests ──< assignments >── episodes >── robots
             │
             └──< request_status_events
```

| Table | Why it exists |
|---|---|
| `users` | email (unique, lower-cased), bcrypt hash, role, `is_active` |
| `robots` | reference table of known robots, seeded from `seed/README.md` |
| `episodes` | `episode_id` from the source system **is** the primary key |
| `requests` | belongs to a client; carries the *current* status |
| `request_status_events` | append-only history of every status change |
| `assignments` | an episode attached to a request; `UNIQUE(episode_id)` |
| `import_runs` | one row per CSV import, with its report as JSONB |

Accounts are deactivated, never deleted. `users.id` is referenced by
`requests.client_id` and `request_status_events.actor_id` with ON DELETE
RESTRICT, so deleting a user would either orphan requests or rewrite history
that is supposed to be immutable. Two guards stop an admin creating a
one-way door: you cannot deactivate or demote your own account, and the last
active admin cannot be removed by anyone.

### Where state lives

All of it is in Postgres. There is no cache, no queue and no in-process state,
so any number of API containers can run behind a load balancer without
coordinating. The only client-side state is the session cookie.

Domain rules are pushed into database constraints wherever a constraint can
express them, so they survive a bug in application code or a direct write:

- `UNIQUE(assignments.episode_id)` — an episode belongs to at most one request
- `FK episodes.robot_id → robots.id` — unknown robots cannot be imported
- `CHECK (duration_seconds > 0 AND <= 86400)` — plausible durations
- `CHECK (episodes_requested > 0)`

Two rules could not be: *only good/usable episodes may be assigned* and *a
request cannot be delivered below its episode count*. Both are cross-table
conditions that would need triggers, which is more machinery than this
warrants. They live in `app/services/` and are covered by tests.

### The three hardest decisions

**1. Current status as a column, history as a table.** I could have derived
`requests.status` from the last event, which has one source of truth. I chose
to store the current status *and* append an event, because every list query
filters on status and deriving it would mean a window function on every page
load. The duplication is contained: `apply_transition()` is the only code that
writes either, so they cannot diverge. The events table is what the median
metric is computed from — the audit trail is load-bearing, not decoration.

**2. Conflicting duplicates in the CSV: last row wins, loudly.** `EP-00011`
appears twice in the export with different qualities — `bad` on line 3, `good`
on line 168. Three options: reject both (destroys data over a source-system
problem), keep the first (an export is usually append-ordered, so the later row
is more likely the correction), or keep the last. I keep the last and report it
as `conflicting_duplicate_in_file` with the line number. This one is reported
rather than silently resolved specifically because a flipped quality decides
whether the episode may be delivered to a client — it is the one duplicate
class with a commercial consequence.

**3. 404 instead of 403 when a client requests another client's resource.**
Returning 403 confirms that request #7 exists. Across the whole scoping layer
a client gets 404 for anything that is not theirs, because an id is a guessable
integer and "does this exist" is itself information. The cost is a slightly
confusing error for a legitimately confused user; the benefit is that the API
cannot be used to enumerate the request table. This is applied as a `WHERE`
clause in `_visible_requests()` rather than as a post-load check, so there is
no code path where a row is fetched and then found to belong to someone else.

### CSV cleaning decisions

Every defect in `seed/episodes.csv`, and what the importer does with it:

| Case | Example | Decision |
|---|---|---|
| Exact duplicate row | `EP-00074`, `EP-00030` | Keep once, count as `duplicate_in_file` |
| Contradicting duplicate | `EP-00011` (`bad` then `good`) | Later row wins, reported |
| Case-variant id | `ep-00003` vs `EP-00003` | Same episode; ids upper-cased |
| Blank `episode_id` | line 59 | **Reject** — no key to upsert on |
| Blank `robot_id` | `EP-00021` | **Reject** |
| Unknown robot | `EP-00024` → `arm-99` | **Reject** — typo or unregistered device; inventing it hides the problem |
| Leading space in field | `" arm-01"` | Trim |
| Task casing/spacing | `"  Pick Cup "`, `"PICK CUP"` | Trim, collapse spaces, lower-case |
| Quality casing | `Good`, `USABLE` | Lower-case |
| Invalid quality | `excellent` | **Reject** — guessing a bucket would corrupt the assignability rule |
| Blank quality | `EP-00019` | **Reject** — same reason |
| `dd/mm/yyyy` date | `14/08/2026 09:15` | Parse |
| Space instead of `T` | `2026-08-14 09:12:00` | Parse |
| Trailing `Z` | `...T09:20:00Z` | Parse as UTC |
| Unparseable date | `not a date` | **Reject** |
| Future date | `2031-01-01` | **Reject** (24h skew allowance) |
| Fractional duration | `45.5` | Round to 46 |
| `N/A` duration | `EP-90003` | **Reject** |
| Blank duration | `EP-00016` | **Reject** |
| Negative duration | `-5` | **Reject** |
| Absurd duration | `999999` (others 8–120) | **Reject** as implausible, ceiling 86400 |
| Short row | `EP-90001`, 5 of 7 fields | **Reject** as malformed |
| Quoted comma | `"pick cup, then place"` | Kept — `csv.reader` handles it |
| Blank `operator_name` | `EP-90005` | **Keep**, store NULL |
| Trailing blank lines | lines 191–193 | Skipped silently, counted |

The pattern: reject when the missing value would make the record *wrong*
(no key, unknown robot, unknown quality), keep when it is merely *incomplete*
(no operator name). Nothing is dropped without being counted and explained.

Against the supplied file this gives 173 imported, 16 skipped, 1 blank line,
190 read — and a second run imports nothing.

**Timezone assumption.** Dates without an offset are assumed UTC. The export
does not say what zone it wrote, and this is a guess — if the recording system
writes local Kigali time, every "episodes per day" bucket is off by two hours.
This is the first thing I would confirm with whoever owns that export.

---

## 2. What I left out, and what I would do next

Deliberately not built:

- **No stretch item.** I chose to spend the remaining time on the required
  work and on CI instead. Of the three, background jobs is the one I would
  pick; it is item 3 below.
- **No password reset or self-service account changes.** An admin can create
  an account and set its initial password, but nobody can change their own
  password and an admin cannot reset someone else's. Email is immutable too,
  because changing it silently changes who can log into an account that
  already owns requests. A real deployment needs a reset flow with an
  expiring token; this one does not have one.
- **No assignment history.** `assignments` models the *current* attachment
  only; unassigning deletes the row. Who removed an episode and when is not
  recoverable. For an internal tool at this stage that is acceptable; it would
  become a soft delete the first time someone asked "why did this disappear".
- **No pagination in the UI.** The API paginates; the frontend requests a
  large page and renders it. That breaks at a few thousand requests.
- **No rate limiting on login.** Noted as a vulnerability in section 4.
- **Stateless JWTs, so logout is client-side only.** Clearing the cookie
  stops the browser sending it, but a copied token stays valid until it
  expires (8 hours). Real revocation needs either a server-side session table
  or a short access token plus a refresh token.

With two more days, in order:

1. **A password reset flow**, which is the obvious hole left in user
   management.
2. **Replace stateless JWTs with server-side sessions.** Logout that does not
   actually log you out is the kind of thing that looks fine until it is in
   a security review.
3. **The background-jobs stretch item**, which is the one that would teach me
   most about this domain — a real export pipeline is where idempotency and
   retries actually bite.
4. **Soft-delete assignments** and show the full attachment history.
5. **Widen CI** to run the importer against a generated 200k-row file, so a
   performance regression shows up as a failed build rather than a surprise.

---

## 3. Something that went wrong

The API container refused to start with `exec ./entrypoint.sh: permission
denied`, even though the Dockerfile ran `chmod +x entrypoint.sh` and the build
succeeded.

What made it confusing is that the build was demonstrably fine — the layer
with the `chmod` ran without error. So the file was executable *in the image*
but not *in the running container*.

I worked backwards from that gap. The only thing that differs between the
image and the container is what compose mounts over it, and the compose file
bind-mounts `./api` onto `/app` for development reload. That mount replaces
the entire directory, including the file the Dockerfile had just chmod'ed,
with the host's copy — and the host copy had never had its executable bit set,
because I created it with a text editor.

Two fixes, and I applied both: set the bit on the host file so git records
mode `100755`, and change the command to `["bash", "entrypoint.sh"]` so it
does not depend on the bit at all. The second is the one that actually makes
it robust — the first would quietly regress on any filesystem that does not
carry permissions.

The general lesson I took: when a bind mount is in play, anything the
Dockerfile did to those paths is advisory. It is worth knowing which of your
image's properties survive the mount.

**A second one, caught on the first real run.** The sample import crashed at
the very last line of a successful import with:

```
KeyError: "Attempt to overwrite 'created' in LogRecord"
```

The import had done all its work; it died while *logging that it had
finished*. I was passing the report through as structured fields —
`extra={"created": 173, "updated": 0, ...}` — and `created` is a name the
logging module already puts on every record (it is the record's timestamp).
stdlib logging refuses to let an application field shadow one of its own and
raises rather than overwriting.

The narrow fix is to rename the field, which I did: the importer logs
`created_count`. But the same trap is waiting anywhere a dictionary of
application context is splatted into `extra=`, and the domain-error handler in
`main.py` does exactly that with whatever context an exception carries. So I
also added `safe_extra()`, which suffixes any key that would collide instead
of dropping it, and used it there.

What I take from it: structured logging makes the log a typed interface, and
`extra=` is a namespace shared with the logging library. Treating it as a
free-form dict is fine until a field name happens to collide — and then it
fails at the point of logging, which is the worst place to put a crash,
because it takes out the code path that was about to tell you what happened.

*(One non-defect worth noting: building the API image took around 40 minutes
because the container's connection to PyPI was running at roughly 12 kB/s.
That is environmental, but it is why the Dockerfile uses a BuildKit cache
mount for pip's download cache and raises pip's timeout and retry count — a
rebuild after a dependency change no longer re-fetches everything.)*

---

## 4. Security

**Passwords.** bcrypt with a per-password salt at the library default cost.
Plaintext never reaches the database and is never logged. bcrypt silently
truncates input at 72 bytes, which would make two different long passwords
interchangeable, so `hash_password()` rejects anything longer and the Pydantic
schema caps the field at the same length.

**Tokens.** HS256 JWTs carrying only the user id, role and expiry. Delivered
to the browser in an **HttpOnly, SameSite=Lax** cookie, so no token is
reachable from JavaScript — the standard failure mode of a React SPA is a
token in `localStorage` that any XSS can exfiltrate. The SPA and the API are
served from the same origin through nginx, which is what makes the cookie
workable and is also why there is no CORS configuration anywhere.
`Authorization: Bearer` is accepted too, for scripts and curl.

The role is in the token but **not trusted from it for authorization** — the
user is loaded from the database on every request and `is_active` re-checked,
so deactivating an account takes effect immediately.

**Input validation.** Pydantic validates every request body and query
parameter: types, string lengths, numeric ranges, enum membership. A malformed
body is a 422 before any handler runs. All database access goes through
SQLAlchemy with bound parameters; there is no string-built SQL in the project.
The CSV importer validates and normalises every field before it reaches an
insert, and the upload endpoint caps body size.

**The two vulnerabilities I would worry about most here:**

**1. Broken object-level authorization.** This is the characteristic flaw of
exactly this kind of system: a multi-tenant internal tool where every resource
has a sequential integer id and most endpoints are correctly locked down. It
only takes one endpoint that forgets the ownership filter for client A to read
client B's commercial requests. I mitigated it structurally — scoping is a
single `_visible_requests()` function applied as a `WHERE` clause, not a check
sprinkled through handlers — and `tests/test_authorization.py` asserts
cross-client isolation directly. It stays my top concern because the mitigation
is a convention, and conventions decay as endpoints are added.

**2. Credential stuffing against `/api/auth/login`.** There is no rate
limiting, no lockout and no MFA. The seed passwords are `admin123` and
`ops123`. An internal tool reachable from the internet with an unthrottled
login endpoint and weak passwords is a straightforward break, and an admin
account here can read every client's data. The login endpoint deliberately
returns an identical response for an unknown email and a wrong password, so it
cannot be used to enumerate accounts — but that only raises the cost, it does
not stop the attack. Rate limiting per IP and per account, plus a real password
policy, would be the first thing I added before this was exposed to anything.

**Also known, not fixed:** the JWT secret has a development default in
`config.py`; in production it must come from a secret store and the container
should refuse to start without it. Cookies are not `Secure` because the dev
stack is plain HTTP; behind TLS that flag must be on.

---

## 5. Scale

**What breaks first at 10× users.** Not the database — it is the bind-mounted
`--reload` uvicorn in `docker-compose.yml`, which is a development
convenience and single-process. The first change is a production compose file
with no mount, no reload, and uvicorn under multiple workers behind nginx.
After that the next limit is the connection pool: SQLAlchemy's default is
5 connections with 10 overflow per process, and workers multiply that, so
Postgres's `max_connections` becomes the ceiling. The answer is PgBouncer in
transaction mode, not a bigger pool.

The second thing to break is the frontend, which fetches up to 200 requests
and renders them all. That needs real pagination before it needs anything
server-side.

**What breaks first at 100× episodes (≈20M rows).** Not the analytics — the
indexes described in the README keep those bounded by the date window rather
than the table. Three other things break first:

1. **The operator episode browser.** `GET /api/episodes` filters with
   `task_name ILIKE '%…%'`, which cannot use a B-tree index and becomes a
   sequential scan. The fix is a trigram index (`pg_trgm`) or, better, exact
   matching on the normalised `task_name` with an autocomplete endpoint backed
   by a distinct-values query.
2. **`COUNT(*)` for pagination totals.** Counting matching rows costs as much
   as the query itself. At that size you either drop exact totals for
   keyset pagination, or accept an estimate from the planner.
3. **The importer's in-memory duplicate map.** It keeps one entry per
   `episode_id` seen, so a 20M-row file would need gigabytes. Rows are already
   streamed and upserted in batches of 1000, so only this map grows. The right
   answer for files that size is `COPY` into an unlogged staging table and
   resolve duplicates and validation in SQL — which also makes the import
   restartable, something it currently is not.

What would *not* need to change: the schema, the constraints, and the fact
that aggregation happens in Postgres.

---

## 6. AI tooling

I used **Claude Code** (Anthropic's CLI) throughout.

The decisions this document defends are mine: the stack, the schema shape,
treating episode ids case-insensitively, last-row-wins on a contradicting
duplicate, 404 rather than 403 for another client's resource, and storing the
current status alongside an append-only history. It wrote most of the
first-draft code from those decisions, and effectively all of the
boilerplate — Dockerfiles, nginx config, the React forms and tables.

Where it earned its place: enumerating every defect in the 190-row
`seed/episodes.csv`. Finding them by hand is exactly the work worth
delegating; deciding what each one should do is not, and the decision table
in section 1 is where I spent that time instead.

Where I overrode it: it first proposed a React SPA holding the token in
`localStorage` with CORS between two origins. I replaced that with the
same-origin nginx setup and an HttpOnly cookie, which is why there is no CORS
configuration in this project. It also suggested Postgres' `xmax = 0` trick
to distinguish inserts from updates in the importer's `RETURNING` clause; I
rejected it as clever rather than clear and used an explicit per-batch
`SELECT`, which also yields the `unchanged` count the report needs.

So this is AI-assisted code rather than code I typed line by line. I have
read all of it, and `services/importer.py` is the file I would most like to
be asked about.

