"""
Employee Attendance & Analytics API

Starts with: uvicorn app.main:app --port 8000
Stack: Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+
"""

import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
import os
import re
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Database Initialization & Indexes
# --------------------------------------------------------------------------- #
def init_db():
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_dept_emp_code")
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_joined_on")

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="idx_emp_date_unique"
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="idx_date_desc_emp_asc"
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
            name="idx_emp_punch_in_desc"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("status", ASCENDING)],
            name="idx_date_status"
        )
    except Exception as e:
        # Avoid crashing startup if connection is initially connecting
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Domain Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[Union[float, int, Decimal]], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_ms_to_utc_dt(epoch_ms: int) -> datetime:
    # R1: Instants are truncated to whole seconds before any calculation
    truncated_sec = epoch_ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=UTC)


def utc_dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def now_epoch_ms() -> int:
    sec = int(datetime.now(tz=UTC).timestamp())
    return sec * 1000


def get_ist_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """R1: Attendance date in IST. Handles overnight shifts (shift_end <= shift_start)."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end_str <= shift_start_str
    if is_overnight:
        eh, em = map(int, shift_end_str.split(":"))
        shift_end_time = time(hour=eh, minute=em)
        if punch_in_ist.time() < shift_end_time:
            # Belongs to the shift that started the previous calendar day
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start_str: str, date_str: str) -> int:
    """R2: Grace period 10 minutes. Strictly more than 10:00 (600s) -> floor minutes since shift_start."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start_str.split(":"))
    shift_start_dt = datetime.fromisoformat(date_str).replace(hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return math.floor(elapsed_seconds / 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours_dec = Decimal(str(int(seconds))) / Decimal("3600")
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_end_str: str, date_str: str, shift_start_str: str) -> int:
    """R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30, else 0."""
    punch_out_ist = punch_out_utc.astimezone(IST)
    eh, em = map(int, shift_end_str.split(":"))
    shift_end_date = datetime.fromisoformat(date_str)
    if shift_end_str <= shift_start_str:
        # Overnight shift ends on the next calendar day
        shift_end_date += timedelta(days=1)
    shift_end_dt = shift_end_date.replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    mins = math.floor(elapsed_seconds / 60)
    if mins >= 30:
        return mins
    return 0


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes = {}
    for k, v in entry.get("changes", {}).items():
        if k in ("punch_in", "punch_out"):
            changes[k] = {
                "from": utc_dt_to_epoch_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                "to": utc_dt_to_epoch_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to")
            }
        else:
            changes[k] = {
                "from": v.get("from"),
                "to": v.get("to")
            }
    return {
        "at": utc_dt_to_epoch_ms(entry["at"]) if isinstance(entry.get("at"), datetime) else entry.get("at"),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])]
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v, info):
        shift_start = info.data.get("shift_start")
        if shift_start and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at_dt = epoch_ms_to_utc_dt(now_epoch_ms())
    doc = body.model_dump()
    doc["created_at"] = created_at_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": utc_dt_to_epoch_ms(doc["created_at"])
    }


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort([("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = []
    for doc in cursor:
        doc["created_at"] = utc_dt_to_epoch_ms(doc.get("created_at"))
        items.append(doc)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else now_epoch_ms()
    punch_in_dt = epoch_ms_to_utc_dt(punch_ms)

    attendance_date = get_ist_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else now_epoch_ms()
    punch_out_dt = epoch_ms_to_utc_dt(punch_ms)

    # Closes employee's most recent record whose punch_in <= punched_at
    latest_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )
    if not latest_record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if latest_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_dt = latest_record["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    wh = compute_work_hours(punch_in_dt, punch_out_dt)
    ot = compute_overtime(punch_out_dt, emp["shift_end"], latest_record["date"], emp["shift_start"])
    hd = wh < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": latest_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return serialize_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for that date")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot provide punch times when status is ABSENT or LEAVE")
        target_punch_in = None
        target_punch_out = None
    else:
        target_punch_in = epoch_ms_to_utc_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        target_punch_out = epoch_ms_to_utc_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    if target_status in ("PRESENT", "WFH", "ON_DUTY") and target_punch_in is None:
        raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    if target_punch_in is not None:
        computed_date = get_ist_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if target_punch_out is not None:
        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        if target_punch_out <= target_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(target_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    changes = {}
    if target_status != record.get("status"):
        changes["status"] = {"from": record.get("status"), "to": target_status}
    if target_punch_in != record.get("punch_in"):
        changes["punch_in"] = {"from": record.get("punch_in"), "to": target_punch_in}
    if target_punch_out != record.get("punch_out"):
        changes["punch_out"] = {"from": record.get("punch_out"), "to": target_punch_out}
    if new_late_minutes != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if new_work_hours != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_overtime_minutes != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request made no changes to the record")

    now_dt = epoch_ms_to_utc_dt(now_epoch_ms())
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Concurrent safety: ensure history is preserved and loser returns 409
    current_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": current_history},
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict on attendance record")

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def get_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6]
                    ]
                },
                "is_present": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                }
            }
        },
        {
            "$project": {
                "present_contrib": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                },
                "leave_contrib": {
                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                },
                "late_contrib": {
                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                },
                "late_minutes": 1,
                "overtime_minutes": 1
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_contrib"},
                "leave_days": {"$sum": "$leave_contrib"},
                "late_count": {"$sum": "$late_contrib"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start_date = date(y, m, 1)
    month_end_date = date(y, m, num_days)

    joined_date = date.fromisoformat(emp["joined_on"])
    effective_start = max(month_start_date, joined_date)

    working_days = 0
    if joined_date <= month_end_date:
        curr = effective_start
        while curr <= month_end_date:
            if curr.weekday() < 5:  # Mon-Fri
                working_days += 1
            curr += timedelta(days=1)

    pipeline = get_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = round_half_up(row.get("present_days", 0.0), 2)
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct
    }


def get_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "log_status": "$logs.status",
                "log_late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "log_work_hours": "$logs.work_hours",
                "log_half_day": {"$ifNull": ["$logs.half_day", False]},
                "log_is_weekday": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {
                            "$in": [
                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                [2, 3, 4, 5, 6]
                            ]
                        },
                        False
                    ]
                },
                "log_is_present": {
                    "$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$log_is_weekday", "$log_is_present"]},
                            {"$cond": ["$log_half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$log_late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": "$log_late_minutes"
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "ON_DUTY"]}, 1, 0]}
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    "$log_is_present",
                                    {"$ne": ["$log_work_hours", None]}
                                ]
                            },
                            "$log_work_hours",
                            "$$REMOVE"
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": {"$size": "$employees"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count"
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    pipeline = get_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)
    items = []
    for doc in cursor:
        if doc["headcount"] == 0:
            continue
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": round_half_up(doc["present_days"], 2),
            "avg_work_hours": round_half_up(doc["avg_work_hours"], 2),
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"]
        })
    return {"month": month, "items": items}


