from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

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

    for path in ["/", "/backwards", "/search", "/import", "/export", "/share/current-window"]:
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
    assert json_body["generated_at"].endswith("Z")
    assert json_body["meal_days"][0]["meals"][2]["description"] == "Export Dinner"
    assert json_body["meal_days"][0]["meals"][2]["is_leftover"] is True

    csv_response = client.get("/api/export/meals.csv")
    assert csv_response.status_code == 200
    assert "meal_day_id,date,is_starred,is_sammy_working" in csv_response.text
    assert "Export Dinner" in csv_response.text


def test_timezone_config_formats_display_time_without_changing_utc_storage(app_context, monkeypatch):
    main, models, SessionLocal, client = app_context
    instant = datetime(2026, 7, 29, 6, 30, tzinfo=UTC)

    monkeypatch.setenv("APP_TIMEZONE", "America/Chicago")

    assert main._format_generated_at(instant) == "07/29/2026 01:30 CDT"
    assert main._format_storage_timestamp(instant) == "2026-07-29T06:30:00Z"


def test_current_window_pdf_uses_wide_page_and_visible_dates(app_context):
    main, models, SessionLocal, client = app_context
    today = date.today()
    db = SessionLocal()
    try:
        seed_meal_day(
            db,
            models,
            today,
            meals={
                "breakfast": {"description": "Share Breakfast"},
                "lunch": {"description": "Share Lunch", "is_takeout": True},
                "dinner": {
                    "description": "Share Dinner",
                    "cooking_user": "Sam",
                    "is_leftover": True,
                    "is_favorite": True,
                },
            },
            is_starred=True,
            is_sammy_working=True,
        )
    finally:
        db.close()

    response = client.get("/api/share/current-window.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert 'inline; filename="meal-plan-' in response.headers["content-disposition"]
    expected_width = (
        main.SHARE_PDF_MARGIN * 2
        + main.SHARE_PDF_DAY_WIDTH * main.DAYS
        + main.SHARE_PDF_DAY_GAP * (main.DAYS - 1)
    )
    assert f"{expected_width:g}".encode("ascii") in response.content
    assert f"{main.SHARE_PDF_HEIGHT:g}".encode("ascii") in response.content
    assert b"Share Dinner" in response.content
    assert f"Meal plan: {today:%A} {today:%m/%d} to {(today + timedelta(days=8)):%A} {(today + timedelta(days=8)):%m/%d}".encode("ascii") in response.content
    assert today.strftime("%m/%d/%Y").encode("ascii") in response.content
    assert today.strftime("%Y-%m-%d") in response.headers["content-disposition"]


def test_share_preview_renders_mm_dd_yyyy_date(seeded_db):
    main, models, SessionLocal, client = seeded_db
    today = date.today()

    response = client.get("/share/current-window")

    assert response.status_code == 200
    assert f"Meal plan: {today:%A} {today:%m/%d} to {(today + timedelta(days=8)):%A} {(today + timedelta(days=8)):%m/%d}" in response.text
    assert f"{today:%m/%d/%Y} to {(today + timedelta(days=8)):%m/%d/%Y}" in response.text
    assert "/api/share/current-window.pdf" in response.text


def test_send_current_window_email_posts_mailgun_payload(app_context, monkeypatch):
    main, models, SessionLocal, client = app_context
    today = date.today()
    db = SessionLocal()
    try:
        seed_meal_day(
            db,
            models,
            today,
            meals={"dinner": {"description": "Email Dinner"}},
        )
    finally:
        db.close()

    monkeypatch.setenv("MAILGUN_API_KEY", "test-key")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setenv("MAILGUN_FROM_EMAIL", "Meal Planner <meals@example.com>")
    monkeypatch.setenv("MAILGUN_TO_EMAIL", "partner@example.com")
    monkeypatch.setenv("MAILGUN_API_BASE_URL", "https://api.mailgun.net")

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(main.requests, "post", fake_post)

    response = client.post(
        "/api/share/current-window/send",
        json={
            "recipient": "different@example.com",
            "note": "Please check Thursday dinner.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "sent"
    assert body["subject"] == f"Meal plan: {today:%A} {today:%m/%d} to {(today + timedelta(days=8)):%A} {(today + timedelta(days=8)):%m/%d}"
    assert captured["url"] == "https://api.mailgun.net/v3/mg.example.com/messages"
    assert captured["auth"] == ("api", "test-key")
    assert captured["data"]["to"] == "different@example.com"
    assert captured["data"]["subject"] == body["subject"]
    assert "Please check Thursday dinner." in captured["data"]["text"]
    assert today.strftime("%m/%d/%Y") in captured["data"]["text"]
    filename, pdf_bytes, media_type = captured["files"]["attachment"]
    assert filename == f"meal-plan-{today:%Y-%m-%d}-to-{(today + timedelta(days=8)):%Y-%m-%d}.pdf"
    assert pdf_bytes.startswith(b"%PDF")
    assert media_type == "application/pdf"


def test_send_current_window_email_reports_missing_config(app_context, monkeypatch):
    main, models, SessionLocal, client = app_context
    for name in [
        "MAILGUN_API_KEY",
        "MAILGUN_DOMAIN",
        "MAILGUN_FROM_EMAIL",
    ]:
        monkeypatch.delenv(name, raising=False)

    response = client.post(
        "/api/share/current-window/send",
        json={"recipient": "partner@example.com", "note": ""},
    )

    assert response.status_code == 500
    assert "Mailgun is not configured" in response.json()["message"]


def test_send_current_window_email_sanitizes_mailgun_failure(app_context, monkeypatch):
    main, models, SessionLocal, client = app_context
    monkeypatch.setenv("MAILGUN_API_KEY", "test-key")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setenv("MAILGUN_FROM_EMAIL", "Meal Planner <meals@example.com>")
    monkeypatch.setenv("MAILGUN_TO_EMAIL", "partner@example.com")

    def fake_post(url, **kwargs):
        return SimpleNamespace(status_code=400, text="secret provider details")

    monkeypatch.setattr(main.requests, "post", fake_post)

    response = client.post("/api/share/current-window/send", json={"note": ""})

    assert response.status_code == 502
    assert "Mailgun rejected" in response.json()["message"]
    assert "secret provider details" not in response.text


def test_send_current_window_email_rejects_multiple_recipients(app_context, monkeypatch):
    main, models, SessionLocal, client = app_context
    monkeypatch.setenv("MAILGUN_API_KEY", "test-key")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setenv("MAILGUN_FROM_EMAIL", "Meal Planner <meals@example.com>")

    response = client.post(
        "/api/share/current-window/send",
        json={"recipient": "one@example.com,two@example.com", "note": ""},
    )

    assert response.status_code == 422
    assert "Only one recipient" in response.json()["message"]


def test_send_current_window_email_rejects_invalid_recipient_format(app_context, monkeypatch):
    main, models, SessionLocal, client = app_context
    monkeypatch.setenv("MAILGUN_API_KEY", "test-key")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setenv("MAILGUN_FROM_EMAIL", "Meal Planner <meals@example.com>")

    response = client.post(
        "/api/share/current-window/send",
        json={"recipient": "not-an-email", "note": ""},
    )

    assert response.status_code == 422
    assert "valid email" in response.json()["message"]


def test_send_current_window_email_rate_limits_after_five_sends(app_context, monkeypatch):
    main, models, SessionLocal, client = app_context
    monkeypatch.setenv("MAILGUN_API_KEY", "test-key")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setenv("MAILGUN_FROM_EMAIL", "Meal Planner <meals@example.com>")
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda url, **kwargs: SimpleNamespace(status_code=200),
    )

    payload = {"recipient": "partner@example.com", "note": ""}
    responses = [client.post("/api/share/current-window/send", json=payload) for _ in range(6)]

    assert [response.status_code for response in responses[:5]] == [200, 200, 200, 200, 200]
    assert responses[5].status_code == 429
    assert "rate limit" in responses[5].json()["message"]
