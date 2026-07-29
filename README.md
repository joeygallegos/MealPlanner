# Meal Planner

Meal Planner is a small FastAPI app for planning meals, saving favorites, searching past meals, importing ChatGPT-generated dinner plans, and exporting meal data.

## Local Setup

Create a `.env` file with the database and service settings:

```text
DB_USER=
DB_PASS=
DB_HOST=
DB_PORT=
DB_NAME=

SERVICE_HOST=127.0.0.1
SERVICE_PORT=9001
```

Install the pinned dependencies:

```powershell
pip install -r requirements.txt
```

Run the app:

```powershell
python main.py
```

The app uses `SERVICE_HOST` and `SERVICE_PORT` when started through `python main.py`.

## Tests

Run the test suite with:

```powershell
python -m pytest
```

Tests use FastAPI `TestClient` and an in-memory SQLite database, so they do not need the configured MySQL database.

## Project Structure

- `main.py`: FastAPI app, routes, request validation, import/export helpers, and logging setup.
- `models.py`: SQLAlchemy engine, session factory, ORM models, and `init_db`.
- `templates/`: Jinja templates for the app pages.
- `templates/partials/`: Shared template pieces such as the header and head assets.
- `static/`: Static assets including shared CSS, veggies data, and the dinner import JSON schema.
- `scripts/`: Standalone utility scripts.
- `alembic/`: Database migration setup and revisions.
- `exports/`: Generated meal data dumps.
- `logs/`: Runtime app logs.
- `tests/`: Backend and route smoke tests.

## DB Sessions

Routes use the FastAPI `get_db` dependency so request database sessions are opened and closed consistently. Standalone scripts use `SessionLocal()` directly because they run outside FastAPI request handling.

The app still calls `init_db()` on startup to create missing tables from the current SQLAlchemy models.

## Logging

Application logs are written to:

```text
logs/mealplanner.log
```

Log files are runtime artifacts and are ignored by git. The `logs/.gitkeep` file keeps the directory present in fresh checkouts.

## DB Schema Migrations

Create a new migration with:

```powershell
alembic revision -m "describe change"
```

Apply migrations with the normal Alembic workflow for this project environment. Do not change the schema without adding a migration.

## Data Export

Use the Export page or direct API routes:

- `/export`
- `/api/export/meals.json`
- `/api/export/meals.csv`

You can also run:

```powershell
python scripts/dump_meals_json.py
```

That writes a timestamped full meal data dump to `exports/`.

## UI Behavior Notes

- Meal cards autosave as you type and when meal/day flags change.
- Each breakfast, lunch, and dinner row has an Actions menu to keep the card header uncluttered.
- Takeout, Favorite, and Leftover live in the Actions menu, but active states still show as small badges next to the meal label.
- Leftovers are hidden from meal search by default. Use Include Leftovers on Search when you intentionally want to find them.
- Search deduplicates repeated meal text, so the same meal planned on multiple days appears once in results.
- Swap exchanges a meal with another day and keeps the meal text, takeout flag, favorite flag, and leftover flag together.
- Queue stores meal text for later so it can be dragged or tapped into an empty slot from the Quick Tray.
- Import lets you paste a ChatGPT-generated dinner plan JSON, preview date conflicts, and load several dinners by date.

## Browser Local Storage

The UI stores quick-tray data in browser local storage:

- `mealplanner.savedMeals.v1`: meals copied from Search for reuse on the home screen.
- `mealplanner.queue.v1`: meals queued from the home screen for later placement.

Clearing browser site data removes these local-only lists.

## ChatGPT Dinner Import Workflow

1. Open `/import`.
2. Copy the prompt from the left side of the page.
3. Paste it into ChatGPT and replace the preferences section with the dates, budget, ingredients, dietary limits, leftovers, or takeout plans you want.
4. Copy ChatGPT's JSON-only response.
5. Paste it into the import box, click Preview, review creates/updates/conflicts, then click Import.

The import schema is available at `/static/chatgpt-dinner-plan.schema.json`. The current schema version is `mealplanner.chatgpt_dinner_plan.v1`.

The importer uses `date`, not database IDs. It creates missing days as needed, preserves breakfast and lunch, and updates only dinner fields plus any day flags included in the JSON. Missing dinner booleans default to `false`.

Prompt to use:

```text
Generate a dinner plan as JSON only. Do not include markdown, comments, or explanation.

Conform exactly to this schema:
- schema_version must be "mealplanner.chatgpt_dinner_plan.v1"
- meal_days must contain 3 to 7 days unless I ask for a different number
- date must use YYYY-MM-DD
- each day must include dinner.description
- dinner.cooking_user may be "Joey", "Sam", or null
- dinner.is_favorite, dinner.is_takeout, and dinner.is_leftover must be booleans

Use this JSON shape:
{
  "schema_version": "mealplanner.chatgpt_dinner_plan.v1",
  "meal_days": [
    {
      "date": "2026-07-21",
      "is_starred": false,
      "is_sammy_working": false,
      "dinner": {
        "description": "Chicken fajita bowls with peppers, onions, rice, salsa, and avocado",
        "cooking_user": "Joey",
        "is_favorite": false,
        "is_takeout": false,
        "is_leftover": false
      }
    }
  ]
}

My preferences:
[Paste dinner preferences, dates, budget, dietary limits, leftovers, ingredients to use, or restaurants here.]
```

## Runtime Artifacts

Generated exports, Python cache files, and log files are runtime artifacts. They should not be committed.

## Todo

- Take local storage data and be able to drag it into this week's meal plan.
