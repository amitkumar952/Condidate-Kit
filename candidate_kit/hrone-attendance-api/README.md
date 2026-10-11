# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment (contract v2.0). All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB (real env vars win over .env)
uvicorn app.main:app --port 8000
```

Requires MongoDB 6.0+ (`$densify`, `$setWindowFields`, `$documents`). Indexes are created at startup, idempotently.

## Test

`tests/smoke_test.py` runs end-to-end checks against the running app with a hand-built dataset (ties, zero-log
employees, mid-month joiners, weekend records, overnight shifts, legacy records, empty trend ranges, parallel
requests). **It wipes the collections in `MONGO_DB`, so point it at a throwaway database.**

```bash
MONGO_DB=attendance_test uvicorn app.main:app --port 8000     # terminal 1
MONGO_DB=attendance_test python tests/smoke_test.py --big      # terminal 2 (--big adds ~100k records + explain checks)
```

## Notes

- `attendance_pct` is rounded to 2 decimals (it is a percentage; only `attendance_rate` and `moving_avg_7d` are rates with 4).
- The trend range is limited to 92 days inclusive (a full quarter is allowed).
- Half-up rounding inside pipelines is done on Decimal128 (`floor(x * 10^n + 0.5) / 10^n`), because `$round` uses banker's rounding.
- Nothing is missing from the contract.
