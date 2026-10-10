"""
Employee Attendance & Analytics API.
Run from repository root: uvicorn app.main:app --port 8000
"""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
DERIVED = ("work_hours", "late_minutes", "overtime_minutes", "half_day")


def ensure_indexes():
    """Idempotent indexes; uniqueness is the concurrency guarantee for natural keys."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
                                    name="attendance_employee_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="attendance_date_status")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="attendance_status_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_punch_lookup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def fail_validation(message: str):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        fail_validation(f"{field} must be a valid YYYY-MM-DD date")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail_validation("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def month_bounds(value: str) -> tuple[str, str]:
    first, last = parse_month(value)
    return first.isoformat(), last.isoformat()


def validate_clock(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        raise ValueError("time must be HH:MM in 24-hour format")
    return value


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(quantum, rounding=ROUND_HALF_UP))


def epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.replace(microsecond=0).timestamp() * 1000)


def from_epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    if type(value) is not int or not 100_000_000_000 <= value <= 4_102_444_800_000:
        fail_validation("timestamp must be an integer epoch-milliseconds value in the supported range")
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def ist_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def attendance_date(punch_in: datetime, shift_start: str = "09:30", shift_end: str = "18:30") -> str:
    ist_dt = ist_instant(punch_in)
    d = ist_dt.date()
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        if ist_dt.time() < time(end_h, end_m):
            d = d - timedelta(days=1)
    return d.isoformat()


def shift_instant(day: str, clock: str, overnight: bool = False) -> datetime:
    local_day = parse_date(day)
    h, m = map(int, clock.split(":"))
    result = datetime.combine(local_day, time(h, m), tzinfo=IST)
    return result + (timedelta(days=1) if overnight else timedelta())


def late_minutes(punch_in: datetime | None, shift_start: str, day: str) -> int:
    if punch_in is None:
        return 0
    start = shift_instant(day, shift_start)
    local_in = ist_instant(punch_in)
    elapsed = int((local_in - start).total_seconds() // 60)
    return elapsed if (local_in - start).total_seconds() > 600 else 0


def work_hours(punch_in: datetime | None, punch_out: datetime | None) -> float | None:
    if punch_in is None or punch_out is None:
        return None
    pin = ist_instant(punch_in)
    pout = ist_instant(punch_out)
    seconds = (pout - pin).total_seconds()
    return round_half_up(Decimal(str(seconds)) / Decimal("3600"), 2)


def overtime_minutes(punch_out: datetime | None, shift_end: str, day: str, shift_start: str) -> int:
    if punch_out is None:
        return 0
    overnight = shift_end <= shift_start
    end = shift_instant(day, shift_end, overnight)
    pout = ist_instant(punch_out)
    elapsed_seconds = (pout - end).total_seconds()
    mins = int(elapsed_seconds // 60)
    return mins if mins >= 30 else 0


def compute_derived(record: dict, employee: dict) -> dict:
    status = record.get("status")
    pin = record.get("punch_in")
    pout = record.get("punch_out")
    if status not in PRESENCE:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(pin, employee["shift_start"], record["date"])
    hours = work_hours(pin, pout)
    overtime = overtime_minutes(pout, employee["shift_end"], record["date"], employee["shift_start"])
    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": overtime,
        "half_day": bool(hours is not None and hours < 4.50),
    }


def serialize(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, list):
        return [serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items() if k != "_id"}
    return value


def api_record(doc: dict) -> dict:
    result = dict(doc)
    result.setdefault("history", [])
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("work_hours", None)
    result.pop("_id", None)
    result.pop("id", None)
    return serialize(result)


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def month_working_days(joined_on: str, month: str) -> int:
    first, last = parse_month(month)
    start = max(first, parse_date(joined_on, "joined_on"))
    return working_days(start, last)


def page_args(page: int, page_size: int):
    if page < 1:
        fail_validation("page must be >= 1")
    if not 1 <= page_size <= 100:
        fail_validation("page_size must be between 1 and 100")


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmployeeIn(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def clock_format(cls, v):
        return validate_clock(v)

    @field_validator("joined_on")
    @classmethod
    def joined_date_format(cls, v):
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def shift_not_equal(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(APIModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def timestamp_cannot_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an epoch-milliseconds integer")
        return self


class PunchOutIn(APIModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)

    @model_validator(mode="after")
    def timestamp_cannot_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an epoch-milliseconds integer")
        return self


class RegularizeIn(APIModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def changed_values_cannot_be_null(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit it to leave unchanged")
        return self


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="unknown employee")
    instant = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_date(instant, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": instant, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(instant, employee["shift_start"], day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    doc["_id"] = result.inserted_id
    return api_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="unknown employee")
    instant = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": instant}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)]
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="no punch-in found before this timestamp")
    pin = candidate.get("punch_in")
    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance record is already punched out")
    pin_aware = ist_instant(pin) if pin else None
    instant_aware = ist_instant(instant)
    if instant_aware <= pin_aware or (instant_aware - pin_aware).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in and within 24 hours")
    derived = compute_derived({**candidate, "punch_out": instant}, employee)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None, "punch_in": pin},
        {"$set": {"punch_out": instant, **derived}},
        return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record was punched out concurrently")
    return api_record(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES:
        fail_validation("invalid status")
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        fail_validation("date_from must be <= date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [api_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


def snapshot_filter(doc: dict) -> dict:
    """Optimistic concurrency filter across every mutable value and history."""
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
              "overtime_minutes", "half_day", "history")
    clauses = []
    for field in fields:
        if field in doc:
            clauses.append({field: doc[field]})
        else:
            clauses.append({field: {"$exists": False}})
    return {"_id": doc["_id"], "$and": clauses}


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    parse_date(day, "date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="unknown employee")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not current:
        raise HTTPException(status_code=404, detail="attendance record not found")
    request = body.model_dump(exclude_unset=True)
    requested_status = request.get("status", current.get("status"))
    if requested_status in ("ABSENT", "LEAVE") and ("punch_in" in request or "punch_out" in request):
        raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot be supplied with punch times")
    pin = from_epoch_ms(request["punch_in"]) if "punch_in" in request else current.get("punch_in")
    pout = from_epoch_ms(request["punch_out"]) if "punch_out" in request else current.get("punch_out")
    if requested_status in ("ABSENT", "LEAVE"):
        pin = pout = None
    elif requested_status in PRESENCE and pin is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")
    if pin is not None and attendance_date(pin, employee["shift_start"], employee["shift_end"]) != day:
        raise HTTPException(status_code=422, detail="punch_in must fall on the attendance date in IST")
    if pout is not None:
        pin_aware = ist_instant(pin) if pin else None
        pout_aware = ist_instant(pout)
        if pin is None or pout_aware <= pin_aware or (pout_aware - pin_aware).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
    proposed = {**current, "status": requested_status, "punch_in": pin, "punch_out": pout}
    proposed.update(compute_derived(proposed, employee))
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    current_normalized = {
        "status": current.get("status"),
        "punch_in": epoch_ms(current.get("punch_in")),
        "punch_out": epoch_ms(current.get("punch_out")),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    proposed_normalized = {
        "status": proposed.get("status"),
        "punch_in": epoch_ms(proposed.get("punch_in")),
        "punch_out": epoch_ms(proposed.get("punch_out")),
        "work_hours": proposed.get("work_hours"),
        "late_minutes": proposed.get("late_minutes", 0),
        "overtime_minutes": proposed.get("overtime_minutes", 0),
        "half_day": proposed.get("half_day", False),
    }
    changes = {}
    for field in tracked:
        if current_normalized.get(field) != proposed_normalized.get(field):
            old_val = current.get(field) if field in ("punch_in", "punch_out") else current_normalized.get(field)
            new_val = proposed.get(field) if field in ("punch_in", "punch_out") else proposed_normalized.get(field)
            changes[field] = {"from": old_val, "to": new_val}
    if not changes:
        raise HTTPException(status_code=422, detail="request does not change the record")
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by,
                     "reason": body.reason, "changes": changes}
    set_values = {field: proposed.get(field) for field in tracked}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(current),
        {"$set": set_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry with latest values")
    return api_record(updated)


def monthly_logs_pipeline(emp_code: str, month: str) -> list[dict]:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE)]},
                          {"$gte": ["$date", first]}, {"$lte": ["$date", last]},
                          {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}}, [2,3,4,5,6]]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    return monthly_logs_pipeline(emp_code, month)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if not employee:
        raise HTTPException(status_code=404, detail="unknown employee")
    parse_month(month)
    aggregate = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    values = aggregate[0] if aggregate else {}
    working = month_working_days(employee["joined_on"], month)
    present = round_half_up(values.get("present_days", 0), 2)
    pct = round_half_up(present / working * 100, 2) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working,
            "present_days": present, "leave_days": int(values.get("leave_days", 0)),
            "late_count": int(values.get("late_count", 0)),
            "total_late_minutes": int(values.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(values.get("total_overtime_minutes", 0)),
            "attendance_pct": pct}


def department_summary_pipeline(month: str, department: str | None = None) -> list[dict]:
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    present_weekday = {"$and": [
        {"$in": ["$logs.status", list(PRESENCE)]},
        {"$ne": ["$logs.date", None]},
        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "onError": None, "onNull": None}}}, [2,3,4,5,6]]}
    ]}
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]}
            ]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"},
            "headcount": {"$first": 1},
            "present_days": {"$sum": {"$cond": [present_weekday,
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]},
                "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"}, "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"}, "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$round": ["$present_days", 2]},
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, department: str | None = None, limit: int = 10) -> list[dict]:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "employee.emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$employee.emp_code",
            "name": "$employee.name", "department": "$employee.department",
            "total_late_minutes": 1, "late_count": 1}}
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    parse_month(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(
        leaderboard_pipeline(month, department, limit), allowDiskUse=True))}


def department_trend_pipeline(department: str, from_day: str, to_day: str) -> list[dict]:
    start = datetime.combine(parse_date(from_day, "from"), time.min, tzinfo=UTC)
    end = datetime.combine(parse_date(to_day, "to"), time.min, tzinfo=UTC)
    day_count = (parse_date(to_day) - parse_date(from_day)).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {
            "emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"_id": 0, "employees": 1, "dates": {"$map": {
            "input": {"$range": [0, day_count]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$dates"},
        {"$addFields": {"date": {"$dateToString": {"date": "$dates", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$dates"}, 2]},
                                         {"$lte": [{"$dayOfWeek": "$dates"}, 6]}]},
            "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e",
                "cond": {"$lte": ["$$e.joined_on", {"$dateToString": {"date": "$dates", "format": "%Y-%m-%d", "timezone": "UTC"}}]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date", "codes": "$employees.emp_code"},
            "pipeline": [{"$match": {"$expr": {"$eq": ["$date", "$$day"]}}}], "as": "day_logs"}},
        {"$addFields": {"department_logs": {"$filter": {"input": "$day_logs", "as": "log",
            "cond": {"$in": ["$$log.emp_code", "$employees.emp_code"]}}}}},
        {"$addFields": {
            "present_count": {"$sum": {"$map": {"input": {"$filter": {"input": "$department_logs", "as": "l",
                "cond": {"$in": ["$$l.status", list(PRESENCE)]}}}, "as": "l",
                "in": {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1]}}}},
            "late_count": {"$size": {"$filter": {"input": "$department_logs", "as": "l",
                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}}
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"dates": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$round": ["$present_count", 2]}, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_day: str = Query(alias="from"), to_day: str = Query(alias="to")):
    first, last = parse_date(from_day, "from"), parse_date(to_day, "to")
    if last < first:
        fail_validation("to must be >= from")
    if (last - first).days + 1 > 92:
        fail_validation("date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")
    pipeline = department_trend_pipeline(department, from_day, to_day)
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


def explain_pipeline(endpoint: str, params: dict) -> tuple[str, list[dict] | None, dict | None]:
    """Return collection, pipeline (or find query/sort/skip/limit) for explain."""
    if endpoint == "attendance_list":
        emp_code, date_from, date_to, status = (params.get(k) for k in ("emp_code", "date_from", "date_to", "status"))
        if date_from: parse_date(date_from, "date_from")
        if date_to: parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            fail_validation("date_from must be <= date_to")
        if status is not None and status not in STATUSES:
            fail_validation("invalid status")
        query = {}
        if emp_code: query["emp_code"] = emp_code
        if status: query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from: query["date"]["$gte"] = date_from
            if date_to: query["date"]["$lte"] = date_to
        return "attendance_logs", None, {"filter": query, "sort": [("date", -1), ("emp_code", 1)],
            "skip": (params.get("page", 1)-1)*params.get("page_size", 20), "limit": params.get("page_size", 20)}
    if endpoint == "employee_monthly":
        if not params.get("emp_code") or not params.get("month"): fail_validation("emp_code and month are required")
        parse_month(params["month"])
        return "attendance_logs", employee_monthly_pipeline(params["emp_code"], params["month"]), None
    if endpoint == "department_summary":
        if not params.get("month"): fail_validation("month is required")
        parse_month(params["month"])
        return "employees", department_summary_pipeline(params["month"], params.get("department")), None
    if endpoint == "late_leaderboard":
        if not params.get("month"): fail_validation("month is required")
        parse_month(params["month"])
        return "attendance_logs", leaderboard_pipeline(params["month"], params.get("department"), params.get("limit", 10)), None
    if endpoint == "department_trend":
        department, start, end = params.get("department"), params.get("from"), params.get("to")
        if not department or not start or not end: fail_validation("department, from and to are required")
        d1, d2 = parse_date(start, "from"), parse_date(end, "to")
        if d2 < d1 or (d2-d1).days + 1 > 92: fail_validation("invalid trend date range")
        return "employees", department_trend_pipeline(department, start, end), None
    fail_validation("endpoint must be attendance_list, employee_monthly, department_summary, late_leaderboard, or department_trend")


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                  department: Optional[str] = None, date_from: Optional[str] = None,
                  date_to: Optional[str] = None, status: Optional[str] = None,
                  page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                  limit: int = Query(10, ge=1, le=50), from_day: Optional[str] = Query(None, alias="from"),
                  to_day: Optional[str] = Query(None, alias="to")):
    params = {"emp_code": emp_code, "month": month, "department": department,
              "date_from": date_from, "date_to": date_to, "status": status,
              "page": page, "page_size": page_size, "limit": limit,
              "from": from_day, "to": to_day}
    collection_name, pipeline, find_args = explain_pipeline(endpoint, params)
    try:
        if pipeline is not None:
            command = {"explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
                       "verbosity": "executionStats"}
        else:
            command = {"explain": {"find": collection_name, "filter": find_args["filter"],
                "sort": dict(find_args["sort"]), "skip": find_args["skip"], "limit": find_args["limit"]},
                "verbosity": "executionStats"}
        return {"endpoint": endpoint, "collection": collection_name, "explain": serialize(db.command(command))}
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"explain failed: {exc.__class__.__name__}")