def get_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                }
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count"
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "emp_code": ASCENDING
            }
        }
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = get_late_leaderboard_pipeline(month, limit, department)
    try:
        docs = list(db.attendance_logs.aggregate(pipeline))
    except PyMongoError:
        # Fallback for environments with window restrictions (e.g. Atlas M0)
        docs = _run_late_leaderboard_fallback(month, limit, department)

    items = []
    for doc in docs:
        items.append({
            "rank": doc["rank"],
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": doc["total_late_minutes"],
            "late_count": doc["late_count"]
        })
    return {"month": month, "items": items}


def _run_late_leaderboard_fallback(month: str, limit: int, department: Optional[str]) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_stage = {"date": {"$gte": start_date, "$lte": end_date}}
    base_pipe = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"}
    ]
    if department:
        base_pipe.append({"$match": {"emp.department": department}})

    base_pipe.append({"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}})
    raw = list(db.attendance_logs.aggregate(base_pipe))

    # Standard competition ranking: 1, 2, 2, 4
    result = []
    current_rank = 1
    prev_late = None
    for idx, r in enumerate(raw):
        if prev_late is None or r["total_late_minutes"] != prev_late:
            current_rank = idx + 1
            prev_late = r["total_late_minutes"]

        if current_rank <= limit:
            result.append({
                "rank": current_rank,
                "emp_code": r["_id"],
                "name": r["emp"]["name"],
                "department": r["emp"]["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"]
            })
    return result


def get_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    # Pure MongoDB pipeline utilizing $densify and $setWindowFields
    return [
        {
            "$match": {
                "department": department
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": "$logs"},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
                "_id": 0
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.fromisoformat(from_date).replace(tzinfo=UTC),
                        datetime.fromisoformat(to_date).replace(tzinfo=UTC) + timedelta(days=1)
                    ]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_dt"}
            }
        },
        {
            "$sort": {"date": ASCENDING}
        }
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    if to < from_:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    d_from = date.fromisoformat(from_)
    d_to = date.fromisoformat(to)
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Check department exists
    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    # Query attendance aggregation
    pipeline = get_department_trend_pipeline(department, from_, to)
    try:
        raw_trend = list(db.employees.aggregate(pipeline))
        # Build date indexed map from raw pipeline results
        data_by_date = {r["date"]: r for r in raw_trend}
    except PyMongoError:
        # Fallback query for MongoDB Atlas M0 restricted environments
        data_by_date = _fetch_trend_data_mongo(department, from_, to)

    # Assemble series exactly one row per day from `from_` to `to`
    items = []
    rates = []
    curr = d_from
    while curr <= d_to:
        d_str = curr.isoformat()
        is_working_day = curr.weekday() < 5

        # Headcount: department employees with joined_on <= that day (R9)
        headcount = sum(1 for e in dept_emps if e.get("joined_on", "") <= d_str)

        log_data = data_by_date.get(d_str, {})
        present_count = round_half_up(log_data.get("present_count", 0.0), 2)
        late_count = int(log_data.get("late_count", 0))

        if is_working_day and headcount > 0:
            rate = round_half_up(present_count / headcount, 4)
        else:
            rate = None

        rates.append(rate)

        # 7-day moving average over current and up to 6 preceding rows in requested range
        window_rates = [r for r in rates[-7:] if r is not None]
        if window_rates:
            moving_avg = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            moving_avg = None

        items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg
        })
        curr += timedelta(days=1)

    return {"department": department, "items": items}


