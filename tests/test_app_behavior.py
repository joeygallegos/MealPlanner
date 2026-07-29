from datetime import date, timedelta

from conftest import seed_meal_day


def test_search_filters_and_deduplicates(app_context):
    main, models, SessionLocal, client = app_context
    db = SessionLocal()
    try:
        seed_meal_day(
            db,
            models,
            date(2026, 1, 1),
            meals={
                "dinner": {
                    "description": "Chicken Tacos",
                    "is_favorite": True,
                }
            },
        )
        seed_meal_day(
            db,
            models,
            date(2026, 1, 2),
            meals={"dinner": {"description": "Chicken Tacos"}},
        )
        seed_meal_day(
            db,
            models,
            date(2026, 1, 3),
            meals={"dinner": {"description": "Chicken Takeout", "is_takeout": True}},
        )
        seed_meal_day(
            db,
            models,
            date(2026, 1, 4),
            meals={"dinner": {"description": "Chicken Leftovers", "is_leftover": True}},
        )
    finally:
        db.close()

    assert client.get("/api/search", params={"query": ""}).json() == {"results": []}

    default_results = client.get("/api/search", params={"query": "Chicken"}).json()["results"]
    assert default_results == ["Chicken Tacos"]

    all_results = client.get(
        "/api/search",
        params={"query": "Chicken", "include_takeout": True, "include_leftovers": True},
    ).json()["results"]
    assert all_results == ["Chicken Leftovers", "Chicken Takeout", "Chicken Tacos"]

    favorite_results = client.get(
        "/api/search",
        params={
            "query": "Chicken",
            "only_favorites": True,
            "include_takeout": True,
            "include_leftovers": True,
        },
    ).json()["results"]
    assert favorite_results == ["Chicken Tacos"]


def test_api_save_preserves_omitted_nested_fields_and_normalizes_text(app_context):
    main, models, SessionLocal, client = app_context
    db = SessionLocal()
    try:
        meal_day = seed_meal_day(
            db,
            models,
            date(2026, 2, 1),
            meals={
                "breakfast": {
                    "description": "Original Breakfast",
                    "cooking_user": "Joey",
                    "is_favorite": True,
                    "is_takeout": True,
                    "is_leftover": True,
                },
                "lunch": {"description": "Original Lunch"},
                "dinner": {"description": "Original Dinner"},
            },
            is_starred=True,
        )
        day_id = meal_day.id
    finally:
        db.close()

    response = client.post(
        "/api/save",
        json={
            "day": {
                "id": day_id,
                "is_starred": False,
                "breakfast": "  Updated Breakfast  ",
                "lunch": "none",
                "dinner": "",
                "meals": {
                    "breakfast": {
                        "is_takeout": False,
                    }
                },
            }
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    db = SessionLocal()
    try:
        saved_day = db.query(models.MealDay).filter(models.MealDay.id == day_id).one()
        meals = {meal.type.value: meal for meal in saved_day.meals}
        assert saved_day.is_starred is False
        assert meals["breakfast"].description == "Updated Breakfast"
        assert meals["breakfast"].is_takeout is False
        assert meals["breakfast"].is_favorite is True
        assert meals["breakfast"].is_leftover is True
        assert meals["breakfast"].cooking_user == "Joey"
        assert meals["lunch"].description is None
        assert meals["dinner"].description is None
    finally:
        db.close()


def test_html_routes_render(seeded_db):
    main, models, SessionLocal, client = seeded_db

    for path in ["/", "/backwards", "/search", "/import", "/export"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "Meal Planner" in response.text


def test_import_dinner_plan_preview_and_apply(app_context):
    main, models, SessionLocal, client = app_context
    db = SessionLocal()
    try:
        seed_meal_day(
            db,
            models,
            date(2026, 3, 1),
            meals={
                "breakfast": {"description": "Keep Breakfast"},
                "lunch": {"description": "Keep Lunch"},
                "dinner": {"description": "Old Dinner"},
            },
        )
    finally:
        db.close()

    plan = {
        "schema_version": main.DINNER_IMPORT_SCHEMA_VERSION,
        "meal_days": [
            {
                "date": "2026-03-01",
                "dinner": {
                    "description": "New Dinner",
                    "cooking_user": "Sam",
                    "is_favorite": True,
                    "is_takeout": False,
                    "is_leftover": False,
                },
            }
        ],
    }

    preview = client.post("/api/import/dinner-plan", json={"dry_run": True, "plan": plan})
    assert preview.status_code == 200
    assert preview.json()["status"] == "preview"
    assert preview.json()["counts"]["update"] == 1
    assert preview.json()["counts"]["conflict"] == 1

    invalid = client.post(
        "/api/import/dinner-plan",
        json={"dry_run": True, "plan": {"schema_version": "bad", "meal_days": []}},
    )
    assert invalid.status_code == 422
    assert "errors" in invalid.json()["message"]

    applied = client.post("/api/import/dinner-plan", json={"dry_run": False, "plan": plan})
    assert applied.status_code == 200
    assert applied.json()["status"] == "imported"

    db = SessionLocal()
    try:
        saved_day = db.query(models.MealDay).filter(models.MealDay.date == date(2026, 3, 1)).one()
        meals = {meal.type.value: meal for meal in saved_day.meals}
        assert meals["breakfast"].description == "Keep Breakfast"
        assert meals["lunch"].description == "Keep Lunch"
        assert meals["dinner"].description == "New Dinner"
        assert meals["dinner"].cooking_user == "Sam"
        assert meals["dinner"].is_favorite is True
    finally:
        db.close()


def test_export_json_and_csv(app_context):
    main, models, SessionLocal, client = app_context
    db = SessionLocal()
    try:
        seed_meal_day(
            db,
            models,
            date.today() + timedelta(days=1),
            meals={"dinner": {"description": "Export Dinner", "is_leftover": True}},
        )
    finally:
        db.close()

    json_response = client.get("/api/export/meals.json")
    assert json_response.status_code == 200
    json_body = json_response.json()
    assert json_body["meal_day_count"] == 1
    assert json_body["meal_count"] == 3
    assert json_body["meal_days"][0]["meals"][2]["description"] == "Export Dinner"
    assert json_body["meal_days"][0]["meals"][2]["is_leftover"] is True

    csv_response = client.get("/api/export/meals.csv")
    assert csv_response.status_code == 200
    assert "meal_day_id,date,is_starred,is_sammy_working" in csv_response.text
    assert "Export Dinner" in csv_response.text
