# main.py

import os
import random
import re
from datetime import date, datetime, timedelta
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
DAYS_BACKWARDS = 3  # How many days backwards to show on /backwards


def get_db():
    """Yield DB session for request context"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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

    # Define template configuration: show_days_until_payday, show_days_eating_out
    template_config = {
        "title": "Home",
        "show_days_until_payday": True,
        "show_days_eating_out": True,
        "days_are_stale": False,
    }

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "days": days, "template_config": template_config},
    )


@app.get("/backwards", response_class=HTMLResponse)
def backwards_index(request: Request):
    """
    Homepage HTML — displays last N days of meals.
    """
    db = next(get_db())
    today = date.today()
    days = []

    for i in range(1, DAYS_BACKWARDS + 1):
        current_date = today - timedelta(days=i)
        meal_day = (
            db.query(MealDay)
            .options(joinedload(MealDay.meals))
            .filter(MealDay.date == current_date)
            .first()
        )
        days.append(meal_day)

    # Reverse to show oldest first
    days.reverse()

    # Define template configuration: show_days_until_payday, show_days_eating_out
    template_config = {
        "title": "Past Meals",
        "show_days_until_payday": False,
        "show_days_eating_out": False,
        "days_are_stale": True,
    }

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "days": days, "template_config": template_config},
    )


def _assign_nested_key(d, keys, value):
    """Assign value into a nested dictionary given a list of keys."""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _update_days_from_payload(days: list[dict], db):
    # Print payload
    print("Payload days:", days)
    for day in days:
        meal_day = db.query(MealDay).filter(MealDay.id == day["id"]).first()
        if not meal_day:
            continue

        meal_day.is_starred = day.get("is_starred", False)
        meal_day.is_sammy_working = day.get("is_sammy_working", False)

        meals_by_type = {meal.type.value: meal for meal in meal_day.meals}

        for meal_type in ["breakfast", "lunch", "dinner"]:
            meal = meals_by_type.get(meal_type)
            if not meal:
                continue

            # Update description
            meal.description = day.get(meal_type, "")

            # Get nested fields from "meals" block in payload
            meal_fields = day.get("meals", {}).get(meal_type, {})

            # Update is_takeout if present
            if meal.is_takeout is not None or "is_takeout" in meal_fields:
                # Print the current value and new updated value
                print(
                    f"Current {meal_type} for day {meal_day.date}: is_takeout={meal.is_takeout} -> New: {meal_fields.get('is_takeout', 'off')}"
                )
                # Make change
                meal.is_takeout = str(meal_fields.get("is_takeout", "off")).lower() in (
                    "on",
                    "true",
                    "1",
                )

            # Update cooking_user and is_favorite correctly if present
            if meal.cooking_user is not None or "cooking_user" in meal_fields:
                # Print the current value and new updated value
                print(
                    f"Current {meal_type} for day {meal_day.date}: cooking_user={meal.cooking_user} -> New: {meal_fields.get('cooking_user', 'None')}"
                )
                # Make change
                meal.cooking_user = meal_fields.get("cooking_user", None)
            else:
                print(
                    f"SKIPPING cooking_user update for {meal_type} on day {meal_day.date} as it's not in payload"
                )

            if meal.is_favorite is not None or "is_favorite" in meal_fields:
                # Print the current value and new updated value
                print(
                    f"Current {meal_type} for day {meal_day.date}: is_favorite={meal.is_favorite} -> New: {meal_fields.get('is_favorite', 'off')}"
                )
                # Make change
                meal.is_favorite = str(
                    meal_fields.get("is_favorite", "off")
                ).lower() in (
                    "on",
                    "true",
                    "1",
                )
            else:
                print(
                    f"SKIPPING is_favorite update for {meal_type} on day {meal_day.date} as it's not in payload"
                )


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


@app.get("/api/favorites")
def get_favorites():
    db = SessionLocal()
    favorites = (
        db.query(Meal.meal_text)
        .join(MealDay)
        .filter(MealDay.is_favorite == True)
        .distinct()
        .order_by(Meal.meal_text)
        .all()
    )
    db.close()
    return [{"meal_text": m[0]} for m in favorites if m[0]]


@app.get("/api/next-payday", response_class=JSONResponse)
def get_next_payday():
    today = datetime.today().date()

    # Anchor payday: Thursday, Sep 18, 2025
    anchor = datetime(2025, 9, 18).date()
    delta = (today - anchor).days

    # Figure out how many pay periods have passed
    if delta >= 0:
        # Paydays after anchor
        periods_passed = delta // 14
        next_payday = anchor + timedelta(days=(periods_passed + 1) * 14)
    else:
        # Paydays before anchor
        weeks_behind = abs(delta) // 14
        next_payday = anchor - timedelta(days=weeks_behind * 14)
        while next_payday <= today:
            next_payday += timedelta(days=14)

    days_until = (next_payday - today).days

    return {
        "days_until_next_payday": days_until,
        "next_payday_date": next_payday.strftime("%Y-%m-%d"),
    }


@app.get("/api/how-many-times", response_class=JSONResponse)
def get_how_many_times_eat_out():
    db = SessionLocal()
    # Get count of meals where is_takeout is True in the last 7 days
    seven_days_ago = date.today() - timedelta(days=7)
    count = (
        db.query(Meal)
        .join(MealDay, Meal.meal_day_id == MealDay.id)
        .filter(Meal.is_takeout == True)
        .filter(MealDay.date >= seven_days_ago)
        .count()
    )
    db.close()
    return {"count": count}


@app.get("/api/rotation-suggestions")
def rotation_suggestions(meal_type: Optional[str] = None):
    db = SessionLocal()

    # Get recent meals from the last 3 days
    recent_cutoff = date.today() - timedelta(days=3)
    recent_query = (
        db.query(Meal.description).join(MealDay).filter(MealDay.date >= recent_cutoff)
    )
    if meal_type:
        recent_query = recent_query.filter(Meal.type == meal_type)
    recent_meals = recent_query.distinct().all()
    recent_set = {r[0].strip().lower() for r in recent_meals if r[0]}

    # Get favorite meals
    favorite_query = db.query(Meal.description).filter(Meal.is_favorite == True)
    if meal_type:
        favorite_query = favorite_query.filter(Meal.type == meal_type)
    favorite_meals = favorite_query.distinct().all()
    favorite_set = {
        f[0].strip()
        for f in favorite_meals
        if f[0] and f[0].strip().lower() not in recent_set
    }

    db.close()

    if not favorite_set:
        return {"suggestion": None}

    return {"suggestion": random.choice(list(favorite_set))}


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
