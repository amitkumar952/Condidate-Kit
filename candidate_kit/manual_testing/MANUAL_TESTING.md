# HROne Attendance API: plain-English guide and manual testing

This folder is **not** part of the GitHub repo. It is for you: to understand the project and to test it by hand.

---

## Part 1: What the project is about

Think of a company attendance system, like a biometric machine plus an HR dashboard.

| Real-world thing | In this API |
|---|---|
| An employee joins | `POST /employees` |
| Employee arrives at office | `POST /attendance/punch-in` |
| Employee leaves | `POST /attendance/punch-out` |
| HR fixes a wrong entry ("machine glitch") | `PATCH /attendance/{emp_code}/{date}`, and the change is recorded in `history` (audit trail) |
| Manager asks "how did Asha do this month?" | `GET /analytics/employees/{emp_code}/monthly` |
| "How is each department doing?" | `GET /analytics/departments/summary` |
| "Who comes late the most?" | `GET /analytics/leaderboard/late` |
| "Daily attendance trend of Engineering" | `GET /analytics/departments/{department}/trend` |
| "Prove your queries are fast (use indexes)" | `GET /admin/explain/{endpoint}` |

### The rules the API calculates automatically (R1-R10)

| Rule | Plain meaning | Example |
|---|---|---|
| R1 | Times are Indian time (IST). The record's date = the day you punched in. Night shift (22:00-06:00): punching in at 01:00 belongs to *yesterday's* shift. | Punch-in 01:00 on 7 Jul → date 6 Jul |
| R2 | Late only if you arrive **more than 10 minutes** after shift start; then late = minutes since shift start. | Shift 09:30: 09:40:00 → 0, 09:40:01 → 10, 10:05 → 35 |
| R3 | Overtime counts only if you stay **30+ minutes** after shift end. | Out 18:59 → 0, out 19:10 → 40 |
| R4 | Work hours = out − in, 2 decimals, normal school rounding (4.495 → 4.50). | 10:05 → 19:10 = 9.08 |
| R5 | Less than 4.50 hours = half day = counts as 0.5 day. | 10:00 → 13:30 = 3.5 h → half day |
| R6 | PRESENT, WFH, ON_DUTY = present. ABSENT, LEAVE = not present. | |
| R7 | Working days = Mon-Fri, counted from joining date. Weekend work doesn't add present days but still adds late/overtime. | Joined 15 Jul → 13 working days in July |
| R8 | Reported numbers 2 decimals; rates 4 decimals. | |
| R9 | Headcount = everyone who has joined by then, **even with zero attendance records**. | |
| R10 | Paging: page starts at 1, max 100 per page. | |

### The assignment had 3 parts

| Part | What they asked | Where it is |
|---|---|---|
| A. Review | A colleague's half-done `app/main.py` has 10+ hidden bugs. Find and fix them, list them. | Fixed in `app/main.py`, listed in `REVIEW.md` (23 found) |
| B. Build | Write the missing 7 endpoints, analytics using MongoDB aggregation pipelines. | `app/main.py` |
| C. Prove | Explain endpoint showing indexes are used; answer 5 design questions. | `/admin/explain/...`, `DECISIONS.md` |

---

## Part 2: What has been done

| Step | Status |
|---|---|
| Read the whole kit (problem statement, OpenAPI contract, data model, starter code) | Done |
| Fixed the starter's bugs (wrong late calculation, timezone bugs, race conditions, page-1 bug, 500 on unknown employee, …) | Done, 23 listed in `REVIEW.md` |
| Built punch-out, PATCH correction with audit history, 4 analytics pipelines, explain endpoint | Done |
| Indexes created automatically at startup | Done |
| Safe under parallel requests (unique index for punch-in, conditional update for punch-out and PATCH) | Done, tested |
| Automated tests: 81 rule/validation checks + `tests/smoke_test.py` (22 end-to-end checks, 100k records) | **All passed on your Mac** |
| `README.md`, `REVIEW.md`, `DECISIONS.md`, `.gitignore`, `.env.example` | Done |
| Rewrite `DECISIONS.md` in your own words | **Your step** |
| Push to public GitHub repo + submit the form before 15 Oct, 11:59 PM IST | **Your step** |

### How a request flows through `app/main.py`

```
Request → Pydantic validation (bad input → 422)
        → endpoint function
        → business rules R1-R5 (section 4 of main.py)
        → MongoDB (unique index / conditional update → 409 on conflict)
        → serializer: dates → epoch ms, hide _id, fill missing legacy fields
        → JSON response
```