def _fetch_trend_data_mongo(department: str, from_date: str, to_date: str) -> Dict[str, Dict[str, Any]]:
    dept_emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
    pipe = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        }
    ]
    docs = list(db.attendance_logs.aggregate(pipe))
    return {d["_id"]: d for d in docs}


def clean_explain_doc(doc: Any) -> Any:
    import json
    from bson import json_util
    return json.loads(json_util.dumps(doc))


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        explain_doc = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_explain_doc(explain_doc)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        pipeline = get_employee_monthly_pipeline(emp_code, month)
        explain_doc = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_explain_doc(explain_doc)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        pipeline = get_department_summary_pipeline(month, department)
        explain_doc = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_explain_doc(explain_doc)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        pipeline = get_late_leaderboard_pipeline(month, limit, department)
        try:
            explain_doc = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        except PyMongoError:
            # Safe explain fallback for Atlas M0
            fallback_pipe = [
                {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}
            ]
            explain_doc = db.command("explain", {"aggregate": "attendance_logs", "pipeline": fallback_pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_explain_doc(explain_doc)}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        if to < from_:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        pipeline = get_department_trend_pipeline(department, from_, to)
        try:
            explain_doc = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        except PyMongoError:
            # Safe explain fallback for Atlas M0
            fallback_pipe = [
                {"$match": {"department": department}},
                {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}
            ]
            explain_doc = db.command("explain", {"aggregate": "employees", "pipeline": fallback_pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_explain_doc(explain_doc)}

    raise HTTPException(status_code=404, detail="Endpoint not found")
