"""
End-to-end checks against a RUNNING app and a REAL MongoDB.

!! It wipes `employees` and `attendance_logs` in MONGO_DB. Use a throwaway database. !!

    export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_test"
    uvicorn app.main:app --port 8000          # terminal 1
    python tests/smoke_test.py                # terminal 2
    python tests/smoke_test.py --big          # also loads ~100k logs and checks explain plans

Covers what the sample data does not: ties, zero-log employees, mid-month joiners,
joiners after the month, weekend records, half days, open records, legacy records,
logs of unknown employees, gap filling in the trend, races, and IXSCAN at 100k.
Only uses the standard library + pymongo (already in requirements.txt).
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from pymongo import MongoClient

BASE = os.environ.get("API_URL", "http://127.0.0.1:8000")
db = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[
    os.environ.get("MONGO_DB", "attendance_test")]
IST = timezone(timedelta(hours=5, minutes=30))
results = {"pass": 0, "fail": 0}


def call(method, path, body=None, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(label, actual, expected):
    if actual == expected:
        results["pass"] += 1
    else:
        results["fail"] += 1
        print(f"FAIL {label}\n   expected: {expected}\n   actual:   {actual}")


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@x.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


def log(code, day, status, wh=None, late=0, ot=0, half=False, legacy=False):
    pin = datetime.fromisoformat(day + "T04:00:00+00:00") if status not in ("ABSENT", "LEAVE") else None
    pout = pin + timedelta(hours=wh) if (pin and wh is not None) else None
    doc = {"emp_code": code, "date": day, "status": status, "punch_in": pin, "punch_out": pout,
           "work_hours": wh, "late_minutes": late, "overtime_minutes": ot}
    if not legacy:
        doc.update(half_day=half, history=[])
    return doc


def seed():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([
        emp("EMP0101", "Eng", "2026-01-01"),
        emp("EMP0102", "Eng", "2026-07-15"),            # mid-month joiner
        emp("EMP0103", "Eng", "2026-08-10"),            # joins after July
        emp("EMP0104", "Ops", "2026-01-01"),            # zero logs
        emp("EMP0105", "Ops", "2026-01-01", "22:00", "06:00"),  # overnight
        emp("EMP0106", "Sales", "2026-01-01"),
        emp("EMP0107", "Sales", "2026-01-01"),
    ])
    db.attendance_logs.insert_many([
        log("EMP0101", "2026-07-01", "PRESENT", 9.0, late=35),
        log("EMP0101", "2026-07-02", "WFH", 3.5, half=True),
        log("EMP0101", "2026-07-03", "LEAVE"),
        log("EMP0101", "2026-07-04", "PRESENT", 5.0, late=20, ot=40),    # Saturday
        log("EMP0101", "2026-07-06", "ABSENT"),
        log("EMP0101", "2026-07-07", "ON_DUTY", None, late=15),          # open record
        log("EMP0101", "2026-07-08", "PRESENT", 8.0, legacy=True),       # no half_day / history
        log("EMP0102", "2026-07-15", "PRESENT", 8.5, late=50),
        log("EMP0102", "2026-07-16", "PRESENT", 8.25, ot=30),
        log("EMP0105", "2026-07-06", "PRESENT", 8.75, late=50, ot=40),
        log("EMP0106", "2026-07-01", "PRESENT", 9.0, late=50),
        log("EMP0107", "2026-07-02", "PRESENT", 9.01, late=12),          # Sales avg 9.005 -> 9.01
        log("EMP0999", "2026-07-02", "PRESENT", 9.0, late=500),          # unknown employee
    ])


def test_analytics():
    s, j = call("GET", "/analytics/employees/EMP0101/monthly", month="2026-07")
    check("monthly EMP0101", (s, j), (200, {
        "emp_code": "EMP0101", "month": "2026-07", "working_days": 23, "present_days": 3.5, "leave_days": 1,
        "late_count": 3, "total_late_minutes": 70, "total_overtime_minutes": 40, "attendance_pct": 15.22}))
    s, j = call("GET", "/analytics/employees/EMP0102/monthly", month="2026-07")
    check("monthly mid-month joiner", (j["working_days"], j["present_days"], j["attendance_pct"]), (13, 2.0, 15.38))
    s, j = call("GET", "/analytics/employees/EMP0103/monthly", month="2026-07")
    check("monthly not joined yet", (j["working_days"], j["attendance_pct"]), (0, None))

    s, j = call("GET", "/analytics/departments/summary", month="2026-07")
    check("dept summary", (s, j), (200, {"month": "2026-07", "items": [
        {"department": "Eng", "headcount": 2, "present_days": 5.5, "avg_work_hours": 7.04, "late_count": 4,
         "total_late_minutes": 120, "leave_count": 1, "on_duty_count": 1},
        {"department": "Ops", "headcount": 2, "present_days": 1.0, "avg_work_hours": 8.75, "late_count": 1,
         "total_late_minutes": 50, "leave_count": 0, "on_duty_count": 0},
        {"department": "Sales", "headcount": 2, "present_days": 2.0, "avg_work_hours": 9.01, "late_count": 2,
         "total_late_minutes": 62, "leave_count": 0, "on_duty_count": 0},
    ]}))
    s, j = call("GET", "/analytics/departments/summary", month="2026-06", department="Eng")
    check("dept summary no logs month", j["items"], [{"department": "Eng", "headcount": 1, "present_days": 0.0,
          "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}])

    s, j = call("GET", "/analytics/leaderboard/late", month="2026-07", limit=2)
    check("leaderboard ties", [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in j["items"]],
          [(1, "EMP0101", 70), (2, "EMP0102", 50), (2, "EMP0105", 50), (2, "EMP0106", 50)])
    s, j = call("GET", "/analytics/leaderboard/late", month="2026-07", limit=10)
    check("leaderboard skip rank", [i["rank"] for i in j["items"]], [1, 2, 2, 2, 5])
    check("leaderboard fields", j["items"][0], {"rank": 1, "emp_code": "EMP0101", "name": "Name EMP0101",
          "department": "Eng", "total_late_minutes": 70, "late_count": 3})
    s, j = call("GET", "/analytics/leaderboard/late", month="2026-07", limit=1, department="Ops")
    check("leaderboard department", [(i["rank"], i["emp_code"]) for i in j["items"]], [(1, "EMP0105")])

    s, j = call("GET", "/analytics/departments/Eng/trend", **{"from": "2026-07-01", "to": "2026-07-16"})
    rows = [(r["date"][-2:], r["is_working_day"], r["headcount"], r["present_count"], r["late_count"],
             r["attendance_rate"], r["moving_avg_7d"]) for r in j["items"]]
    check("trend Eng", rows, [
        ("01", True, 1, 1.0, 1, 1.0, 1.0), ("02", True, 1, 0.5, 0, 0.5, 0.75),
        ("03", True, 1, 0.0, 0, 0.0, 0.5), ("04", False, 1, 1.0, 1, None, 0.5),
        ("05", False, 1, 0.0, 0, None, 0.5), ("06", True, 1, 0.0, 0, 0.0, 0.375),
        ("07", True, 1, 1.0, 1, 1.0, 0.5), ("08", True, 1, 1.0, 0, 1.0, 0.5),
        ("09", True, 1, 0.0, 0, 0.0, 0.4), ("10", True, 1, 0.0, 0, 0.0, 0.4),
        ("11", False, 1, 0.0, 0, None, 0.4), ("12", False, 1, 0.0, 0, None, 0.4),
        ("13", True, 1, 0.0, 0, 0.0, 0.4), ("14", True, 1, 0.0, 0, 0.0, 0.2),
        ("15", True, 2, 1.0, 1, 0.5, 0.1), ("16", True, 2, 1.0, 0, 0.5, 0.2)])
    s, j = call("GET", "/analytics/departments/Ops/trend", **{"from": "2026-07-25", "to": "2026-07-27"})
    check("trend with no records at all", [(r["date"], r["attendance_rate"], r["moving_avg_7d"]) for r in j["items"]],
          [("2026-07-25", None, None), ("2026-07-26", None, None), ("2026-07-27", 0.0, 0.0)])
    check("trend unknown dept", call("GET", "/analytics/departments/Nope/trend",
                                     **{"from": "2026-07-01", "to": "2026-07-02"})[0], 404)


def test_races():
    db.employees.insert_one(emp("EMP0200", "Race", "2026-01-01"))
    with ThreadPoolExecutor(20) as pool:
        codes = sorted(pool.map(lambda _: call("POST", "/attendance/punch-in",
                                               {"emp_code": "EMP0200", "punched_at": ms(2026, 7, 20, 9, 30)})[0],
                                range(20)))
    check("parallel punch-in", (codes.count(201), codes.count(409)), (1, 19))
    with ThreadPoolExecutor(20) as pool:
        codes = sorted(pool.map(lambda _: call("POST", "/attendance/punch-out",
                                               {"emp_code": "EMP0200", "punched_at": ms(2026, 7, 20, 18, 30)})[0],
                                range(20)))
    check("parallel punch-out", (codes.count(200), codes.count(409)), (1, 19))
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda i: call("PATCH", "/attendance/EMP0200/2026-07-20",
                                             {"reason": f"fix number {i}", "regularized_by": "hr",
                                              "punch_out": ms(2026, 7, 20, 18, 31 + i)})[0], range(10)))
    stored = db.attendance_logs.find_one({"emp_code": "EMP0200"})
    check("parallel PATCH keeps every history entry", len(stored["history"]), codes.count(200))
    check("parallel PATCH only 200/409", set(codes) <= {200, 409}, True)
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: call("POST", "/employees", {"emp_code": "EMP0201", "name": "x",
                     "email": "x@x.com", "department": "Race", "joined_on": "2026-01-01"})[0], range(10)))
    check("parallel create employee", (codes.count(201), codes.count(409)), (1, 9))


def test_big():
    print("loading ~100k attendance records ...")
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    depts = ["Eng", "Ops", "Sales", "HR", "Finance"]
    db.employees.insert_many([emp(f"EMP{i:04d}", depts[i % 5], "2025-01-01") for i in range(1, 501)])
    docs, start = [], date(2025, 9, 1)
    for i in range(1, 501):
        for d in range(200):
            day = (start + timedelta(days=d)).isoformat()
            docs.append(log(f"EMP{i:04d}", day, random.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]),
                            8.0, late=random.choice([0, 0, 0, 15, 30])))
    db.attendance_logs.insert_many(docs)
    call("GET", "/health")  # makes sure indexes exist
    cases = {
        "attendance_list": {}, "employee_monthly": {"emp_code": "EMP0001", "month": "2026-01"},
        "department_summary": {"month": "2026-01"}, "late_leaderboard": {"month": "2026-01"},
        "department_trend": {"department": "Eng", "from": "2026-01-01", "to": "2026-03-01"},
    }
    for name, params in cases.items():
        s, j = call("GET", f"/admin/explain/{name}", **params)
        text = json.dumps(j)
        check(f"explain {name}", (s, "IXSCAN" in text, "COLLSCAN" in text), (200, True, False))
    for path, params in [("/attendance", {"status": "LEAVE"}),
                         ("/analytics/departments/summary", {"month": "2026-01"}),
                         ("/analytics/leaderboard/late", {"month": "2026-01"}),
                         ("/analytics/departments/Eng/trend", {"from": "2026-01-01", "to": "2026-03-01"})]:
        t = time.perf_counter()
        call("GET", path, **params)
        print(f"   {path}: {(time.perf_counter() - t) * 1000:.0f} ms")


if __name__ == "__main__":
    if call("GET", "/health")[0] != 200:
        sys.exit("app is not healthy - start it first")
    seed()
    test_analytics()
    test_races()
    if "--big" in sys.argv:
        test_big()
    print(f"\n{results['pass']} passed, {results['fail']} failed")
    sys.exit(1 if results["fail"] else 0)
