"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Contract: openapi.yaml (paths, status codes, business rules R1-R10).
Storage:  DATA_MODEL.md (collections `employees` and `attendance_logs`).

Layout of this file (all code lives here, as the assignment requires):
    1. Configuration and database connection
    2. Constants
    3. Time, conversion and rounding helpers
    4. Business rules R1-R5
    5. Request validation (Pydantic models and query-parameter checks)
    6. Serializers (MongoDB document -> API response)
    7. Indexes and application startup
    8. Query / pipeline builders (shared by the endpoints and /admin/explain)
    9. Endpoints: system, employees, attendance, analytics, admin
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError

log = logging.getLogger("attendance")


# =========================================================================== #
# 1. Configuration and database connection
# =========================================================================== #
# load_dotenv() does not override variables that already exist, so real
# environment variables always win over the .env file.
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees_col = db["employees"]
logs_col = db["attendance_logs"]


# =========================================================================== #
# 2. Constants
# =========================================================================== #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")

GRACE_SECONDS = 10 * 60          # R2: late only if strictly more than 10:00 after shift start
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = Decimal("4.50")  # R5
MAX_SHIFT_LENGTH = timedelta(hours=24)  # punch-out must be within 24h of punch-in
MAX_TREND_DAYS = 92

MIN_EPOCH_MS = 100_000_000_000   # below this the value looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
DateStr = Annotated[StrictStr, Field(pattern=DATE_PATTERN)]
HHMM = Annotated[StrictStr, Field(pattern=HHMM_PATTERN)]


# =========================================================================== #
# 3. Time, conversion and rounding helpers
# =========================================================================== #
def now_utc() -> datetime:
    """Current time, truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    """Aware or naive (assumed UTC) datetime -> epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def round_half_up(value: Any, places: int) -> Optional[float]:
    """R8 rounding in Python (not banker's rounding, not float rounding)."""
    if value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[date, date]:
    """'2026-07' -> (2026-07-01, 2026-07-31)."""
    first = date.fromisoformat(f"{month}-01")
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, next_month - timedelta(days=1)


