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

# Initialize FastAPI app
app = FastAPI()

# Set up database connection and tables
init_db()

# Mount static file handling and Jinja2 templating
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Constant for UI and API logic
DAYS = 9


def get_db():
    """Yield DB session for request context"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------- Shared DB Update Logic for HTML/JSON Inputs --------------------------
def _update_days_from_payload(days_payload: List[Dict[str, Any]], db: Session) -> None:
    """Update MealDay entries with new field names (no legacy fallback)."""
    for day_info in days_payload:
        day_id = int(day_info["id"])

        is_starred = bool(day_info.get("is_starred"))
        is_sammy_working = bool(day_info.get("is_sammy_working"))

        meal_day = db.query(MealDay).filter(MealDay.id == day_id).first()

        if not meal_day:
            meal_day = MealDay(date=day_info.get("date"))
            meal_day.meals = [
                Meal(type=MealType.breakfast, description=""),
                Meal(type=MealType.lunch, description=""),
                Meal(type=MealType.dinner, description=""),
            ]
            db.add(meal_day)
            db.flush()

        # Rename-safe assignment
        setattr(meal_day, "is_starred", is_starred)
        setattr(meal_day, "is_sammy_working", is_sammy_working)

        for meal in meal_day.meals:
            if meal.type == MealType.breakfast:
                meal.description = day_info.get("breakfast", "")
            elif meal.type == MealType.lunch:
                meal.description = day_info.get("lunch", "")
            elif meal.type == MealType.dinner:
                meal.description = day_info.get("dinner", "")


# --------- HTML VIEWS --------------------------
@app.get("/", response_class=HTMLResponse)
def read_index(request: Request):
    """
    Homepage HTML — displays next N days of meals.
    """
    db = next(get_db())
    today = date.today()
    days = []

    for i in range(DAYS):
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
    Classic HTML form POST fallback — parses raw field names like days[1][breakfast].
    """
    form: FormData = await request.form()
    db = next(get_db())

    days_data = {}
    pattern = re.compile(r"days\[(\d+)]\[(\w+)]")

    for key, value in form.items():
        match = pattern.match(key)
        if match:
            idx, field = match.groups()
            days_data.setdefault(idx, {})[field] = value

    json_days = []
    for day_info in days_data.values():
        json_days.append(
            {
                "id": int(day_info["id"]),
                "is_starred": day_info.get("is_starred", "off").lower() == "on",
                "is_sammy_working": day_info.get("is_sammy_working", "off").lower()
                == "on",
                "breakfast": day_info.get("breakfast", ""),
                "lunch": day_info.get("lunch", ""),
                "dinner": day_info.get("dinner", ""),
            }
        )

    _update_days_from_payload(json_days, db)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


# --------- JSON API VIEWS --------------------------


@app.post("/api/save", response_class=JSONResponse)
def api_save(payload: Dict[str, Any] = Body(...)):
    """
    Accepts:
      {"day": {...}}  or  {"days": [{...}, ...]}
    Updates the database and returns a JSON response.
    """
    db = next(get_db())

    if "day" in payload:
        days_payload = [payload["day"]]
    elif "days" in payload:
        days_payload = payload["days"]
    else:
        raise HTTPException(status_code=400, detail="Missing 'day' or 'days' field.")

    for d in days_payload:
        if "id" not in d:
            raise HTTPException(status_code=422, detail="Each day must have an 'id'.")

    _update_days_from_payload(days_payload, db)
    db.commit()
    return {"status": "ok"}


@app.post("/api/copy-week", response_class=JSONResponse)
def api_copy_week(payload: Dict[str, Any] = Body(...)):
    """
    Copy meals from one week to another. JSON input only.
    """
    db = next(get_db())

    try:
        from_date = date.fromisoformat(payload["from_date"])
        to_date = date.fromisoformat(payload["to_date"])
        overwrite = bool(payload.get("overwrite", False))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or missing date fields.")

    conflicting_days = []

    for i in range(DAYS):
        tgt_day = (
            db.query(MealDay)
            .filter(MealDay.date == to_date + timedelta(days=i))
            .first()
        )
        if not tgt_day:
            continue
        if not overwrite and any(m.description.strip() for m in tgt_day.meals):
            conflicting_days.append(str(tgt_day.date))

    if conflicting_days:
        return JSONResponse(
            status_code=409,
            content={
                "message": "Target week has existing meals.",
                "conflicting_days": conflicting_days,
            },
        )

    for i in range(DAYS):
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


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    """
    Ensures all API paths return JSON error shape.
    """
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=exc.status_code, content={"message": exc.detail}
        )
    return JSONResponse(status_code=exc.status_code, content={"message": exc.detail})


# ------------------- Test Utilities ------------------------


# Entry point for local dev
if __name__ == "__main__":
    uvicorn.run(
        app,
        host=str(os.getenv("SERVICE_HOST", "127.0.0.1")),
        port=int(os.getenv("SERVICE_PORT", "80")),
    )
