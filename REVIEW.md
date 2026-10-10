# REVIEW.md

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses a naive timestamp and does not consistently apply IST. | Run with UTC/IST boundary timestamps. | Normalize instants to IST and compare with the shift start on the attendance date. |
| 2 | `compute_late_minutes` | The strict 10-minute grace rule is not implemented precisely for seconds. | Test 09:40:00 and 09:40:01 for a 09:30 shift. | Late only when elapsed time is strictly greater than 600 seconds; minutes are floored from shift start. |
| 3 | `compute_overtime` | Replaces timezone on a date parsed from ISO without handling overnight shifts. | Shift 22:00–06:00 with punch-out next morning. | Construct the shift end in IST on the next calendar day when the shift crosses midnight. |
| 4 | `compute_overtime` | Counts overtime even when below the 30-minute minimum. | Punch out 29:59 after shift end. | Floor whole minutes and return overtime only when it is at least 30. |
| 5 | `compute_work_hours` | Python `round` uses ties-to-even and float rounding. | Test a duration exactly halfway at two decimals. | Use `Decimal` with `ROUND_HALF_UP`. |
| 6 | `create_employee` | Duplicate check before insert is race-prone. | Send parallel creates for the same `emp_code`. | Unique MongoDB index; convert duplicate-key exception to 409. |
| 7 | `create_employee` | Stores `created_at` as a naive datetime. | Inspect stored BSON date / API timestamp. | Store an aware UTC instant and serialize to epoch milliseconds. |
| 8 | `list_employees` | `skip` is `page * page_size`, which skips page one. | Request page 1 with 20 rows. | Use `(page - 1) * page_size`; total uses the applied filter. |
| 9 | `list_employees` | `total` ignores the department filter. | Filter a department with fewer employees than the whole company. | Count using the same query as the page fetch. |
| 10 | `punch_in` | Does not return 404 for an unknown employee before dereferencing it. | Punch in with an unknown code. | Check employee existence and return 404. |
| 11 | `punch_in` | Uses local machine time and the machine's timezone to derive the date. | Run on a non-IST host or submit an epoch near midnight UTC. | Use an explicit IST conversion for attendance date. |
| 12 | `punch_in` | Check-then-insert permits duplicate records under concurrency. | Parallel identical punch-in requests. | Enforce unique `(emp_code, date)` index and handle duplicate-key as 409. |
| 13 | `punch_in` | Accepts unsupported statuses and does not validate epoch-millisecond input. | Send `ABSENT`, a seconds timestamp, or a float. | Validate status and strict integer epoch milliseconds. |
| 14 | `list_attendance` | Fetches every matching record and sorts/slices in Python. | Profile with 100k records. | Use indexed MongoDB sort, skip and limit. |
| 15 | `list_attendance` | Leaves `_id` as ObjectId and BSON datetimes unconverted; omits legacy defaults. | Serialize a result with FastAPI. | Convert instants to epoch milliseconds, omit `_id`, and default legacy fields. |
| 16 | `list_attendance` | Missing date/status validation and date-range order checks. | Invalid dates or `date_from > date_to`. | Validate ISO dates/status and return 422. |
| 17 | Helper / all endpoints | No startup index creation. | Inspect indexes on a clean database. | Create idempotent indexes during startup. |
| 18 | `/health` | Reports success without checking MongoDB. | Stop MongoDB and call health. | Ping MongoDB and return 503 when unavailable. |
| 19 | Missing endpoints | Punch-out, regularization, analytics and explain contract are unimplemented. | Call any endpoint marked “You”. | Implement all contract endpoints and aggregation pipelines. |
| 20 | `admin_explain` | Did not wrap explain output in `{endpoint, collection, explain}` per OpenAPI contract. | Compare response keys with `openapi.yaml` schema. | Return `{"endpoint": endpoint, "collection": collection_name, "explain": ...}`. |
| 21 | `api_record` | Exposing artificial `id` property on attendance records. | Inspect JSON output of `/attendance` or punch endpoints. | Omit `_id` and remove `id`; attendance records have no ID per contract. |
| 22 | R1 second truncation | Stored datetimes and epoch ms retained milliseconds and microseconds. | Submit timestamp with milliseconds or omit `punched_at`. | Truncate instants to whole seconds (`value // 1000`, `replace(microsecond=0)`). |
| 23 | R1 overnight punch-in | `attendance_date` did not assign early-morning punch-in to previous day for overnight shifts. | Punch in at 01:00 for a 22:00-06:00 shift. | Pass shift times to `attendance_date` and decrement date if time < `shift_end`. |
| 24 | `department_summary` / `monthly_logs` | `$dateFromString` risks throwing an error when encountering null dates on unlogged employees. | Add employee with zero logs and run department summary. | Add `onError: null, onNull: null` and null checks in aggregation pipeline. |

**Reviewed and retained:** using `emp_code` rather than MongoDB `_id` as the business identifier is intentional. Calendar dates and shift times remain strings in storage; only actual instants are BSON dates. `history` is for manual corrections only, not ordinary punch events.

