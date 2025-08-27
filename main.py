# main.py
import os
import re
from datetime import date, timedelta
from typing import Annotated, Dict, Any, List, Optional

from fastapi import FastAPI, Request, Depends, HTTPException, Body
from fastapi.datastructures import FormData
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from models import MealDay, Meal, MealType, SessionLocal, init_db
import uvicorn

app = FastAPI()

# Initialize DB (same behavior)
init_db()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------- Helpers shared by HTML + JSON paths ----------


def _update_days_from_payload(days_payload: List[Dict[str, Any]], db: Session) -> None:
    """
    Updates MealDay rows from a list of day dicts shaped like:
      {"id": 1, "is_sammy_home": true/false, "is_work_day": true/false,
       "breakfast": "...", "lunch": "...", "dinner": "..."}
    Functionality mirrors the existing /save logic.
    """
    for day_info in days_payload:
        day_id = int(day_info["id"])
        is_sammy_home = bool(day_info.get("is_sammy_home", False))
        is_work_day = bool(day_info.get("is_work_day", False))

        meal_day = db.query(MealDay).filter(MealDay.id == day_id).first()
        if not meal_day:
            # If a day was somehow missing, create it (mirrors safety in GET /)
            meal_day = MealDay(date=day_info.get("date"))
            meal_day.meals = [
                Meal(type=MealType.breakfast, description=""),
                Meal(type=MealType.lunch, description=""),
                Meal(type=MealType.dinner, description=""),
            ]
            db.add(meal_day)
            db.flush()  # get an id

        meal_day.is_sammy_home = is_sammy_home
        meal_day.is_work_day = is_work_day

        for meal in meal_day.meals:
            if meal.type == MealType.breakfast:
                meal.description = day_info.get("breakfast", "")
            elif meal.type == MealType.lunch:
                meal.description = day_info.get("lunch", "")
            elif meal.type == MealType.dinner:
                meal.description = day_info.get("dinner", "")


# --------- Existing HTML views (unchanged behavior) ----------


@app.get("/", response_class=HTMLResponse)
def read_index(request: Request):
    db = next(get_db())
    from datetime import date, timedelta

    today = date.today()
    days = []

    for i in range(7):
        current_date = today + timedelta(days=i)
        meal_day = (
            db.query(MealDay)
            .options(joinedload(MealDay.meals))
            .filter(MealDay.date == current_date)
            .first()
        )

        if not meal_day:
            meal_day = MealDay(date=current_date)
            meal_day.meals = [
                Meal(type=MealType.breakfast, description=""),
                Meal(type=MealType.lunch, description=""),
                Meal(type=MealType.dinner, description=""),
            ]
            db.add(meal_day)
            db.commit()
            db.refresh(meal_day)

        days.append(meal_day)

    return templates.TemplateResponse("index.html", {"request": request, "days": days})


@app.post("/save")
async def save_day(request: Request):
    """
    Original form POST. Kept for compatibility.
    """
    form: FormData = await request.form()
    db = next(get_db())

    # Group form fields into days by index
    days_data = {}
    pattern = re.compile(r"days\[(\d+)]\[(\w+)]")

    for key, value in form.items():
        match = pattern.match(key)
        if match:
            idx, field = match.groups()
            days_data.setdefault(idx, {})[field] = value

    # Convert to the JSON-like shape helper expects
    json_days = []
    for day_info in days_data.values():
        json_days.append(
            {
                "id": int(day_info["id"]),
                "is_sammy_home": day_info.get("is_sammy_home", "off").lower() == "on",
                "is_work_day": day_info.get("is_work_day", "off").lower() == "on",
                "breakfast": day_info.get("breakfast", ""),
                "lunch": day_info.get("lunch", ""),
                "dinner": day_info.get("dinner", ""),
            }
        )

    _update_days_from_payload(json_days, db)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/copy-week")
