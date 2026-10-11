# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate-create race), `{department, emp_code}` (filtered list, trend), `{department, joined_on}` and `{joined_on}` (headcount). `attendance_logs`: unique `{emp_code, date}` (one record per day, punch-in race, punch-out, PATCH, monthly, `$lookup`), `{date: -1, emp_code: 1}` (list sort, month scans), `{status, date: -1, emp_code}` (status filter). I rejected an index on `late_minutes`: the leaderboard already narrows by month, so it would add write cost for little gain.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no read-then-write gap.

3. **Ties.** Ranks come from `$setWindowFields` with `$rank`, so ties share a rank and the next one is skipped. I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that is more than `limit` rows, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (filtered by `joined_on`), `$lookup`s the month's logs and `$unwind`s with `preserveNullAndEmptyArrays`, so people with no logs still form a row.

5. **At 100x.** Pre-aggregate daily per-department counts when records change, instead of scanning raw logs for every analytics request.