### Why times look like `1783312500000`

The contract says every instant is **epoch milliseconds**. `1783312500000` = 6 Jul 2026, 10:05 IST.
Convert any IST time on your Mac:

```bash
python3 -c "from datetime import datetime,timezone,timedelta; print(int(datetime(2026,7,6,10,5,tzinfo=timezone(timedelta(hours=5,minutes=30))).timestamp()*1000))"
```

---

## Part 3: Manual testing

### Step 0: start with an empty database (important)

The expected results below assume an **empty** database, run **in order**.

```bash
# Terminal 1
cd ~/Downloads/candidate_kit/hrone-attendance-api
source ../.venv/bin/activate
docker start mongo
docker exec mongo mongosh attendance_manual --quiet --eval "db.dropDatabase()"
MONGO_DB=attendance_manual uvicorn app.main:app --port 8000
```

To repeat the tests from scratch: stop the server (Ctrl+C), run the `dropDatabase` line again, start the server again
(restarting re-creates the indexes).

### Option A: Postman

1. Postman → **Import** → choose `HROne_Attendance_API.postman_collection.json` (in this folder).
2. The collection has a variable `base_url` = `http://127.0.0.1:8000`.
3. Run requests top to bottom, or use **Run collection**. Each request has a test that checks the expected status code;
   the request's description says what to look for in the response.

### Option B: curl, one by one

```bash
# Terminal 2
BASE=http://127.0.0.1:8000
```
Tip: add ` | python3 -m json.tool` at the end of a command for pretty output.

### 1. System

**1. Health check** — expect **200**. Returns {"status":"ok"} only when MongoDB answers. Stop Mongo and it returns 503.
```bash
curl -s -X GET "$BASE/health" ; echo
```


### 2. Employees

**2. Create EMP0001 (day shift)** — expect **201**. created_at comes back as epoch milliseconds; no _id in the response.
```bash
curl -s -X POST "$BASE/employees" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"}' ; echo
```

**3. Create EMP0002 (joins mid-month)** — expect **201**. Joined 15 July: July working days are counted from the 15th.
```bash
curl -s -X POST "$BASE/employees" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0002", "name": "Ravi", "email": "ravi@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-15"}' ; echo
```

**4. Create EMP0003 (overnight shift)** — expect **201**. Shift 22:00-06:00: shift_end <= shift_start means overnight.
```bash
curl -s -X POST "$BASE/employees" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0003", "name": "Meera", "email": "meera@example.com", "department": "Operations", "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-01"}' ; echo
```

**5. Create EMP0004 (will never have logs)** — expect **201**. Still counts in Operations headcount.
```bash
curl -s -X POST "$BASE/employees" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0004", "name": "Karan", "email": "karan@example.com", "department": "Operations", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"}' ; echo
```

**6. Duplicate emp_code -> 409** — expect **409**. Unique index on emp_code.
```bash
curl -s -X POST "$BASE/employees" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"}' ; echo
```

**7. Invalid employee -> 422** — expect **422**. Bad emp_code format and shift_start == shift_end.
```bash
curl -s -X POST "$BASE/employees" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "E1", "name": "Bad", "email": "bad@example.com", "department": "Engineering", "shift_start": "10:00", "shift_end": "10:00", "joined_on": "2026-02-30"}' ; echo
```

**8. List employees (sorted, paged)** — expect **200**. 4 employees sorted by emp_code; total = 4.
```bash
curl -s -X GET "$BASE/employees?page=1&page_size=10" ; echo
```

**9. List employees by department** — expect **200**. total = 2 (filtered count, not whole collection).
```bash
curl -s -X GET "$BASE/employees?department=Operations" ; echo
```


### 3. Punch in / out

**10. Punch-in late (10:05 IST, 6 Jul)** — expect **201**. date 2026-07-06, late_minutes 35 (counted from 09:30), work_hours null.
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783312500000}' ; echo
```

**11. Same day again -> 409** — expect **409**. Unique (emp_code, date) index.
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783314000000}' ; echo
```

**12. Unknown employee -> 404** — expect **404**
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP9999"}' ; echo
```

**13. Seconds instead of ms -> 422** — expect **422**. 1783312500 looks like seconds; must be milliseconds.
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783312500}' ; echo
```