def copy_meal_week(
    from_date: date,
    to_date: date,
    overwrite: bool = False,
    db: Session = Depends(get_db),
):
    conflicting_days = []

    for i in range(7):  # Iterate through each day of the week
        src_day = (
            db.query(MealDay)
            .filter(MealDay.date == from_date + timedelta(days=i))
            .first()
        )
        tgt_day = (
            db.query(MealDay)
            .filter(MealDay.date == to_date + timedelta(days=i))
            .first()
        )

        if not src_day or not tgt_day:
            continue  # Skip if day doesn't exist

        # Check if target has any existing meals
        if not overwrite and any(m.description.strip() for m in tgt_day.meals):
            conflicting_days.append(str(tgt_day.date))

    if conflicting_days and not overwrite:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Target week has existing meals.",
                "conflicting_days": conflicting_days,
            },
        )

    # Proceed with copy
    for i in range(7):
        src_day = (
            db.query(MealDay)
            .filter(MealDay.date == from_date + timedelta(days=i))
            .first()
        )
        tgt_day = (
            db.query(MealDay)
            .filter(MealDay.date == to_date + timedelta(days=i))
            .first()
        )

        if not src_day or not tgt_day:
            continue

        for meal_type in MealType:
            src_meal = next((m for m in src_day.meals if m.type == meal_type), None)
            if not src_meal:
                continue

            tgt_meal = next((m for m in tgt_day.meals if m.type == meal_type), None)
            if not tgt_meal:
                tgt_meal = Meal(type=meal_type, day_id=tgt_day.id)
                db.add(tgt_meal)

            tgt_meal.description = src_meal.description

    db.commit()
    return {"status": "success", "message": "Meal week copied successfully."}


# --------- New JSON APIs for AJAX/auto-save (no behavior change) ----------


@app.post("/api/save", response_class=JSONResponse)
def api_save(payload: Dict[str, Any] = Body(...)):
    """
    Accepts either:
      {"day": {...}}  # single day
    or
      {"days": [{...}, {...}]}
    and updates records. Returns JSON.
    """
    db = next(get_db())

    days_payload: List[Dict[str, Any]] = []
    if "day" in payload and isinstance(payload["day"], dict):
        days_payload = [payload["day"]]
    elif "days" in payload and isinstance(payload["days"], list):
        days_payload = payload["days"]
    else:
        raise HTTPException(status_code=400, detail="Invalid payload shape.")

    # Validate minimal keys
    for d in days_payload:
        if "id" not in d:
            raise HTTPException(status_code=422, detail="Day 'id' is required.")

    _update_days_from_payload(days_payload, db)
    db.commit()
    return {"status": "ok"}


@app.post("/api/copy-week", response_class=JSONResponse)
def api_copy_week(payload: Dict[str, Any] = Body(...)):
    """
    Mirrors /copy-week but with JSON body:
      {"from_date":"YYYY-MM-DD","to_date":"YYYY-MM-DD","overwrite":false}
    """
    db = next(get_db())
    try:
        from_date = date.fromisoformat(payload["from_date"])
        to_date = date.fromisoformat(payload["to_date"])
        overwrite = bool(payload.get("overwrite", False))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or missing dates.")

    # Reuse the same logic
    conflicting_days = []
    for i in range(7):
        src_day = (
            db.query(MealDay)
            .filter(MealDay.date == from_date + timedelta(days=i))
            .first()
        )
        tgt_day = (
            db.query(MealDay)
            .filter(MealDay.date == to_date + timedelta(days=i))
            .first()
        )
        if not src_day or not tgt_day:
            continue
        if not overwrite and any(m.description.strip() for m in tgt_day.meals):
            conflicting_days.append(str(tgt_day.date))

    if conflicting_days and not overwrite:
        return JSONResponse(
            status_code=409,
            content={
                "message": "Target week has existing meals.",
                "conflicting_days": conflicting_days,
            },
        )

    for i in range(7):
        src_day = (
            db.query(MealDay)
            .filter(MealDay.date == from_date + timedelta(days=i))
            .first()
        )
        tgt_day = (
            db.query(MealDay)
            .filter(MealDay.date == to_date + timedelta(days=i))
            .first()
        )
        if not src_day or not tgt_day:
            continue
        for meal_type in MealType:
            src_meal = next((m for m in src_day.meals if m.type == meal_type), None)
            if not src_meal:
                continue
            tgt_meal = next((m for m in tgt_day.meals if m.type == meal_type), None)
            if not tgt_meal:
                tgt_meal = Meal(type=meal_type, day_id=tgt_day.id)
                db.add(tgt_meal)
            tgt_meal.description = src_meal.description

    db.commit()
    return {"status": "success", "message": "Meal week copied successfully."}


# Optional: JSON error handler → consistent error shape for frontend toasts
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    wants_json = request.url.path.startswith("/api/")
    if wants_json:
        return JSONResponse(
            status_code=exc.status_code, content={"message": exc.detail}
        )
    # Fallback to default behavior for HTML routes
    return JSONResponse(status_code=exc.status_code, content={"message": exc.detail})


if __name__ == "__main__":
    # Keeping your env-driven host/port exactly as-is
    uvicorn.run(
        app,
        host=str(os.getenv("SERVICE_HOST", "80")),
        port=int(os.getenv("SERVICE_PORT", "80")),
    )
