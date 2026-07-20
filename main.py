# main.py

import os
import random
import re
import json
import csv
import io
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Dict, Any, List, Optional

from fastapi import FastAPI, Request, Depends, HTTPException, Body, Query
from fastapi.datastructures import FormData
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
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
MEAL_TYPE_SORT_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2}


def get_db():
    """Yield DB session for request context"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sorted_meals(meals: list[Meal]) -> list[Meal]:
    return sorted(
        meals,
        key=lambda meal: (MEAL_TYPE_SORT_ORDER.get(meal.type.value, 99), meal.id),
    )


def _fetch_meal_days_for_export(db: Session) -> list[MealDay]:
    return (
        db.query(MealDay)
        .options(joinedload(MealDay.meals))
        .order_by(MealDay.date.asc())
        .all()
    )


def _serialize_meal(meal: Meal) -> dict[str, Any]:
    return {
        "id": meal.id,
        "meal_day_id": meal.meal_day_id,
        "type": meal.type.value,
        "description": meal.description,
        "cooking_user": meal.cooking_user,
        "is_favorite": bool(meal.is_favorite),
        "is_takeout": bool(meal.is_takeout),
    }


def _serialize_meal_day(meal_day: MealDay) -> dict[str, Any]:
    return {
        "id": meal_day.id,
        "date": meal_day.date.isoformat(),
        "is_starred": bool(meal_day.is_starred),
        "is_sammy_working": bool(meal_day.is_sammy_working),
        "meals": [_serialize_meal(meal) for meal in _sorted_meals(meal_day.meals)],
    }


def _build_export_summary(meal_days: list[MealDay]) -> dict[str, Any]:
    meals = [meal for meal_day in meal_days for meal in meal_day.meals]
    return {
        "meal_day_count": len(meal_days),
        "meal_count": len(meals),
        "favorite_count": sum(1 for meal in meals if meal.is_favorite),
        "takeout_count": sum(1 for meal in meals if meal.is_takeout),
        "date_min": meal_days[0].date.isoformat() if meal_days else None,
        "date_max": meal_days[-1].date.isoformat() if meal_days else None,
    }


# --------- HTML VIEWS --------------------------
@app.get("/", response_class=HTMLResponse)
def read_index(request: Request):
    """
    Homepage HTML — displays next N days of meals.
    """
    db = next(get_db())
    today = date.today()
    days = []

    # Ensure we have MealDay and Meal entries for the next N days
    for i in range(DAYS):
        current_date = today + timedelta(days=i)
        meal_day = (
            db.query(MealDay)
            .options(joinedload(MealDay.meals))
            .filter(MealDay.date == current_date)
            .first()
        )

        # If not found, create meal day with meal rows with null descriptions
        if not meal_day:
            meal_day = MealDay(date=current_date)
            meal_day.meals = [
                Meal(type=MealType.breakfast),
                Meal(type=MealType.lunch),
                Meal(type=MealType.dinner),
            ]
            db.add(meal_day)
            db.commit()
            db.refresh(meal_day)

        days.append(meal_day)

    # Define template configuration: show_days_until_payday, show_meal_metrics
    template_config = {
        "title": "Home",
        "show_days_until_payday": True,
        "show_meal_metrics": True,
        "days_are_stale": False,
        "show_quick_tray": True,
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

    # Define template configuration
    template_config = {
        "title": "Past Meals",
        "show_days_until_payday": False,
        "show_meal_metrics": False,
        "days_are_stale": True,
        "show_quick_tray": True,
    }

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "days": days, "template_config": template_config},
    )


# --------- API VIEWS --------------------------
def _update_days_from_payload(days: list[dict], db):
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
            desc = day.get(meal_type, "")
            if isinstance(desc, str) and desc.strip().lower() not in ("none", ""):
                meal.description = desc.strip()
            else:
                meal.description = None

            # Get nested fields from "meals" block in payload
            meal_fields = day.get("meals", {}).get(meal_type, {})

            # Update is_takeout if present
            if meal.is_takeout is not None or "is_takeout" in meal_fields:
                # Print the current value and new updated value
                print(
                    f"Current {meal_type} for day {meal_day.date}: is_takeout={meal.is_takeout} -> New: {meal_fields.get('is_takeout', 'off')}"
                )
                # Make change
                meal.is_takeout = is_truthy(meal_fields.get("is_takeout", "off"))

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
                meal.is_favorite = is_truthy(meal_fields.get("is_favorite", "off"))
            else:
                print(
                    f"SKIPPING is_favorite update for {meal_type} on day {meal_day.date} as it's not in payload"
                )


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("on", "true", "1")
    return False


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
def get_favorites(limit: int = 200):
    db = SessionLocal()
    safe_limit = max(1, min(limit, 500))
    try:
        favorites = (
            db.query(Meal.description)
            .filter(Meal.is_favorite == True)
            .filter(Meal.description.isnot(None))
            .filter(Meal.description != "")
            .distinct()
            .order_by(Meal.description.asc())
            .limit(safe_limit)
            .all()
        )
        return [{"meal_text": m[0]} for m in favorites if m[0]]
    finally:
        db.close()


@app.get("/api/veggies", response_class=JSONResponse)
def get_veggies_eaten():
    today = datetime.today().date()

    veggies = None
    with open("./static/veggies.json", "r") as f:
        veggies = json.load(f)

    db = SessionLocal()
    # This month
    first_of_month = today.replace(day=1)
    meals_this_month = (
        db.query(Meal.description)
        .join(MealDay)
        .filter(MealDay.date >= first_of_month)
        .all()
    )
    meal_texts_this_month = [m[0].lower() for m in meals_this_month if m[0]]
    veggie_count_this_month = sum(
        1 for text in meal_texts_this_month if any(veggie in text for veggie in veggies)
    )

    # Last month
    if first_of_month.month == 1:
        last_month = first_of_month.replace(year=first_of_month.year - 1, month=12)
    else:
        last_month = first_of_month.replace(month=first_of_month.month - 1)
    first_of_last_month = last_month
    # Get last day of last month
    if first_of_month.month == 1:
        last_day_of_last_month = first_of_month - timedelta(days=1)
    else:
        last_day_of_last_month = first_of_month - timedelta(days=1)
    meals_last_month = (
        db.query(Meal.description)
        .join(MealDay)
        .filter(MealDay.date >= first_of_last_month)
        .filter(MealDay.date <= last_day_of_last_month)
        .all()
    )
    db.close()
    meal_texts_last_month = [m[0].lower() for m in meals_last_month if m[0]]
    veggie_count_last_month = sum(
        1 for text in meal_texts_last_month if any(veggie in text for veggie in veggies)
    )

    return {
        "veggies_eaten_this_month": veggie_count_this_month,
        "veggies_eaten_last_month": veggie_count_last_month,
    }


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


@app.get("/api/search", response_class=JSONResponse)
def get_search_meal(
    query: str = "",
    favorites_only: Optional[bool] = False,
    only_favorites: Optional[bool] = Query(default=None),
    include_takeout: Optional[bool] = False,
    limit: int = 60,
):
    term = (query or "").strip()
    if not term:
        return {"results": []}

    db = SessionLocal()
    safe_limit = max(1, min(limit, 200))
    use_favorites_filter = is_truthy(favorites_only) or is_truthy(only_favorites)

    try:
        query_obj = (
            db.query(Meal.description)
            .filter(Meal.description.isnot(None))
            .filter(Meal.description != "")
            .filter(Meal.description.ilike(f"%{term}%"))
        )
        if use_favorites_filter:
            query_obj = query_obj.filter(Meal.is_favorite == True)
        if not is_truthy(include_takeout):
            query_obj = query_obj.filter(Meal.is_takeout == False)

        rows = query_obj.order_by(Meal.id.desc()).limit(safe_limit).all()

        seen = set()
        deduped = []
        for (text,) in rows:
            normalized = text.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(text.strip())

        return {"results": deduped}
    finally:
        db.close()


@app.get("/search", response_class=HTMLResponse)
def get_search(request: Request):

    # Define template configuration
    template_config = {
        "title": "Search",
        "show_days_until_payday": False,
        "show_meal_metrics": False,
        "days_are_stale": False,
        "show_quick_tray": False,
    }

    return templates.TemplateResponse(
        "search.html",
        {"request": request, "template_config": template_config},
    )


@app.get("/export", response_class=HTMLResponse)
def get_export_page(request: Request):
    db = SessionLocal()
    try:
        meal_days = _fetch_meal_days_for_export(db)
        export_summary = _build_export_summary(meal_days)
    finally:
        db.close()

    template_config = {
        "title": "Export",
        "show_days_until_payday": False,
        "show_meal_metrics": False,
        "days_are_stale": False,
        "show_quick_tray": False,
    }

    return templates.TemplateResponse(
        "export.html",
        {
            "request": request,
            "template_config": template_config,
            "export_summary": export_summary,
        },
    )


@app.get("/api/export/meals.json")
def export_meals_json():
    db = SessionLocal()
    try:
        meal_days = _fetch_meal_days_for_export(db)
        payload = {
            "generated_at": datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "meal_day_count": len(meal_days),
            "meal_count": sum(len(meal_day.meals) for meal_day in meal_days),
            "meal_days": [_serialize_meal_day(meal_day) for meal_day in meal_days],
        }
    finally:
        db.close()

    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="meal-planner-export.json"'
        },
    )


@app.get("/api/export/meals.csv")
def export_meals_csv():
    db = SessionLocal()
    try:
        meal_days = _fetch_meal_days_for_export(db)
    finally:
        db.close()

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "meal_day_id",
            "date",
            "is_starred",
            "is_sammy_working",
            "meal_id",
            "meal_type",
            "description",
            "cooking_user",
            "is_favorite",
            "is_takeout",
        ]
    )

    for meal_day in meal_days:
        for meal in _sorted_meals(meal_day.meals):
            writer.writerow(
                [
                    meal_day.id,
                    meal_day.date.isoformat(),
                    bool(meal_day.is_starred),
                    bool(meal_day.is_sammy_working),
                    meal.id,
                    meal.type.value,
                    meal.description or "",
                    meal.cooking_user or "",
                    bool(meal.is_favorite),
                    bool(meal.is_takeout),
                ]
            )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="meal-planner-export.csv"'
        },
    )


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
