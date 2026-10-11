# REVIEW.md

Line numbers refer to the starter `app/main.py` from the kit.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, L36-37 | Minutes are truncated *before* the grace check (`minutes > 10`), so anything up to 10:59 after shift start is treated as on time. R2 says late means strictly more than 10:00. | Shift 09:30, punch 09:40:01 → returns 0, expected 10. Punch 09:40:59 → 0, expected 10. | Compare elapsed **seconds** with 600, then floor to minutes. |
| 2 | `compute_late_minutes`, L35 | `punch_in.replace(hour=..)` builds the shift start on the punch-in's own clock (naive server time / UTC), not in IST. | On a UTC server a 09:45 IST punch (04:15 UTC) is compared with 09:30 *UTC* → 0 late. | Convert to IST and build the shift start from the attendance date in IST. |
| 3 | `compute_late_minutes`, L35 | Overnight shifts: a punch-in after midnight is compared with *that* day's shift start, giving a negative value → 0. | Shift 22:00-06:00, punch 00:30 → 0, expected 150. | Shift start is taken from the attendance date given by R1 (the previous day). |
| 4 | `compute_work_hours`, L41 | Python `round()` on a float is banker's rounding on an inexact binary value, not half-up. | 4h29m42s = 4.495 h → `round` gives 4.49 (and `half_day` becomes true). Expected 4.50. | `Decimal` + `ROUND_HALF_UP`; the Decimal is also used for the R5 comparison. |
| 5 | `compute_overtime`, L47 | No 30-minute minimum (R3). | Punch-out 18:35 with shift end 18:30 → 5, expected 0. | Return the minutes only when they are >= 30. |
| 6 | `compute_overtime`, L46 | For an overnight shift the shift end is on the **next** day, but it is placed on the attendance date. | Shift 22:00-06:00, out 06:40 next day → ~24h40m of overtime, expected 40. | Add one day to the shift end when `shift_end <= shift_start`. |
| 7 | module, L22 | `MongoClient` without `tz_aware=True` returns naive datetimes; mixing them with aware ones raises `TypeError` (500) or silently shifts times. | Read a seeded record and subtract an aware datetime → `TypeError: can't subtract offset-naive and offset-aware`. | `MongoClient(..., tz_aware=True)`; all instants are aware UTC. |
| 8 | module / startup | No indexes are created, so the unique rules can't be enforced and every query is a COLLSCAN. | `/admin/explain` shows COLLSCAN; GET /attendance is slow at 100k docs. | `ensure_indexes()` at startup (idempotent), retried from `/health`. |
| 9 | `health`, L72-74 | Always 200, even when MongoDB is down. | Stop MongoDB → `/health` still returns 200. | Ping MongoDB; 503 on failure. |
| 10 | `create_employee`, L79-83 | Check-then-insert: two parallel creates both pass the check, both insert → duplicate `emp_code`. | Fire 10 parallel POSTs with the same code → several 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` → 409. |
| 11 | `create_employee`, L82 / L85 | `created_at` is `datetime.now()` (naive server-local time) and is returned as an ISO string instead of epoch ms. | Response has `"created_at": "2026-10-11T11:02:03.123"`. | `datetime.now(UTC)`; serialized as epoch milliseconds. |
| 12 | `EmployeeIn`, L53-60 | No validation: any `emp_code`, email, `HH:MM`, `joined_on`, empty names, and `shift_start == shift_end` are accepted. | POST with `shift_start "25:99"` or `joined_on "2026-02-30"` → 201. | Pydantic constraints from the contract + model validator (real date, start != end) → 422. |
| 13 | `list_employees`, L93 | `skip = page * page_size` skips the first page; `page=1` returns rows 21-40. | `GET /employees` with 5 employees → empty `items`. | `skip = (page - 1) * page_size`. |
| 14 | `list_employees`, L94-95 | `total` counts the whole collection, ignoring the department filter; no sort; `page`/`page_size` unbounded (`page=0` gives a negative skip → 500). | `?department=Ops` → `total` = all employees. `?page=0` → 500. | Count with the same filter; sort by `emp_code`; `page >= 1`, `1 <= page_size <= 100`. |
| 15 | `punch_in`, L101 / L113 | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises → 500. | Punch in `EMP9999` → 500. | 404 when the employee does not exist. |
| 16 | `punch_in`, L102-103 | `fromtimestamp()` uses the server's local zone, so `date` is the server's date, not the IST date (R1); overnight rule missing; milliseconds not truncated. | On a UTC server a punch at 01:00 IST gets the previous date; 09:40:00.900 is stored with ms. | Truncate to seconds, convert to IST, apply the overnight rule. |
| 17 | `punch_in`, L104-118 | Check-then-insert race: simultaneous punch-ins both see "no record" and both insert. | 20 parallel punch-ins → several 201s and duplicate records. | Unique index on `(emp_code, date)`; `DuplicateKeyError` → 409. |
| 18 | `PunchInIn`, L64-66 | `status` accepts anything (ABSENT, LEAVE, "foo"); `punched_at` accepts seconds, floats (`1.78e12`) and bools. | `{"status": "ABSENT"}` → 201; `punched_at: 1783312500` (seconds) → a 1970 record. | `PresenceStatus` literal; strict int in `[1e11, 4102444800000]` → 422. |
| 19 | `punch_in`, L119-121 | Response leaks an `id` (the ObjectId) and returns datetimes instead of epoch ms. | Response contains `"id": "66..."` and `"punch_in": "2026-..."`. | One serializer for all records: no `_id`/`id`, instants as epoch ms. |
| 20 | `list_attendance`, L146-150 | Loads the whole collection into memory and sorts/pages in Python. | Slow and memory heavy at 100k records; explain shows COLLSCAN. | MongoDB `sort/skip/limit` + `count_documents` on indexed fields. |
| 21 | `list_attendance`, L147 | Sort is only by `date`; the contract requires `date` desc then `emp_code` asc, so page order is unstable. | Several employees on the same date come back in random order across pages. | `sort([("date", -1), ("emp_code", 1)])`, backed by index `{date: -1, emp_code: 1}`. |
| 22 | `list_attendance`, L150 | Returns `id` (ObjectId) and raw datetimes; legacy records without `history`/`half_day` are returned without those fields. | Response contains `id`; legacy record lacks `half_day`. | Serializer fills `history: []`, `half_day: false`, `late/overtime: 0`. |
| 23 | `list_attendance`, L125-131 | No validation: `date_from > date_to`, a bad date, an unknown `status`, and `page`/`page_size` bounds are not checked. | `?date_from=2026-07-10&date_to=2026-07-01` → 200 with nothing; `?page=0` → wrong slice. | 422 for invalid dates/ranges/status; bounded paging. |

## Looked at and decided were not defects

- `load_dotenv()` (L20): by default it does **not** override variables that already exist, so real environment variables already win over `.env`. Kept, made explicit with `override=False`.
- `IST = timezone(timedelta(hours=5, minutes=30))` (L25): correct; India has no daylight saving time, so a fixed offset is right.
- The `if body.punched_at` truthiness check (L102) would treat `0` as missing, but `0` is now rejected by validation anyway, so it can't happen. I still use `is not None`.
- `list_attendance` building the `date` filter with string `$gte/$lte` (L136-141) is correct: `YYYY-MM-DD` strings sort the same way as the dates they represent.
