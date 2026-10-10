import pytest
from fastapi.testclient import TestClient
from app.main import app, db


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_employees(client):
    response = client.get("/employees?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    if data["items"]:
        first = data["items"][0]
        assert "_id" not in first
        assert "emp_code" in first


def test_attendance_record_shape(client):
    response = client.get("/attendance?page=1&page_size=5")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    if data["items"]:
        item = data["items"][0]
        # Required OpenAPI schema fields
        for field in ("emp_code", "date", "status", "punch_in", "punch_out",
                      "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"):
            assert field in item, f"Missing required field {field}"
        # Assert neither _id nor id is exposed in the attendance record
        assert "_id" not in item
        assert "id" not in item


def test_admin_explain_contract(client):
    endpoints = [
        ("attendance_list", {"page": 1, "page_size": 10}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 5}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}),
    ]
    for ep, params in endpoints:
        response = client.get(f"/admin/explain/{ep}", params=params)
        assert response.status_code == 200, f"Explain failed for {ep}: {response.text}"
        data = response.json()
        assert "endpoint" in data
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        assert isinstance(data["explain"], dict)


def test_department_summary_with_zero_log_employee(client):
    # Insert a dummy employee who has no attendance logs
    emp = {
        "emp_code": "EMP9998",
        "name": "Zero Logs Test",
        "email": "zerologs@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    db.employees.delete_one({"emp_code": emp["emp_code"]})
    db.employees.insert_one(emp)
    try:
        response = client.get("/analytics/departments/summary?month=2026-07")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        eng = next((d for d in data["items"] if d["department"] == "Engineering"), None)
        assert eng is not None
        assert eng["headcount"] >= 1
    finally:
        db.employees.delete_one({"emp_code": emp["emp_code"]})


def test_mandatory_october_2026_wfh_scenario(client):
    """
    Mandatory test scenario:
    - Test employee joining date before October 2026, shift 09:30-18:30.
    - One valid WFH attendance record for Friday, October 9, 2026.
    - October 2026: 22 working days, 1 present day, 0 leave days, 0 late count.
    - Attendance percentage: ~4.55% (rounded to 2 decimals per R8: 4.55).
    """
    emp = {
        "emp_code": "EMP8001",
        "name": "Aarav Sharma",
        "email": "aarav@example.com",
        "department": "Quality",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    }
    db.employees.delete_one({"emp_code": emp["emp_code"]})
    db.attendance_logs.delete_many({"emp_code": emp["emp_code"]})
    try:
        resp = client.post("/employees", json=emp)
        assert resp.status_code == 201

        # Friday, Oct 9, 2026 09:30 IST = 04:00 UTC = 1791518400000 ms
        punch_time = 1791518400000
        punch_resp = client.post("/attendance/punch-in", json={
            "emp_code": emp["emp_code"],
            "punched_at": punch_time,
            "status": "WFH",
        })
        assert punch_resp.status_code == 201
        log = punch_resp.json()
        assert log["date"] == "2026-10-09"
        assert log["status"] == "WFH"
        assert log["late_minutes"] == 0

        # Query monthly analytics
        monthly_resp = client.get(f"/analytics/employees/{emp['emp_code']}/monthly?month=2026-10")
        assert monthly_resp.status_code == 200
        stats = monthly_resp.json()
        assert stats["emp_code"] == emp["emp_code"]
        assert stats["month"] == "2026-10"
        assert stats["working_days"] == 22
        assert stats["present_days"] == 1.0
        assert stats["leave_days"] == 0
        assert stats["late_count"] == 0
        assert stats["total_late_minutes"] == 0
        assert stats["total_overtime_minutes"] == 0
        assert stats["attendance_pct"] == 4.55
    finally:
        db.employees.delete_one({"emp_code": emp["emp_code"]})
        db.attendance_logs.delete_many({"emp_code": emp["emp_code"]})


def test_punch_lifecycle_overtime_and_half_day(client):
    emp = {
        "emp_code": "EMP8002",
        "name": "Pooja Patel",
        "email": "pooja@example.com",
        "department": "Quality",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    }
    db.employees.delete_one({"emp_code": emp["emp_code"]})
    db.attendance_logs.delete_many({"emp_code": emp["emp_code"]})
    try:
        client.post("/employees", json=emp)

        # 1. Half day test (< 4.5 hours)
        # Punch in at 09:30 IST (04:00 UTC), punch out at 13:30 IST (4.00 hours)
        pin_half = 1791518400000
        pout_half = 1791518400000 + (4 * 3600 * 1000)
        client.post("/attendance/punch-in", json={"emp_code": emp["emp_code"], "punched_at": pin_half})
        out_resp = client.post("/attendance/punch-out", json={"emp_code": emp["emp_code"], "punched_at": pout_half})
        assert out_resp.status_code == 200
        rec = out_resp.json()
        assert rec["work_hours"] == 4.0
        assert rec["half_day"] is True
        assert rec["overtime_minutes"] == 0

        # 2. Duplicate punch in on same day rejected with 409
        dup_resp = client.post("/attendance/punch-in", json={"emp_code": emp["emp_code"], "punched_at": pin_half})
        assert dup_resp.status_code == 409

        # 3. Punch out again on already punched out record rejected with 409
        dup_out = client.post("/attendance/punch-out", json={"emp_code": emp["emp_code"], "punched_at": pout_half})
        assert dup_out.status_code == 409
    finally:
        db.employees.delete_one({"emp_code": emp["emp_code"]})
        db.attendance_logs.delete_many({"emp_code": emp["emp_code"]})


def test_regularize_endpoint(client):
    emp = {
        "emp_code": "EMP8003",
        "name": "Rohan Gupta",
        "email": "rohan@example.com",
        "department": "Operations",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    }
    db.employees.delete_one({"emp_code": emp["emp_code"]})
    db.attendance_logs.delete_many({"emp_code": emp["emp_code"]})
    try:
        client.post("/employees", json=emp)
        # Punch in late at 10:05 IST (04:35 UTC) on 2026-10-12 (Monday)
        # 2026-10-12 04:35:00 UTC = 1791779700000
        pin_late = 1791779700000
        client.post("/attendance/punch-in", json={"emp_code": emp["emp_code"], "punched_at": pin_late})

        # Regularize punch_in back to 09:30 IST (04:00 UTC = 1791777600000)
        pin_on_time = 1791777600000
        patch_resp = client.patch(
            f"/attendance/{emp['emp_code']}/2026-10-12",
            json={
                "punch_in": pin_on_time,
                "reason": "Traffic jam and biometric machine issue",
                "regularized_by": "hr_manager",
            }
        )
        assert patch_resp.status_code == 200
        data = patch_resp.json()
        assert data["late_minutes"] == 0
        assert len(data["history"]) == 1
        hist = data["history"][0]
        assert hist["by"] == "hr_manager"
        assert hist["reason"] == "Traffic jam and biometric machine issue"
        assert "late_minutes" in hist["changes"]
        assert hist["changes"]["late_minutes"]["from"] == 35
        assert hist["changes"]["late_minutes"]["to"] == 0

        # Request that changes nothing returns 422
        noop_resp = client.patch(
            f"/attendance/{emp['emp_code']}/2026-10-12",
            json={
                "punch_in": pin_on_time,
                "reason": "Duplicate change attempt",
                "regularized_by": "hr_manager",
            }
        )
        assert noop_resp.status_code == 422
    finally:
        db.employees.delete_one({"emp_code": emp["emp_code"]})
        db.attendance_logs.delete_many({"emp_code": emp["emp_code"]})


def test_leaderboard_competition_ranking(client):
    # Verify leaderboard endpoint structure and department filtering
    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert "month" in data
    assert "items" in data
    assert isinstance(data["items"], list)
    for row in data["items"]:
        assert "rank" in row
        assert "emp_code" in row
        assert "total_late_minutes" in row
        assert row["total_late_minutes"] > 0


def test_department_trend_gap_filling_and_moving_avg(client):
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7  # All 7 calendar days must exist
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates)
    for row in data["items"]:
        assert "is_working_day" in row
        assert "headcount" in row
        assert "attendance_rate" in row
        assert "moving_avg_7d" in row