**14. Status ABSENT at punch-in -> 422** — expect **422**. Only PRESENT, WFH, ON_DUTY are allowed at punch-in.
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "status": "ABSENT"}' ; echo
```

**15. Punch-out 19:10 IST** — expect **200**. work_hours 9.08, overtime_minutes 40 (>= 30), half_day false.
```bash
curl -s -X POST "$BASE/attendance/punch-out" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783345200000}' ; echo
```

**16. Punch-out again -> 409** — expect **409**. Already punched out.
```bash
curl -s -X POST "$BASE/attendance/punch-out" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783346000000}' ; echo
```

**17. Overnight punch-in 21:55 IST** — expect **201**. date 2026-07-06, late 0 (before 22:00 start).
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0003", "punched_at": 1783355100000}' ; echo
```

**18. Overnight punch-out 06:40 next day** — expect **200**. Same record (date 2026-07-06); work_hours 8.75; overtime 40 (shift ended 06:00 on the 7th).
```bash
curl -s -X POST "$BASE/attendance/punch-out" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0003", "punched_at": 1783386600000}' ; echo
```

**19. WFH punch-in 10:00 IST, 7 Jul** — expect **201**. status WFH, late_minutes 30.
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783398600000, "status": "WFH"}' ; echo
```

**20. WFH punch-out 13:30 -> half day** — expect **200**. work_hours 3.5 < 4.50 so half_day true.
```bash
curl -s -X POST "$BASE/attendance/punch-out" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783411200000}' ; echo
```

**21. Grace boundary 09:40:01, 8 Jul** — expect **201**. 10:01 after shift start: strictly more than 10:00, so late_minutes = 10.
```bash
curl -s -X POST "$BASE/attendance/punch-in" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783483801000}' ; echo
```

**22. Punch-out 18:30, 8 Jul** — expect **200**. overtime 0 (no minutes after shift end).
```bash
curl -s -X POST "$BASE/attendance/punch-out" \
  -H 'Content-Type: application/json' \
  -d '{"emp_code": "EMP0001", "punched_at": 1783515600000}' ; echo
```


### 4. List attendance

**23. All records of EMP0001** — expect **200**. 3 records, newest date first; instants are epoch ms; no id.
```bash
curl -s -X GET "$BASE/attendance?emp_code=EMP0001" ; echo
```

**24. Date range + status filter** — expect **200**. Only the WFH record of 7 Jul.
```bash
curl -s -X GET "$BASE/attendance?date_from=2026-07-06&date_to=2026-07-08&status=WFH" ; echo
```

**25. date_from after date_to -> 422** — expect **422**
```bash
curl -s -X GET "$BASE/attendance?date_from=2026-07-08&date_to=2026-07-06" ; echo
```

**26. page_size over 100 -> 422** — expect **422**
```bash
curl -s -X GET "$BASE/attendance?page_size=101" ; echo
```


### 5. Corrections (PATCH)

**27. Fix punch-in to 09:28 (biometric glitch)** — expect **200**. late_minutes 35 -> 0, work_hours 9.08 -> 9.7; history gets 1 entry listing only changed fields.
```bash
curl -s -X PATCH "$BASE/attendance/EMP0001/2026-07-06" \
  -H 'Content-Type: application/json' \
  -d '{"punch_in": 1783310280000, "reason": "biometric glitch", "regularized_by": "hr.admin"}' ; echo
```

**28. Same change again -> 422** — expect **422**. Nothing would change.
```bash
curl -s -X PATCH "$BASE/attendance/EMP0001/2026-07-06" \
  -H 'Content-Type: application/json' \
  -d '{"punch_in": 1783310280000, "reason": "biometric glitch", "regularized_by": "hr.admin"}' ; echo
```

**29. Punch-in on another day -> 422** — expect **422**. punch_in must stay on the record's date (R1).
```bash
curl -s -X PATCH "$BASE/attendance/EMP0001/2026-07-06" \
  -H 'Content-Type: application/json' \
  -d '{"punch_in": 1783395000000, "reason": "wrong day test", "regularized_by": "hr.admin"}' ; echo
```

**30. Mark 8 Jul as LEAVE** — expect **200**. punch_in/punch_out cleared, work_hours null, late 0; history entry added.
```bash
curl -s -X PATCH "$BASE/attendance/EMP0001/2026-07-08" \
  -H 'Content-Type: application/json' \
  -d '{"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr.admin"}' ; echo
```

**31. ABSENT with a punch time -> 422** — expect **422**. ABSENT/LEAVE can't have punch times.
```bash
curl -s -X PATCH "$BASE/attendance/EMP0001/2026-07-07" \
  -H 'Content-Type: application/json' \
  -d '{"status": "ABSENT", "punch_in": 1783398600000, "reason": "invalid combo", "regularized_by": "hr.admin"}' ; echo
