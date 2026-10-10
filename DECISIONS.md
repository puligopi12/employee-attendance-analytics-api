# DECISIONS.md

This document explains the key engineering decisions, architecture trade-offs, and design choices behind the Employee Attendance & Analytics API.

---

### 1. Database Indexes & Query Strategy

- **`employees.emp_code` (Unique ASCENDING)**: `emp_code` is the business primary key. An index-level unique constraint prevents race-condition duplicates during parallel creation requests and converts MongoDB `DuplicateKeyError` directly to HTTP 409.
- **`attendance_logs.(emp_code, date)` (Unique ASCENDING, ASCENDING)**: Enforces the core domain rule that each employee has at most one attendance record per IST attendance day. It also accelerates employee monthly lookups and regularizations.
- **`attendance_logs.(date DESCENDING, emp_code ASCENDING)`**: Powers the primary paginated attendance listing (`GET /attendance`), satisfying sort order and date-range queries via `IXSCAN` without in-memory sorting.
- **`employees.(department ASCENDING, joined_on ASCENDING)` & `employees.joined_on ASCENDING`**: Backs the department summary and trend pipelines by filtering active headcount (`joined_on <= period_end`) with high selectivity.
- **`attendance_logs.(emp_code ASCENDING, punch_in DESCENDING)`**: Provides instant lookup for the `/attendance/punch-out` query when finding the latest open punch-in.

*Trade-off*: We avoided indexing every query combination to prevent write throughput penalties and storage inflation while guaranteeing that all 5 critical explain paths use `IXSCAN` and 0 `COLLSCAN`.

---

### 2. Punch-In Race Conditions & Concurrency Control

When two identical punch-in requests arrive simultaneously:
- Both requests validate that the employee exists and compute the IST attendance date.
- Neither request executes a naive check-then-insert pattern (which would suffer from race conditions between check and write).
- Both attempt `db.attendance_logs.insert_one()`.
- MongoDB's atomic index check on `(emp_code, date)` allows exactly one document to succeed (HTTP 201) and immediately raises `DuplicateKeyError` on the second, which is caught and returned as HTTP 409.

Similarly, punch-out uses `find_one_and_update({"_id": candidate["_id"], "punch_out": None})`, guaranteeing that parallel punch-outs produce exactly one 200 and the other returns 409.

---

### 3. Tie-Breaking & Limit in the Late Leaderboard

- **Standard Competition Ranking (`$rank`)**:
  Using MongoDB 5.0+ `$setWindowFields` with the `$rank` operator, employees with the exact same total late minutes share the identical rank, and the subsequent rank is skipped (e.g., ranks 1, 2, 2, 4).
- **Post-Ranking Filtering**:
  The `limit` parameter is applied **after** computing the rank (`{"$match": {"rank": {"$lte": limit}}}`). If multiple employees tie at rank 10, all of them are returned.
- **Deterministic Row Ordering**:
  Output rows are sorted by `total_late_minutes DESCENDING`, followed by `emp_code ASCENDING` to guarantee stable ordering.

---

### 4. Headcount & Zero-Log Employees in Department Aggregations

- **Inclusion Strategy**:
  Per rule R9, department summary and daily trend calculations start from the `employees` collection filtered by `joined_on <= period_end`.
- **Preserving Zero-Log Employees**:
  Using `$lookup` to `attendance_logs` followed by `$unwind: {preserveNullAndEmptyArrays: true}`, employees who have never logged any punches are preserved in the pipeline. Their presence contribution is 0, but their headcount contribution is 1, ensuring accurate attendance percentage and average work hours calculations without division-by-zero errors.

---

### 5. Time Zones, Instants, and Overnight Shifts

- **API Layer**: All instants (`punch_in`, `punch_out`, `created_at`, `history[].at`) are strictly typed as integer epoch milliseconds. Milliseconds and microseconds are truncated to whole seconds (`value // 1000`) before calculations per R1.
- **Database Layer**: MongoDB stores instants as UTC BSON `Date` objects, keeping persistence daylight-saving independent and standards-compliant. Naive dates are forbidden.
- **Attendance Day & Overnight Shifts**: For shifts where `shift_end <= shift_start` (e.g., 22:00 to 06:00), a punch-in occurring after midnight but before `shift_end` IST belongs to the previous calendar day's shift. Work hours and overtime are calculated across the midnight boundary.

---

### 6. Scaling to 100x Data (~10,000,000 Records)

If the dataset scaled to 10 million attendance records:
1. **Targeted Compound Indexes**: The existing composite indexes already eliminate table scans. For high-volume multi-tenant scaling, we would shard `attendance_logs` using a hashed compound shard key on `(emp_code, date)`.
2. **Pre-Aggregated Rollups**: For department trend and monthly analytics, background change-streams or incremental daily rollups could maintain materialized daily summaries, avoiding multi-month log scans during real-time dashboard viewing.
3. **Optimized `$lookup`**: In large cross-collection joins, push down `$match` and projection stages inside the sub-pipeline before `$unwind` to minimize intermediate document sizes.
