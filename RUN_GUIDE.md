# Complete Execution & Operational Guide

## Employee Attendance & Analytics API (FastAPI + PyMongo + MongoDB)

This document provides a complete, step-by-step guide on how to set up, seed, run, test, and verify the Employee Attendance & Analytics API project.

---

## 1. Prerequisites

Before running the application, make sure you have:
- **Python 3.11+** installed (check with `python --version`).
- **MongoDB 6.0+ or 7.0+** running locally (default: `mongodb://localhost:27017`) or a MongoDB Atlas connection string.
- (Optional) **MongoDB Compass** or `mongosh` for visual database inspection.

---

## 2. Quick Setup & Run (Step-by-Step)

### Option A: Windows PowerShell (Recommended)

1. **Navigate to the project root directory**:
   ```powershell
   cd c:\Users\pulig\Downloads\SDT_candidate_kit_solution\SDT_candidate_kit
   ```

2. **Create and activate the virtual environment**:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   *(If PowerShell gives an execution policy error, run: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`)*

3. **Install dependencies**:
   ```powershell
   python -m pip install -r requirements-dev.txt
   ```

4. **Configure environment variables**:
   ```powershell
   $env:MONGO_URI = "mongodb://localhost:27017"
   $env:MONGO_DB = "attendance_db"
   ```

5. **Load sample data into MongoDB**:
   ```powershell
   python sample_seed.py
   ```
   *Expected output: `loaded 6 documents into 'attendance_db.employees'` and `loaded 9 documents into 'attendance_db.attendance_logs'`.*

6. **Start the FastAPI application**:
   ```powershell
   uvicorn app.main:app --port 8000 --reload
   ```

---

### Option B: Linux / macOS (Bash)

1. **Navigate to the directory**:
   ```bash
   cd SDT_candidate_kit
   ```

2. **Create and activate virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements-dev.txt
   ```

4. **Set environment variables & seed**:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   python sample_seed.py
   ```

5. **Run the server**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

---

## 3. Interactive API Documentation & Health Check

Once the server is running:
- **Interactive Swagger UI**: Open your browser at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc UI**: Open [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
- **Health Check Probe**:
  ```powershell
  curl http://127.0.0.1:8000/health
  ```
  Response:
  ```json
  {"status": "ok"}
  ```

---

## 4. Running Automated Tests

To run the complete automated test suite (including unit tests, helpers, and end-to-end API tests):

```powershell
pytest -v
```

All 13 test suites validate:
- ✅ Late arrival calculation with strict 10-minute grace period.
- ✅ Work hours decimal rounding (half-up).
- ✅ Overnight shift overtime and next-day boundary calculations.
- ✅ Working day calculations (Mon-Fri).
- ✅ Aggregation pipelines for trends, window functions, and ranking.
- ✅ Late leaderboard competition ranking with tie handling.
- ✅ R1 Overnight shift punch-in attendance date allocation.
- ✅ R1 Whole second instant truncation.
- ✅ Clean attendance record shape (omits `_id` and does not leak `id`).
- ✅ Strict conformity with the OpenAPI explain plan contract (`endpoint`, `collection`, `explain`).
- ✅ Department summary robustness when employees have zero attendance logs.

---

## 5. Summary of Bugs Identified & Fixed

During code analysis and testing against `openapi.yaml` and `DATA_MODEL.md`, the following issues were resolved:

| # | Component | Problem | Fix Implemented |
|---|---|---|---|
| 1 | `admin_explain` | Did not follow OpenAPI schema (`required: [endpoint, collection, explain]`); returned raw dict directly. | Wrapped response in `{"endpoint": endpoint, "collection": collection_name, "explain": ...}`. |
| 2 | `api_record` | Inappropriately assigned and returned an `id` field, violating `AttendanceRecord` schema. | Removed `id` field; attendance records are identified by `(emp_code, date)` natural key. |
| 3 | Rule R1 (Truncation) | Instants retained sub-second milliseconds/microseconds in storage and output. | Truncated all instants to whole seconds (`value // 1000`, `replace(microsecond=0)`). |
| 4 | Rule R1 (Overnight Shifts) | `attendance_date` did not consider employee shift times; early morning punches were assigned to the wrong date. | Updated `attendance_date` to check `shift_end <= shift_start` and assign early-morning punches to the shift's starting day. |
| 5 | `department_summary_pipeline` | `$dateFromString` threw fatal query exceptions when evaluated on null dates for employees with no logs. | Added null checks and `onError: null, onNull: null` error guards. |
| 6 | `overtime_minutes` & `work_hours` | Naive datetime objects from unmanaged seeds caused Python `TypeError` on subtraction. | Normalized all timestamps with `ist_instant`. |
| 7 | `regularize` | Legacy documents with omitted fields incorrectly registered artificial audit changes. | Normalized default fields for change tracking and ensured BSON date preservation in `history`. |
| 8 | FastAPI Startup | `@app.on_event("startup")` triggered deprecation warnings. | Migrated to FastAPI `lifespan` context manager. |

---

## 6. Testing Endpoints Manually

### A. Punch In
```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/attendance/punch-in" -ContentType "application/json" -Body '{"emp_code": "EMP0001", "status": "PRESENT"}'
```

### B. Punch Out
```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/attendance/punch-out" -ContentType "application/json" -Body '{"emp_code": "EMP0001"}'
```

### C. Monthly Employee Analytics
```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/analytics/employees/EMP0001/monthly?month=2026-07"
```

### D. Department Monthly Summary
```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/analytics/departments/summary?month=2026-07"
```

### E. Late Leaderboard
```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-07&limit=5"
```

### F. Department Trend (Daily moving average)
```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07"
```

### G. Query Explain Plans
```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/admin/explain/attendance_list?page=1&page_size=20"
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/admin/explain/department_summary?month=2026-07"
```
