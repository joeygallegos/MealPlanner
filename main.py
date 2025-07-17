# main.py
import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException
from fastapi.datastructures import FormData
from starlette.responses import RedirectResponse
from typing import Annotated
from sqlalchemy.orm import joinedload
import re
from models import MealDay, Meal, MealType, SessionLocal, init_db
from datetime import date, timedelta, datetime
import uvicorn

app = FastAPI()

# Initialize the database (you might choose to run this separately in production)
init_db()

# Mount static files (for Tailwind CSS, Alpine.js, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Dependency to create a new DB session per request
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/", response_class=HTMLResponse)
def read_index(request: Request):
    db = next(get_db())
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

    # Process each day
    for day_info in days_data.values():
        day_id = int(day_info["id"])
        is_sammy_home = day_info.get("is_sammy_home", "off").lower() == "on"
        is_work_day = day_info.get("is_work_day", "off").lower() == "on"
        
        # Update or create MealDay
        meal_day = db.query(MealDay).filter(MealDay.id == day_id).first()
        if meal_day:
            meal_day.is_sammy_home = is_sammy_home
            meal_day.is_work_day = is_work_day
            for meal in meal_day.meals:
                if meal.type == MealType.breakfast:
                    meal.description = day_info.get("breakfast", "")
                elif meal.type == MealType.lunch:
                    meal.description = day_info.get("lunch", "")
                elif meal.type == MealType.dinner:
                    meal.description = day_info.get("dinner", "")

    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/copy-week")
def copy_meal_week(
    from_date: date,
    to_date: date,
    overwrite: bool = False,
    db: Session = Depends(get_db)
):
    conflicting_days = []

    for i in range(7):  # Iterate through each day of the week
        src_day = db.query(MealDay).filter(MealDay.date == from_date + timedelta(days=i)).first()
        tgt_day = db.query(MealDay).filter(MealDay.date == to_date + timedelta(days=i)).first()

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
                "conflicting_days": conflicting_days
            }
        )

    # Proceed with copy
    for i in range(7):
        src_day = db.query(MealDay).filter(MealDay.date == from_date + timedelta(days=i)).first()
        tgt_day = db.query(MealDay).filter(MealDay.date == to_date + timedelta(days=i)).first()

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

if __name__ == "__main__":
    uvicorn.run(app, host=str(os.getenv("SERVICE_HOST", "80")), port=int(os.getenv("SERVICE_PORT", "80")))
