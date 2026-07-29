import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import joinedload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from models import Meal, MealDay, SessionLocal


MEAL_TYPE_SORT_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2}
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "mealplanner.log"
logger = logging.getLogger("mealplanner")
logger.setLevel(logging.INFO)
if not any(
    isinstance(handler, logging.FileHandler)
    and Path(handler.baseFilename) == LOG_PATH.resolve()
    for handler in logger.handlers
):
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(file_handler)


def sorted_meals(meals: list[Meal]) -> list[Meal]:
    return sorted(
        meals,
        key=lambda meal: (MEAL_TYPE_SORT_ORDER.get(meal.type.value, 99), meal.id),
    )


def serialize_meal(meal: Meal) -> dict[str, Any]:
    return {
        "id": meal.id,
        "meal_day_id": meal.meal_day_id,
        "type": meal.type.value,
        "description": meal.description,
        "cooking_user": meal.cooking_user,
        "is_favorite": bool(meal.is_favorite),
        "is_takeout": bool(meal.is_takeout),
        "is_leftover": bool(meal.is_leftover),
    }


def serialize_meal_day(meal_day: MealDay) -> dict[str, Any]:
    return {
        "id": meal_day.id,
        "date": meal_day.date.isoformat(),
        "is_starred": bool(meal_day.is_starred),
        "is_sammy_working": bool(meal_day.is_sammy_working),
        "meals": [serialize_meal(meal) for meal in sorted_meals(meal_day.meals)],
    }


def build_payload(meal_days: list[MealDay]) -> dict[str, Any]:
    meal_count = sum(len(meal_day.meals) for meal_day in meal_days)
    return {
        "generated_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "meal_day_count": len(meal_days),
        "meal_count": meal_count,
        "meal_days": [serialize_meal_day(meal_day) for meal_day in meal_days],
    }


def main() -> None:
    output_dir = PROJECT_ROOT / "exports"
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"meal-data-dump-{timestamp}.json"

    db = SessionLocal()
    try:
        meal_days = (
            db.query(MealDay)
            .options(joinedload(MealDay.meals))
            .order_by(MealDay.date.asc())
            .all()
        )
        payload = build_payload(meal_days)
    finally:
        db.close()

    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote meal data dump to %s", output_path)
    logger.info("meal_day_count=%s", payload["meal_day_count"])
    logger.info("meal_count=%s", payload["meal_count"])


if __name__ == "__main__":
    main()
