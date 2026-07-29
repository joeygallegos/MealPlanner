from datetime import date, timedelta
import importlib
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def app_context(monkeypatch):
    import models

    monkeypatch.setattr(models, "init_db", lambda: None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    models.Base.metadata.create_all(bind=engine)

    main.SessionLocal = TestingSessionLocal

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = override_get_db

    with TestClient(main.app) as client:
        yield main, models, TestingSessionLocal, client

    main.app.dependency_overrides.clear()
    models.Base.metadata.drop_all(bind=engine)


def seed_meal_day(db, models, day_date, meals=None, is_starred=False, is_sammy_working=False):
    meal_day = models.MealDay(
        date=day_date,
        is_starred=is_starred,
        is_sammy_working=is_sammy_working,
    )
    meals = meals or {}
    meal_day.meals = [
        models.Meal(
            type=models.MealType.breakfast,
            description=meals.get("breakfast", {}).get("description"),
            cooking_user=meals.get("breakfast", {}).get("cooking_user"),
            is_favorite=meals.get("breakfast", {}).get("is_favorite", False),
            is_takeout=meals.get("breakfast", {}).get("is_takeout", False),
            is_leftover=meals.get("breakfast", {}).get("is_leftover", False),
        ),
        models.Meal(
            type=models.MealType.lunch,
            description=meals.get("lunch", {}).get("description"),
            cooking_user=meals.get("lunch", {}).get("cooking_user"),
            is_favorite=meals.get("lunch", {}).get("is_favorite", False),
            is_takeout=meals.get("lunch", {}).get("is_takeout", False),
            is_leftover=meals.get("lunch", {}).get("is_leftover", False),
        ),
        models.Meal(
            type=models.MealType.dinner,
            description=meals.get("dinner", {}).get("description"),
            cooking_user=meals.get("dinner", {}).get("cooking_user"),
            is_favorite=meals.get("dinner", {}).get("is_favorite", False),
            is_takeout=meals.get("dinner", {}).get("is_takeout", False),
            is_leftover=meals.get("dinner", {}).get("is_leftover", False),
        ),
    ]
    db.add(meal_day)
    db.commit()
    db.refresh(meal_day)
    return meal_day


@pytest.fixture()
def seeded_db(app_context):
    main, models, SessionLocal, client = app_context
    db = SessionLocal()
    today = date.today()
    try:
        for offset in range(-3, 9):
            seed_meal_day(
                db,
                models,
                today + timedelta(days=offset),
                meals={
                    "breakfast": {"description": f"Breakfast {offset}"},
                    "lunch": {"description": f"Lunch {offset}"},
                    "dinner": {"description": f"Dinner {offset}"},
                },
            )
        yield main, models, SessionLocal, client
    finally:
        db.close()
