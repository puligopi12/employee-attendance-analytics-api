# Employee Attendance & Analytics API

FastAPI + PyMongo + MongoDB 6.0+ production-grade implementation for the HROne Software Engineer assignment.

---

## 1. Project Overview & Business Problem

Organizations require precise, tamper-evident attendance tracking to monitor employee shift compliance, calculate actual work hours, capture late arrivals and overtime, handle administrative regularizations with audit trails, and aggregate monthly and departmental analytics.

Manual attendance calculations or naive datetime comparisons lead to errors, especially around time zone boundaries and overnight shifts. This API solves these problems through:
- Strict timezone modeling (`Asia/Kolkata` IST vs UTC BSON storage vs Epoch Millisecond API contracts).
- Robust race-condition handling backed by MongoDB compound unique indexes.
- Standard competition ranking and moving average analytics computed completely inside MongoDB aggregations (no Python in-memory loops).

---

## 2. Technology Stack & Prerequisites

- **Python**: 3.11+ (Tested on Python 3.12)
- **Framework**: FastAPI (Asynchronous REST API, Pydantic v2 validation)
- **Database**: MongoDB 6.0+ (Tested on MongoDB 7.0.12 Community)
- **Driver**: PyMongo 4.7+
- **Testing**: Pytest 8.0+, HTTPX

---

## 3. Architecture & Data Flow

```
Client / Swagger UI (http://127.0.0.1:8000/docs)
      │
      ▼
FastAPI Routing & Pydantic Validation (Type, Range, Epoch Ms, Clock Regex)
      │
      ▼
Python Business Logic (IST shift boundaries, R1-R5 calculations, Decimal Half-Up)
      │
      ▼
PyMongo Driver (BSON UTC Date persistence & MongoDB Aggregation Pipelines)
      │
      ▼
MongoDB Collections (`employees`, `attendance_logs` with compound unique indexes)
```

### Collections & Natural Keys
- **`employees`**: One document per employee. Primary business key: `emp_code` (unique index `uq_employee_code`).
- **`attendance_logs`**: One document per (`emp_code`, `date`). Unique natural compound index: `uq_attendance_employee_date`.

---

## 4. Environment Configuration & Setup

### Environment Variables
Configure via system environment variables or local `.env` file (system variables take precedence):
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Database name (default: `attendance_db`)

### Installation & Run Instructions (Windows PowerShell)

```powershell
# 1. Clone repository and navigate to folder
cd SDT_candidate_kit

# 2. Create virtual environment (.venv)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements-dev.txt

# 4. Set environment variables
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"

# 5. Seed sample data
python sample_seed.py

# 6. Run the API server
uvicorn app.main:app --port 8000 --reload
```

Interactive Swagger UI documentation is available at:
👉 **`http://127.0.0.1:8000/docs`**

---

## 5. Attendance Business Rules (R1 - R10)

1. **R1 Time Zone & Attendance Date**:
   - Attendance dates and shifts are in IST (UTC+05:30).
   - Instants in requests/responses are integer epoch milliseconds; stored as UTC BSON datetimes.
   - For overnight shifts (`shift_end <= shift_start`, e.g., 22:00-06:00), a punch-in before `shift_end` IST belongs to the shift starting the previous calendar day.
   - Instants are truncated to whole seconds (`value // 1000`, `microsecond=0`).
2. **R2 Late Arrival**:
   - 10-minute grace period. Punch-in strictly greater than 10:00 after `shift_start` is late.
   - `late_minutes` = whole minutes elapsed from `shift_start` (e.g., 09:40:00 -> 0; 09:40:01 -> 10).
3. **R3 Overtime**:
   - Whole minutes between `shift_end` and `punch_out`, counted only when >= 30 minutes; otherwise 0.
4. **R4 Work Hours**:
   - `(punch_out - punch_in)` in seconds / 3600, rounded to 2 decimal places **half-up** using `Decimal`.
5. **R5 Half Day**:
   - `work_hours < 4.50` yields `half_day = True`. A half-day contributes 0.5 to present days.
6. **R6 Presence Statuses**:
   - `PRESENT`, `WFH`, `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.
7. **R7 Working Days**:
   - Monday to Friday. Mid-month joiners count working days from `joined_on`. Present days count only Mon-Fri logs.
8. **R8 Rounding**:
   - 2 decimals half-up for counts/percentages; 4 decimals half-up for rates and moving averages.
9. **R9 Headcount**:
   - Active employees whose `joined_on <= period_end`, including employees with 0 logs.
10. **R10 Pagination**:
    - `page` >= 1, `page_size` 1-100 (default 20), `total` reflects filtered count.

---

## 6. API Endpoints Reference

| Method | Path | Description | Key Status Codes |
|---|---|---|---|
| `GET` | `/health` | Readiness probe; pings MongoDB | 200, 503 |
| `POST` | `/employees` | Create employee (`emp_code` unique) | 201, 409, 422 |
| `GET` | `/employees` | List employees with pagination and department filter | 200, 422 |
| `POST` | `/attendance/punch-in` | Punch-in record creation; race-safe | 201, 404, 409, 422 |
| `POST` | `/attendance/punch-out` | Punch-out record update; derives work hours & overtime | 200, 404, 409, 422 |
| `GET` | `/attendance` | List attendance records with date range and status filters | 200, 422 |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize record; appends to history, recomputes derived fields | 200, 404, 409, 422 |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary via aggregation | 200, 404, 422 |
| `GET` | `/analytics/departments/summary` | Department summary across all eligible employees | 200, 422 |
| `GET` | `/analytics/leaderboard/late` | Competition-ranked late arrivals (`$setWindowFields`) | 200, 422 |
| `GET` | `/analytics/departments/{dept}/trend` | Daily attendance trend with gap filling and 7d moving average | 200, 404, 422 |
| `GET` | `/admin/explain/{endpoint}` | MongoDB execution stats (`IXSCAN` plan verification) | 200, 422, 500 |

---

## 7. Running Automated Tests

Run the full automated test suite using `pytest`:

```powershell
.\.venv\Scripts\pytest -v
```

All 18 unit and integration tests will execute against local MongoDB, verifying:
- Shift time and late grace boundaries (09:40:00 vs 09:40:01).
- Decimal half-up rounding.
- Overnight shift boundary detection and overtime on the next calendar day.
- Mandatory October 2026 WFH scenario.
- Punch lifecycle, half-day (< 4.5h), and concurrency conflicts (409).
- Regularization audit history and no-op 422 rejections.
- Leaderboard competition ranking ties.
- Department trend gap-filling and 7-day moving averages.
- Execution plans asserting `IXSCAN` and zero `COLLSCAN`.