def count_weekdays(start: date, end: date) -> int:
    """Number of Mon-Fri days in [start, end] (R7). Calendar math, not data access."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, extra = divmod(days, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def unprocessable(msg: str, loc: tuple = ("body",)) -> HTTPException:
    """A 422 in the same shape FastAPI uses for validation errors."""
    return HTTPException(status_code=422, detail=[{"type": "value_error", "loc": list(loc), "msg": msg}])


# =========================================================================== #
# 4. Business rules R1-R5
# =========================================================================== #
def is_overnight(emp: dict) -> bool:
    """shift_end <= shift_start means the shift ends on the next calendar day."""
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in, except that on an overnight shift a
    punch-in earlier than shift_end belongs to the shift that started the day before."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day


def shift_start_at(day: date, emp: dict) -> datetime:
    return datetime.combine(day, parse_hhmm(emp["shift_start"]), tzinfo=IST)


def shift_end_at(day: date, emp: dict) -> datetime:
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(emp) else end


def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole
    minutes (floor) counted from shift start."""
    seconds = int((punch_in - shift_start_at(day, emp)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: whole minutes (floor) after shift end, counted only when >= 30."""
    seconds = int((punch_out - shift_end_at(day, emp)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, rounded to 2 decimals half-up. Returned as Decimal so that
    R5 compares the exact rounded value."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  day: date, emp: dict) -> dict:
    """Derived fields for a record, per the table in DATA_MODEL.md section 3."""
    if status in NON_PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    derived = {
        "late_minutes": compute_late_minutes(punch_in, day, emp),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        derived["work_hours"] = float(hours)
        derived["overtime_minutes"] = compute_overtime(punch_out, day, emp)
        derived["half_day"] = hours < HALF_DAY_BELOW_HOURS  # R5
    return derived


def check_punch_out(punch_in: datetime, punch_out: datetime, field: str) -> None:
    if punch_out <= punch_in:
        raise unprocessable("punch_out must be after punch_in", ("body", field))
    if punch_out - punch_in > MAX_SHIFT_LENGTH:
        raise unprocessable("punch_out must be within 24 hours of punch_in", ("body", field))


# =========================================================================== #
# 5. Request validation
# =========================================================================== #
def check_date(value: Optional[str], name: str, where: str = "query") -> Optional[date]:
    """Strict YYYY-MM-DD that is also a real calendar date (2026-02-30 is rejected)."""
    if value is None:
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable("must be a valid date in YYYY-MM-DD format", (where, name))


def check_range(date_from: Optional[str], date_to: Optional[str], names=("date_from", "date_to")):
    start, end = check_date(date_from, names[0]), check_date(date_to, names[1])
    if start and end and start > end:
        raise unprocessable(f"{names[0]} must be on or before {names[1]}", ("query", names[0]))
    return start, end


class EmployeeIn(BaseModel):
    emp_code: Annotated[StrictStr, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    email: Annotated[StrictStr, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[StrictStr, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _check(self):
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Defaults are not validated, so an omitted field stays None while an explicit
    # null is rejected (these fields are not nullable in the contract).
    # Derived fields sent by a client are ignored (Pydantic drops unknown keys).
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: Annotated[StrictStr, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[StrictStr, Field(min_length=1, max_length=50)]


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=MONTH_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=50)]


# =========================================================================== #
# 6. Serializers (never expose _id; instants become epoch ms; fill legacy gaps)
# =========================================================================== #
TIME_FIELDS = ("punch_in", "punch_out")


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in TIME_FIELDS:
            change = {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
        changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    work_hours = doc.get("work_hours")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": float(work_hours) if work_hours is not None else None,
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# =========================================================================== #
# 7. Indexes and application startup
# =========================================================================== #
INDEXES = {
    employees_col: [
        ([("emp_code", ASCENDING)], {"unique": True}),               # identity, duplicate-create race
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {}),   # GET /employees?department, trend lookup
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {}),  # headcount for one department (R9)
        ([("joined_on", ASCENDING)], {}),                             # headcount for all departments (R9)
    ],
    logs_col: [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),  # one record per day, punch-in race,
                                                                              # punch-out, PATCH, monthly, $lookup
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),               # GET /attendance sort, month scans
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),  # GET /attendance?status
    ],
}
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    global _indexes_ready
    if _indexes_ready:
        return
    for collection, specs in INDEXES.items():
        for keys, options in specs:
            collection.create_index(keys, **options)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # do not crash the app; /health retries and reports 503
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def _db_unavailable(_: Request, exc: ConnectionFailure):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# =========================================================================== #
# 8. Query / pipeline builders (one builder per endpoint, reused by explain)
# =========================================================================== #
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def round_expr(expr: Any, places: int) -> dict:
    """R8 half-up rounding inside MongoDB. $round is banker's rounding, so instead:
    floor(x * 10^p + 0.5) / 10^p on Decimal128 values (x is never negative here)."""
    scale = 10 ** places
    return {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}},
        scale,
    ]}}


def weekday_expr(date_field: str) -> dict:
    """ISO day of week (1=Mon .. 7=Sun) of a 'YYYY-MM-DD' string field."""
    return {"$isoDayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}


def presence_weight_expr(prefix: str = "$") -> dict:
    """R5/R6: a present-status record counts 1, or 0.5 when it is a half day; else 0."""
    return {"$cond": [
        {"$in": [f"{prefix}status", list(PRESENCE_STATUSES)]},
        {"$cond": [{"$eq": [{"$ifNull": [f"{prefix}half_day", False]}, True]}, 0.5, 1]},
        0,
    ]}


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    check_range(date_from, date_to)
    flt: dict = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status:
        flt["status"] = status
    return {"filter": flt, "skip": (page - 1) * page_size, "limit": page_size}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$addFields": {"_weekday": {"$lte": [weekday_expr("$date"), 5]}}},
        {"$group": {
            "_id": None,
            # R7: only Mon-Fri records count towards present_days
            "present_days": {"$sum": {"$cond": ["$_weekday", presence_weight_expr(), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            # weekend records still add to late and overtime totals
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9 headcount, zero-log employees included
    if department is not None:
        match["department"] = department
    log_ = "$logs."
    late = {"$ifNull": [f"{log_}late_minutes", 0]}
    is_present = {"$in": [f"{log_}status", list(PRESENCE_STATUSES)]}
    return [
        {"$match": match},                                   # runs on `employees`
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
            "as": "logs",
        }},
        # keep employees with no logs: they still count in headcount
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present, {"$lte": [weekday_expr(f"{log_}date"), 5]}]},
                presence_weight_expr(log_), 0]}},
            # mean over records (not over per-employee means); $avg ignores nulls
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [is_present, {"$ne": [{"$ifNull": [f"{log_}work_hours", None]}, None]}]},
                {"$toDecimal": f"{log_}work_hours"}, None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "leave_count": {"$sum": {"$cond": [{"$eq": [f"{log_}status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": [f"{log_}status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": round_expr("$present_days", 2),
            "avg_work_hours": {"$cond": [{"$eq": ["$avg_work_hours", None]}, None,
                                         round_expr("$avg_work_hours", 2)]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    employee_match = [{"$match": {"department": department}}] if department is not None else []
    return [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},            # runs on `attendance_logs`
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": employee_match + [{"$project": {"_id": 0, "name": 1, "department": 1}}],
            "as": "emp",
        }},
        # drops logs of unknown employees and, with ?department, other departments,
        # so ranking happens inside the department only
        {"$unwind": "$emp"},
        # standard competition ranking: ties share a rank, next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},              # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]


def department_trend_pipeline(department: str, start: date, end: date, emp_codes: list) -> list:
    first, last = start.isoformat(), end.isoformat()
    seed = {"present_count": 0, "late_count": 0}
    rate = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        round_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
        None,
    ]}
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": first, "$lte": last}}},  # `attendance_logs`
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": presence_weight_expr()},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        # Add the two range ends so $densify has bounds even when there are no records at all.
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": first, **seed}, {"_id": last, **seed}]}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # exactly one row per calendar day, weekends and empty days included
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        # R9: department employees who had joined by that day (zero-log employees included)
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                         {"$count": "n"}],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$addFields": {"attendance_rate": rate}},
        # mean of non-null rates in the current row and up to 6 preceding rows of the range
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$toDouble": "$present_count"},
            "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None,
                                        round_expr("$moving_avg_7d", 4)]},
        }},
    ]


def require_employee(emp_code: str) -> dict:
    emp = employees_col.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def trend_inputs(department: str, from_: str, to: str) -> tuple[date, date, list]:
    start, end = check_range(from_, to, names=("from", "to"))
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    emp_codes = [e["emp_code"] for e in employees_col.find({"department": department}, {"_id": 0, "emp_code": 1})]
    if not emp_codes:
        raise HTTPException(404, "department not found")
    return start, end, emp_codes


# =========================================================================== #
# 9. Endpoints
# =========================================================================== #
# ---- system ---------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    try:
        ensure_indexes()  # retried here if the database was not reachable at startup
    except Exception as exc:
        log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees_col.insert_one(dict(doc))  # copy: insert_one would add _id to `doc`
    except DuplicateKeyError:                  # unique index -> race-safe
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    flt = {"department": department} if department is not None else {}
    cursor = (employees_col.find(flt, {"_id": 0}).sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(d) for d in cursor], "total": employees_col.count_documents(flt),
            "page": page, "page_size": page_size}


# ---- attendance ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        logs_col.insert_one(dict(doc))
    except DuplicateKeyError:  # unique (emp_code, date): exactly one concurrent request wins
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    record = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},  # null punch_in never matches $lte
        sort=[("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_out(record["punch_in"], ts, "punched_at")
    day = date.fromisoformat(record["date"])
    derived = derive_fields(record["status"], record["punch_in"], ts, day, emp)
    updated = logs_col.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # conditional: only one concurrent punch-out wins
        {"$set": {"punch_out": ts, **derived}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: Page = 1, page_size: PageSize = 20):
    q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    cursor = (logs_col.find(q["filter"], {"_id": 0}).sort(ATTENDANCE_SORT)
              .skip(q["skip"]).limit(q["limit"]))
    return {"items": [record_out(d) for d in cursor], "total": logs_col.count_documents(q["filter"]),
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[str, Path()], body: RegularizeIn):
    day = check_date(date, "date", where="path")
    emp = require_employee(emp_code)
    record = logs_col.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if record is None:
        raise HTTPException(404, "no attendance record for this date")

    old = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day", False)),
    }
    new_status = body.status or old["status"]
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if new_status in NON_PRESENCE_STATUSES:
        if new_in or new_out:
            raise unprocessable(f"punch times can not be set with status {new_status}")
        # ABSENT / LEAVE clears both punch times
    else:
        if new_in is None:
            new_in = old["punch_in"]
        else:
            if attendance_date(new_in, emp) != day:  # R1
                raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if new_out is None:
            new_out = old["punch_out"]
        if new_in is None:
            raise unprocessable("a presence status requires punch_in", ("body", "punch_in"))
        if new_out is not None:
            check_punch_out(new_in, new_out, "punch_out")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, day, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in old if old[f] != new[f]}
    if not changes:
        raise unprocessable("the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: update only if nobody changed the record since we read it.
    # The history length is part of the guard, so two concurrent corrections can not
    # both append: the second one matches nothing and gets a 409.
    guard = {"_id": record["_id"], "status": old["status"],
             "punch_in": old["punch_in"], "punch_out": old["punch_out"]}
    history = record.get("history")
    if isinstance(history, list):
        guard["history"] = {"$size": len(history)}
        update = {"$set": new, "$push": {"history": entry}}
    else:  # legacy record: history missing (or null) means empty
        guard["history"] = None
        update = {"$set": {**new, "history": [entry]}}
    updated = logs_col.find_one_and_update(
        guard,
        update,
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another request; retry")
    return record_out(updated)


# ---- analytics ------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = require_employee(emp_code)
    first, last = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)  # R7

    rows = list(logs_col.aggregate(employee_monthly_pipeline(emp_code, month)))
    stats = rows[0] if rows else {}
    present_days = stats.get("present_days", 0)
    pct = None
    if working_days:
        pct = round_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = list(employees_col.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    items = list(logs_col.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[str, Query(alias="from")],
                     to: Annotated[str, Query()]):
    start, end, emp_codes = trend_inputs(department, from_, to)
    items = list(logs_col.aggregate(department_trend_pipeline(department, start, end, emp_codes)))
    return {"department": department, "items": items}


# ---- admin ----------------------------------------------------------------- #
def run_explain(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


def require_params(**params) -> None:
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, detail=[{"type": "missing", "loc": ["query", m], "msg": "Field required"}
                                         for m in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q["filter"], "sort": dict(ATTENDANCE_SORT),
                   "skip": q["skip"], "limit": q["limit"]}
    elif endpoint == "employee_monthly":
        require_params(emp_code=emp_code, month=month)
        require_employee(emp_code)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        require_params(month=month)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(month, department),
                   "cursor": {}}
    elif endpoint == "late_leaderboard":
        require_params(month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(month, limit, department),
                   "cursor": {}}
    else:  # department_trend
        require_params(department=department, **{"from": from_}, to=to)
        start, end, emp_codes = trend_inputs(department, from_, to)
        collection = "attendance_logs"
        command = {"aggregate": collection,
                   "pipeline": department_trend_pipeline(department, start, end, emp_codes), "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