```

**32. Reason too short -> 422** — expect **422**. reason needs 5-200 characters.
```bash
curl -s -X PATCH "$BASE/attendance/EMP0001/2026-07-07" \
  -H 'Content-Type: application/json' \
  -d '{"status": "PRESENT", "reason": "x", "regularized_by": "hr.admin"}' ; echo
```

**33. No record for that date -> 404** — expect **404**
```bash
curl -s -X PATCH "$BASE/attendance/EMP0001/2026-07-20" \
  -H 'Content-Type: application/json' \
  -d '{"status": "WFH", "reason": "no such record", "regularized_by": "hr.admin"}' ; echo
```


### 6. Analytics

**34. Monthly summary EMP0001** — expect **200**. working_days 23, present_days 1.5 (6 Jul = 1, 7 Jul half day = 0.5, 8 Jul LEAVE = 0), leave_days 1, late_count 1, total_late_minutes 30, total_overtime_minutes 40, attendance_pct 6.52.
```bash
curl -s -X GET "$BASE/analytics/employees/EMP0001/monthly?month=2026-07" ; echo
```

**35. Monthly summary mid-month joiner** — expect **200**. working_days 13 (from 15 Jul), present_days 0, attendance_pct 0.0.
```bash
curl -s -X GET "$BASE/analytics/employees/EMP0002/monthly?month=2026-07" ; echo
```

**36. Monthly bad month -> 422** — expect **422**
```bash
curl -s -X GET "$BASE/analytics/employees/EMP0001/monthly?month=2026-13" ; echo
```

**37. Department summary** — expect **200**. Engineering: headcount 2, present_days 1.5, avg_work_hours 6.6. Operations: headcount 2 (EMP0004 has no logs but counts), present_days 1, avg 8.75.
```bash
curl -s -X GET "$BASE/analytics/departments/summary?month=2026-07" ; echo
```

**38. Late leaderboard** — expect **200**. EMP0001 rank 1 with 30 late minutes (ties are covered by tests/smoke_test.py).
```bash
curl -s -X GET "$BASE/analytics/leaderboard/late?month=2026-07&limit=10" ; echo
```

**39. Department trend (7 days)** — expect **200**. 7 rows incl. weekend. attendance_rate 1.0, 0.5, 0.0, 0.0, 0.0, null, null; moving_avg_7d 1.0, 0.75, 0.5, 0.375, 0.3, 0.3, 0.3.
```bash
curl -s -X GET "$BASE/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12" ; echo
```

**40. Trend to < from -> 422** — expect **422**
```bash
curl -s -X GET "$BASE/analytics/departments/Engineering/trend?from=2026-07-12&to=2026-07-06" ; echo
```

**41. Trend unknown department -> 404** — expect **404**
```bash
curl -s -X GET "$BASE/analytics/departments/Nope/trend?from=2026-07-06&to=2026-07-12" ; echo
```


### 7. Admin explain

**42. Explain attendance_list** — expect **200**. Search the output for "IXSCAN" (index used). There must be no "COLLSCAN".
```bash
curl -s -X GET "$BASE/admin/explain/attendance_list?emp_code=EMP0001" ; echo
```

**43. Explain employee_monthly** — expect **200**
```bash
curl -s -X GET "$BASE/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07" ; echo
```

**44. Explain department_summary** — expect **200**. Runs on the employees collection.
```bash
curl -s -X GET "$BASE/admin/explain/department_summary?month=2026-07" ; echo
```

**45. Explain late_leaderboard** — expect **200**
```bash
curl -s -X GET "$BASE/admin/explain/late_leaderboard?month=2026-07" ; echo
```

**46. Explain department_trend** — expect **200**
```bash
curl -s -X GET "$BASE/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-12" ; echo
```

**47. Explain missing params -> 422** — expect **422**
```bash
curl -s -X GET "$BASE/admin/explain/employee_monthly" ; echo
```

---

### Bonus: see the race-condition protection yourself

```bash
curl -s -X POST "$BASE/employees" -H 'Content-Type: application/json' \
  -d '{"emp_code":"EMP0050","name":"Race","email":"race@example.com","department":"QA","joined_on":"2026-01-01"}'; echo
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/attendance/punch-in" \
    -H 'Content-Type: application/json' -d '{"emp_code":"EMP0050","punched_at":1783312500000}' &
done; wait
```
Expect exactly one `201` and nine `409`.

### Interactive docs

With the server running, open http://127.0.0.1:8000/docs. FastAPI shows every endpoint with a "Try it out" button.
