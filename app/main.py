"""
Employee Attendance & Analytics API - COMPLETE
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup & Indexes (Rule: create all indexes at startup idempotently)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def configure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ts: Optional[int]) -> Optional[datetime]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


def truncate_to_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def parse_ist_time(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Attendance date is IST calendar date. For overnight shifts (shift_end <= shift_start),
    if punch-in IST time is earlier than shift_end, it belongs to the previous day."""
    dt_ist = punch_in_utc.astimezone(IST)
    if shift_end <= shift_start:
        sh, sm = parse_ist_time(shift_end)
        shift_end_dt = dt_ist.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if dt_ist < shift_end_dt:
            dt_ist = dt_ist - timedelta(days=1)
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: Grace period 10 mins. Late only if strictly > 10:00 after shift_start."""
    sh, sm = parse_ist_time(shift_start)
    d_date = datetime.fromisoformat(attendance_date)
    shift_start_dt = datetime(d_date.year, d_date.month, d_date.day, sh, sm, tzinfo=IST)
    
    diff_minutes = int((punch_in_utc.astimezone(IST) - shift_start_dt).total_seconds() / 60)
    if diff_minutes > 10:
        return diff_minutes
    return 0


def compute_overtime(punch_out_utc: datetime, shift_end_str: str, attendance_date: str, is_overnight: bool) -> int:
    """R3: Whole minutes (floor) from shift_end to punch_out, >= 30 else 0."""
    eh, em = parse_ist_time(shift_end_str)
    d_date = datetime.fromisoformat(attendance_date)
    shift_end_dt = datetime(d_date.year, d_date.month, d_date.day, eh, em, tzinfo=IST)
    if is_overnight:
        shift_end_dt += timedelta(days=1)
        
    diff_minutes = int((punch_out_utc.astimezone(IST) - shift_end_dt).total_seconds() / 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up(val: float, decimals: int = 2) -> float:
    d_val = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d_val.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start cannot equal shift_end")
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except Exception:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for it in items:
        if isinstance(it.get("created_at"), datetime):
            it["created_at"] = dt_to_epoch_ms(it["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Punch-In & Punch-Out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts_utc = truncate_to_second(epoch_ms_to_dt(body.punched_at) if body.punched_at else datetime.now(timezone.utc))
    d_str = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": d_str,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_utc, emp["shift_start"], d_str),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except Exception:
        raise HTTPException(409, "Already punched in for this attendance date")
        
    return format_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts_utc = truncate_to_second(epoch_ms_to_dt(body.punched_at) if body.punched_at else datetime.now(timezone.utc))
    
    # Find most recent open record whose punch_in <= punched_at (working across midnight)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "punch_in": {"$lte": ts_utc}
        },
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(404, "No active punch-in found for punch-out")
        
    if ts_utc <= record["punch_in"] or (ts_utc - record["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "Invalid punch_out timestamp range")
        
    wh = compute_work_hours(record["punch_in"], ts_utc)
    is_overnight = emp["shift_end"] <= emp["shift_start"]
    ot = compute_overtime(ts_utc, emp["shift_end"], record["date"], is_overnight)
    half = wh < 4.50
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": half
            }
        },
        return_document=True
    )
    if not res:
        raise HTTPException(409, "Record already punched out")
        
    return format_attendance_record(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size))
    
    return {
        "items": [format_attendance_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Endpoint: PATCH Regularize Attendance
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for this date")
        
    # Determine new values
    new_status = body.status if body.status is not None else record["status"]
    
    # Validation rules on status vs punch times
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT or LEAVE cannot have punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if body.punch_in is not None:
            new_punch_in = truncate_to_second(epoch_ms_to_dt(body.punch_in))
            # Verify punch_in stays on record's date
            d_str = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
            if d_str != date:
                raise HTTPException(422, "punch_in must remain on the record's attendance date")
        else:
            new_punch_in = record["punch_in"]
            
        if body.punch_out is not None:
            new_punch_out = truncate_to_second(epoch_ms_to_dt(body.punch_out))
        else:
            new_punch_out = record["punch_out"]
            
        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")
            
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in or (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "Invalid punch_out range")

    # Compute derived fields
    if new_status in ["ABSENT", "LEAVE"] or new_punch_in is None:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_half = False
    else:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_wh = compute_work_hours(new_punch_in, new_punch_out)
            is_overnight = emp["shift_end"] <= emp["shift_start"]
            new_ot = compute_overtime(new_punch_out, emp["shift_end"], date, is_overnight)
            new_half = new_wh < 4.50
        else:
            new_wh = None
            new_ot = 0
            new_half = False

    # Check if anything changed
    changes = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if new_punch_in != record.get("punch_in"):
        changes["punch_in"] = {"from": dt_to_epoch_ms(record.get("punch_in")), "to": dt_to_epoch_ms(new_punch_in)}
    if new_punch_out != record.get("punch_out"):
        changes["punch_out"] = {"from": dt_to_epoch_ms(record.get("punch_out")), "to": dt_to_epoch_ms(new_punch_out)}
    if new_wh != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_wh}
    if new_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
    if new_ot != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
    if new_half != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half}
        
    if not changes:
        raise HTTPException(422, "Request changes nothing")
        
    history_entry = {
        "at": truncate_to_second(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_half
            },
            "$push": {"history": history_entry}
        },
        return_document=True
    )
    return format_attendance_record(updated)


def format_attendance_record(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_epoch_ms(doc.get("punch_out"))
    if "history" in doc:
        for h in doc["history"]:
            h["at"] = dt_to_epoch_ms(h.get("at"))
            if "changes" in h:
                for field, val in h["changes"].items():
                    if field in ["punch_in", "punch_out"]:
                        if isinstance(val.get("from"), datetime):
                            val["from"] = dt_to_epoch_ms(val["from"])
                        if isinstance(val.get("to"), datetime):
                            val["to"] = dt_to_epoch_ms(val["to"])
    return doc


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    y, m = map(int, month.split("-"))
    # Calculate working days (Mon-Fri) in month from joined_on
    start_date = max(datetime(y, m, 1).date(), datetime.fromisoformat(emp["joined_on"]).date())
    if m == 12:
        end_date = datetime(y + 1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(y, m + 1, 1).date() - timedelta(days=1)
        
    working_days = 0
    curr = start_date
    while curr <= end_date:
        if curr.weekday() < 5: # Mon-Fri
            working_days += 1
        curr += timedelta(days=1)
        
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    data = res[0] if res else {"present_days": 0.0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    
    present_days = data["present_days"]
    attendance_pct = round_half_up((present_days / working_days) * 100, 2) if working_days > 0 else None
    
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    y, m = map(int, month.split("-"))
    if m == 12:
        last_day = datetime(y + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(y, m + 1, 1) - timedelta(days=1)
    last_day_str = last_day.date().isoformat()
    
    emp_match = {"joined_on": {"$lte": last_day_str}}
    if department:
        emp_match["department"] = department
        
    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$regex": f"^{month}"}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]},
                            "$logs.work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$total_work_hours", "$work_hours_count"]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "_id": 0
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]
    
    items = list(db.employees.aggregate(pipeline))
    for it in items:
        if it["avg_work_hours"] is not None:
            it["avg_work_hours"] = round_half_up(it["avg_work_hours"], 2)
            
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    emp_match = {}
    if department:
        emp_match["department"] = department
        
    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$regex": f"^{month}"},
                            "late_minutes": {"$gt": 0}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": "$logs"},
        {
            "$group": {
                "_id": {"emp_code": "$emp_code", "name": "$name", "department": "$department"},
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id.emp_code": ASCENDING}}
    ]
    
    rows = list(db.employees.aggregate(pipeline))
    
    # Compute standard competition ranking (1, 2, 2, 4) in Python
    ranked_items = []
    current_rank = 1
    prev_late = None
    
    for idx, r in enumerate(rows):
        late_mins = r["total_late_minutes"]
        if prev_late is None or late_mins < prev_late:
            current_rank = idx + 1
        prev_late = late_mins
        
        ranked_items.append({
            "rank": current_rank,
            "emp_code": r["_id"]["emp_code"],
            "name": r["_id"]["name"],
            "department": r["_id"]["department"],
            "total_late_minutes": late_mins,
            "late_count": r["late_count"]
        })
        
    # Limit applies after ranking: include everyone whose rank <= limit
    filtered_items = [it for it in ranked_items if it["rank"] <= limit]
    
    return {"month": month, "items": filtered_items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    f_dt = datetime.fromisoformat(from_date)
    t_dt = datetime.fromisoformat(to_date)
    if t_dt < f_dt or (t_dt - f_dt).days > 92:
        raise HTTPException(422, "Invalid date range or range exceeds 92 days")
        
    # Check if department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")
        
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": from_date, "$lte": to_date}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}}
            }
        },
        {"$match": {"_id": {"$ne": None}}},
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_date, to_date]
                }
            }
        },
        {"$sort": {"_id": ASCENDING}}
    ]
    
    agg_res = {r["_id"]: r for r in db.employees.aggregate(pipeline)}
    
    # Generate all dates from from_date to to_date
    items = []
    curr = f_dt
    while curr <= t_dt:
        d_str = curr.date().isoformat()
        is_wk = curr.weekday() < 5
        
        # Headcount: employees joined_on <= d_str
        hc = db.employees.count_documents({"department": department, "joined_on": {"$lte": d_str}})
        
        day_data = agg_res.get(d_str, {"present_count": 0.0, "late_count": 0})
        present_count = day_data.get("present_count", 0.0)
        late_count = day_data.get("late_count", 0)
        
        if not is_wk:
            attendance_rate = None
        else:
            if hc > 0:
                attendance_rate = round_half_up(present_count / hc, 4)
            else:
                attendance_rate = 0.0
                
        items.append({
            "date": d_str,
            "is_working_day": is_wk,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None
        })
        curr += timedelta(days=1)
        
    # Compute 7-day moving average over non-null attendance_rate values within range
    for i in range(len(items)):
        window = [it["attendance_rate"] for it in items[max(0, i - 6):i + 1] if it["attendance_rate"] is not None]
        if window:
            items[i]["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4)
        else:
            items[i]["moving_avg_7d"] = None
            
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = 1,
    page_size: int = 20
):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, "Invalid endpoint name")
        
    if endpoint == "attendance_list":
        coll = db.attendance_logs
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explain_res = coll.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).explain("executionStats")
        
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        coll = db.attendance_logs
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
            {"$group": {"_id": None, "total": {"$sum": "$late_minutes"}}}
        ]
        explain_res = coll.aggregate(pipeline, explain=True)
        
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        coll = db.employees
        pipeline = [
            {"$match": {"department": department} if department else {}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$limit": 10}
        ]
        explain_res = coll.aggregate(pipeline, explain=True)
        
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        coll = db.employees
        pipeline = [
            {"$match": {"department": department} if department else {}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$limit": limit}
        ]
        explain_res = coll.aggregate(pipeline, explain=True)
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to required")
        coll = db.employees
        pipeline = [
            {"$match": {"department": department}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$limit": 10}
        ]
        explain_res = coll.aggregate(pipeline, explain=True)
    else:
        raise HTTPException(422, "Unknown endpoint")
        
    return {
        "endpoint": endpoint,
        "collection": coll.name,
        "explain": explain_res
    }