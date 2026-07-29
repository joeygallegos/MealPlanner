# main.py

import os
import random
import re
import json
import csv
import html
import io
import logging
import threading
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from time import monotonic
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from fastapi import FastAPI, Request, Depends, HTTPException, Body, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from reportlab.pdfbase.pdfmetrics import stringWidth
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from models import MealDay, Meal, MealType, SessionLocal, init_db
import uvicorn

# Initialize FastAPI app
app = FastAPI()

LOG_DIR = Path("logs")
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

# Set up database connection and tables
init_db()

# Mount static file handling and Jinja2 templating
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Constant for UI and API logic
DAYS = 9
DAYS_BACKWARDS = 3  # How many days backwards to show on /backwards
MEAL_TYPE_SORT_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2}
DINNER_IMPORT_SCHEMA_VERSION = "mealplanner.chatgpt_dinner_plan.v1"
DINNER_IMPORT_MAX_DAYS = 14
DINNER_IMPORT_COOKING_USERS = {"Joey", "Sam"}
APP_TIMEZONE_ENV = "APP_TIMEZONE"
DEFAULT_APP_TIMEZONE = "America/Chicago"
# The share PDF intentionally uses a browser-viewing canvas, not a paper size,
# so the 9-day window can stay in one horizontal row and be zoomed by the recipient.
SHARE_PDF_DAY_WIDTH = 2.45 * inch
SHARE_PDF_DAY_GAP = 0.14 * inch
SHARE_PDF_MARGIN = 0.32 * inch
SHARE_PDF_HEIGHT = 7.7 * inch
MAILGUN_SEND_TIMEOUT_SECONDS = 15
MAILGUN_SEND_RATE_LIMIT_COUNT = 5
MAILGUN_SEND_RATE_LIMIT_SECONDS = 5 * 60
# The send lock prevents duplicate clicks from launching overlapping Mailgun
# requests. The timestamp list is intentionally process-local: enough for this
# small single-service app without introducing persistent rate-limit storage.
_mailgun_send_lock = threading.Lock()
_mailgun_send_timestamps: list[float] = []


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


def _fetch_or_create_current_window(db: Session) -> list[MealDay]:
    """Return the same forward planning window used by Home, creating blank days as needed."""
    today = _today_in_app_timezone()
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
                Meal(type=MealType.breakfast),
                Meal(type=MealType.lunch),
                Meal(type=MealType.dinner),
            ]
            db.add(meal_day)
            db.flush()

        days.append(meal_day)

    db.commit()
    return days


def _format_display_date(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def _format_share_title_date(value: date) -> str:
    return f"{value.strftime('%A')} {value.strftime('%m/%d')}"


def _format_generated_at(value: datetime) -> str:
    """Format UTC instants for people using the configured household timezone."""
    return value.astimezone(_app_timezone()).strftime("%m/%d/%Y %H:%M %Z")


def _format_storage_timestamp(value: datetime) -> str:
    """Keep machine-readable/export timestamps in UTC regardless of display timezone."""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _app_timezone() -> ZoneInfo:
    """Resolve the display timezone once per call so env changes apply after restart/tests."""
    timezone_name = os.getenv(APP_TIMEZONE_ENV, DEFAULT_APP_TIMEZONE).strip()
    try:
        return ZoneInfo(timezone_name or DEFAULT_APP_TIMEZONE)
    except ZoneInfoNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=f"{APP_TIMEZONE_ENV} must be a valid IANA timezone name.",
        )


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _today_in_app_timezone() -> date:
    return _now_utc().astimezone(_app_timezone()).date()


def _share_date_range(meal_days: list[MealDay]) -> dict[str, str]:
    """Build every date label from one source so preview, PDF, email, and filenames match."""
    start = meal_days[0].date
    end = meal_days[-1].date
    return {
        "start_display": _format_display_date(start),
        "end_display": _format_display_date(end),
        "title": f"Meal plan: {_format_share_title_date(start)} to {_format_share_title_date(end)}",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "filename": f"meal-plan-{start.isoformat()}-to-{end.isoformat()}.pdf",
    }


def _meal_by_type(meal_day: MealDay) -> dict[str, Meal]:
    return {meal.type.value: meal for meal in meal_day.meals}


def _meal_badges(meal: Optional[Meal]) -> list[tuple[str, str, str]]:
    """Return text plus colors for meal badges that mirror the Home page states."""
    if not meal:
        return []

    badges = []
    if meal.cooking_user:
        badges.append(("Cook: " + meal.cooking_user, "#dbeafe", "#1e40af"))
    if meal.is_takeout:
        badges.append(("Takeout", "#ffedd5", "#9a3412"))
    if meal.is_leftover:
        badges.append(("Leftover", "#ecfccb", "#3f6212"))
    if meal.is_favorite:
        badges.append(("Favorite", "#ffe4e6", "#9f1239"))
    return badges


def _share_window_context(meal_days: list[MealDay]) -> dict[str, Any]:
    date_range = _share_date_range(meal_days)
    generated_at = _now_utc()

    return {
        **date_range,
        # Display time is local for the human preview; UTC is retained for any
        # machine-facing value that may be reused by exports or future auditing.
        "generated_at": _format_generated_at(generated_at),
        "generated_at_utc": _format_storage_timestamp(generated_at),
        "default_recipient": os.getenv("MAILGUN_TO_EMAIL", "").strip(),
        "days": meal_days,
    }


def _draw_wrapped_text(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    style_name: str,
    max_height: float,
) -> float:
    """Draw paragraph text inside a fixed-height PDF region and report used height."""
    styles = getSampleStyleSheet()
    paragraph = Paragraph(html.escape(text), styles[style_name])
    available_width = max(1, width)
    used_width, used_height = paragraph.wrap(available_width, max_height)
    draw_height = min(used_height, max_height)
    paragraph.drawOn(pdf, x, y - draw_height)
    return draw_height


def _draw_badge(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    fill_color: str = "#e0f2fe",
    text_color: str = "#075985",
) -> float:
    """Draw a compact rounded badge and return its width for inline badge layout."""
    width = stringWidth(text, "Helvetica-Bold", 6.5) + 8
    pdf.setFillColor(colors.HexColor(fill_color))
    pdf.roundRect(x, y - 10, width, 11, 3, fill=1, stroke=0)
    pdf.setFillColor(colors.HexColor(text_color))
    pdf.setFont("Helvetica-Bold", 6.5)
    pdf.drawString(x + 4, y - 7.5, text)
    return width


def _draw_meal_panel(
    pdf: canvas.Canvas,
    meal: Optional[Meal],
    label: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    """Render one Home-style meal box inside a day card."""
    description = meal.description.strip() if meal and meal.description else "Missing"
    is_missing = description == "Missing"

    pdf.setFillColor(colors.HexColor("#f8fafc"))
    pdf.setStrokeColor(colors.HexColor("#cbd5e1"))
    pdf.roundRect(x, y - height, width, height, 4, fill=1, stroke=1)

    pdf.setFillColor(colors.HexColor("#374151"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(x + 7, y - 12, label)

    badges = _meal_badges(meal)
    badge_x = x + 54
    badge_y = y - 3
    for badge_text, fill_color, text_color in badges:
        badge_width = _draw_badge(pdf, badge_text, badge_x, badge_y, fill_color, text_color)
        badge_x += badge_width + 4
        if badge_x > x + width - 45:
            # Meal boxes are intentionally compact; dropping extra badges is better
            # than letting them collide with the meal text in the share PDF.
            break

    if is_missing:
        pdf.setFillColor(colors.HexColor("#b45309"))
        pdf.setFont("Helvetica-Oblique", 8)
        pdf.drawString(x + 7, y - 29, "Missing")
        return

    _draw_wrapped_text(
        pdf,
        description,
        x + 7,
        y - 23,
        width - 14,
        "BodyText",
        height - 30,
    )


def _generate_current_window_pdf(meal_days: list[MealDay]) -> bytes:
    """Generate the wide, single-row PDF that the Share page previews and emails."""
    page_width = (
        SHARE_PDF_MARGIN * 2
        + SHARE_PDF_DAY_WIDTH * DAYS
        + SHARE_PDF_DAY_GAP * (DAYS - 1)
    )
    buffer = io.BytesIO()
    # Keep the content stream uncompressed so lightweight tests can assert visible
    # dates/text and page dimensions without adding another PDF parser dependency.
    pdf = canvas.Canvas(buffer, pagesize=(page_width, SHARE_PDF_HEIGHT), pageCompression=0)

    context = _share_window_context(meal_days)
    pdf.setTitle(context["title"])
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(
        SHARE_PDF_MARGIN,
        SHARE_PDF_HEIGHT - 0.42 * inch,
        context["title"],
    )
    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(colors.HexColor("#475569"))
    pdf.drawString(
        SHARE_PDF_MARGIN,
        SHARE_PDF_HEIGHT - 0.62 * inch,
        f"Generated {context['generated_at']}",
    )

    top = SHARE_PDF_HEIGHT - 0.92 * inch
    card_height = SHARE_PDF_HEIGHT - 1.24 * inch
    meal_labels = [("breakfast", "Breakfast"), ("lunch", "Lunch"), ("dinner", "Dinner")]

    for index, meal_day in enumerate(meal_days):
        x = SHARE_PDF_MARGIN + index * (SHARE_PDF_DAY_WIDTH + SHARE_PDF_DAY_GAP)
        y = top
        pdf.setFillColor(colors.white)
        pdf.setStrokeColor(colors.HexColor("#e2e8f0"))
        pdf.roundRect(x, y - card_height, SHARE_PDF_DAY_WIDTH, card_height, 6, fill=1, stroke=1)

        pdf.setFillColor(colors.HexColor("#0f172a"))
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(x + 10, y - 18, meal_day.date.strftime("%A"))
        pdf.setFont("Helvetica-Bold", 9)
        pdf.setFillColor(colors.HexColor("#64748b"))
        pdf.drawString(x + 10, y - 32, _format_display_date(meal_day.date))

        day_badges = []
        if meal_day.is_starred:
            day_badges.append("Sammy home")
        if meal_day.is_sammy_working:
            day_badges.append("Sammy working")

        badge_x = x + 10
        badge_y = y - 39
        for badge in day_badges:
            # Unlike the Home UI icon buttons, the PDF only shows active day
            # states so the recipient is not asked to decode inactive controls.
            badge_x += _draw_badge(pdf, badge, badge_x, badge_y, "#f5f3ff", "#6d28d9") + 4

        meals_by_type = _meal_by_type(meal_day)
        meal_y = y - 58
        meal_panel_height = 86
        for meal_key, meal_label in meal_labels:
            meal = meals_by_type.get(meal_key)
            _draw_meal_panel(
                pdf,
                meal,
                meal_label,
                x + 10,
                meal_y,
                SHARE_PDF_DAY_WIDTH - 20,
                meal_panel_height,
            )
            meal_y -= meal_panel_height + 12

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _mailgun_config() -> dict[str, str]:
    """Load Mailgun settings without exposing secrets in returned errors or logs."""
    values = {
        "api_key": os.getenv("MAILGUN_API_KEY", "").strip(),
        "domain": os.getenv("MAILGUN_DOMAIN", "").strip(),
        "from_email": os.getenv("MAILGUN_FROM_EMAIL", "").strip(),
        "to_email": os.getenv("MAILGUN_TO_EMAIL", "").strip(),
        "api_base_url": os.getenv("MAILGUN_API_BASE_URL", "https://api.mailgun.net").strip(),
    }
    missing = [
        key
        for key, value in values.items()
        if key not in {"api_base_url", "to_email"} and not value
    ]
    if missing:
        # Keep the error actionable without echoing configured secrets or addresses.
        raise HTTPException(
            status_code=500,
            detail=f"Mailgun is not configured. Missing: {', '.join(missing)}.",
        )
    return values


def _validate_single_email_address(value: Any) -> str:
    """Accept exactly one plain email address for the partner share flow."""
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="recipient must be an email address.")

    recipient = value.strip()
    if not recipient:
        raise HTTPException(status_code=422, detail="recipient is required.")
    if "," in recipient or ";" in recipient or "\n" in recipient or "\r" in recipient:
        raise HTTPException(status_code=422, detail="Only one recipient email is allowed.")

    parsed_name, parsed_email = parseaddr(recipient)
    if parsed_name or parsed_email != recipient or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        raise HTTPException(status_code=422, detail="recipient must be a single valid email address.")
    return recipient


def _check_mailgun_send_rate_limit(now: Optional[float] = None) -> None:
    """Allow a small burst of manual sends while preventing accidental repeat spam."""
    now = monotonic() if now is None else now
    window_start = now - MAILGUN_SEND_RATE_LIMIT_SECONDS
    _mailgun_send_timestamps[:] = [
        timestamp for timestamp in _mailgun_send_timestamps if timestamp > window_start
    ]
    if len(_mailgun_send_timestamps) >= MAILGUN_SEND_RATE_LIMIT_COUNT:
        raise HTTPException(
            status_code=429,
            detail="Share email rate limit reached. Try again in a few minutes.",
        )
    _mailgun_send_timestamps.append(now)


def _send_mailgun_share_email(
    meal_days: list[MealDay],
    note: str,
    recipient: str,
) -> dict[str, Any]:
    """Send the regenerated PDF through Mailgun using the reviewed recipient/note."""
    config = _mailgun_config()
    date_range = _share_date_range(meal_days)
    subject = date_range["title"]
    body_parts = []
    if note:
        body_parts.append(note)
        body_parts.append("")
    body_parts.extend(
        [
            f"Meal plan for {date_range['start_display']} to {date_range['end_display']}.",
            f"Generated {_format_generated_at(_now_utc())}.",
            "PDF attached.",
        ]
    )

    pdf_bytes = _generate_current_window_pdf(meal_days)
    try:
        response = requests.post(
            f"{config['api_base_url'].rstrip('/')}/v3/{config['domain']}/messages",
            auth=("api", config["api_key"]),
            data={
                "from": config["from_email"],
                "to": recipient,
                "subject": subject,
                "text": "\n".join(body_parts),
            },
            files={
                "attachment": (
                    date_range["filename"],
                    pdf_bytes,
                    "application/pdf",
                )
            },
            timeout=MAILGUN_SEND_TIMEOUT_SECONDS,
    )
    except requests.RequestException:
        # Provider/network details can include sensitive request context, so logs and
        # client errors stay intentionally high-level.
        logger.warning("Mailgun share email request failed")
        raise HTTPException(
            status_code=502,
            detail="Mailgun could not be reached. Check network and Mailgun configuration.",
        )

    if response.status_code >= 400:
        logger.warning("Mailgun share email failed with status %s", response.status_code)
        raise HTTPException(
            status_code=502,
            detail="Mailgun rejected the share email. Check configuration and Mailgun logs.",
        )

    logger.info("Meal plan share email accepted by Mailgun")
    return {"status": "sent", "subject": subject, "filename": date_range["filename"]}


def _serialize_meal(meal: Meal) -> dict[str, Any]:
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
        "leftover_count": sum(1 for meal in meals if meal.is_leftover),
        "date_min": meal_days[0].date.isoformat() if meal_days else None,
        "date_max": meal_days[-1].date.isoformat() if meal_days else None,
    }


def _parse_import_date(value: Any, path: str, errors: list[str]) -> Optional[date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        errors.append(f"{path} must be a date string in YYYY-MM-DD format.")
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{path} is not a valid calendar date.")
        return None


def _optional_bool(value: Any, path: str, errors: list[str]) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    errors.append(f"{path} must be true, false, or omitted.")
    return None


def _validate_dinner_import_plan(plan: Any) -> list[dict[str, Any]]:
    errors: list[str] = []

    if not isinstance(plan, dict):
        raise HTTPException(status_code=422, detail="Import payload must be a JSON object.")

    if plan.get("schema_version") != DINNER_IMPORT_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {DINNER_IMPORT_SCHEMA_VERSION!r}."
        )

    meal_days = plan.get("meal_days")
    if not isinstance(meal_days, list):
        errors.append("meal_days must be an array.")
        meal_days = []
    elif len(meal_days) > DINNER_IMPORT_MAX_DAYS:
        errors.append(f"meal_days cannot contain more than {DINNER_IMPORT_MAX_DAYS} days.")
    elif not meal_days:
        errors.append("meal_days must contain at least one day.")

    seen_dates: set[date] = set()
    normalized_days: list[dict[str, Any]] = []

    for index, day_payload in enumerate(meal_days):
        path = f"meal_days[{index}]"
        if not isinstance(day_payload, dict):
            errors.append(f"{path} must be an object.")
            continue

        parsed_date = _parse_import_date(day_payload.get("date"), f"{path}.date", errors)
        if parsed_date:
            if parsed_date in seen_dates:
                errors.append(f"{path}.date duplicates {parsed_date.isoformat()}.")
            seen_dates.add(parsed_date)

        dinner = day_payload.get("dinner")
        if not isinstance(dinner, dict):
            errors.append(f"{path}.dinner must be an object.")
            continue

        description = dinner.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{path}.dinner.description must be a non-empty string.")
            description = ""

        cooking_user = dinner.get("cooking_user")
        if cooking_user == "":
            cooking_user = None
        if cooking_user is not None and cooking_user not in DINNER_IMPORT_COOKING_USERS:
            errors.append(f"{path}.dinner.cooking_user must be Joey, Sam, null, or omitted.")

        is_starred = _optional_bool(day_payload.get("is_starred"), f"{path}.is_starred", errors)
        is_sammy_working = _optional_bool(
            day_payload.get("is_sammy_working"), f"{path}.is_sammy_working", errors
        )
        is_favorite = _optional_bool(
            dinner.get("is_favorite", False), f"{path}.dinner.is_favorite", errors
        )
        is_takeout = _optional_bool(
            dinner.get("is_takeout", False), f"{path}.dinner.is_takeout", errors
        )
        is_leftover = _optional_bool(
            dinner.get("is_leftover", False), f"{path}.dinner.is_leftover", errors
        )

        if parsed_date:
            normalized_days.append(
                {
                    "date": parsed_date,
                    "date_text": parsed_date.isoformat(),
                    "description": description.strip(),
                    "cooking_user": cooking_user,
                    "is_favorite": bool(is_favorite),
                    "is_takeout": bool(is_takeout),
                    "is_leftover": bool(is_leftover),
                    "has_is_starred": "is_starred" in day_payload,
                    "is_starred": bool(is_starred),
                    "has_is_sammy_working": "is_sammy_working" in day_payload,
                    "is_sammy_working": bool(is_sammy_working),
                }
            )

    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    return normalized_days


def _ensure_meal_day_for_import(db: Session, day_date: date) -> tuple[MealDay, bool]:
    meal_day = (
        db.query(MealDay)
        .options(joinedload(MealDay.meals))
        .filter(MealDay.date == day_date)
        .first()
    )
    if meal_day:
        return meal_day, False

    meal_day = MealDay(date=day_date)
    meal_day.meals = [
        Meal(type=MealType.breakfast),
        Meal(type=MealType.lunch),
        Meal(type=MealType.dinner),
    ]
    db.add(meal_day)
    db.flush()
    return meal_day, True


def _ensure_meals_by_type(meal_day: MealDay) -> dict[str, Meal]:
    meals_by_type = {meal.type.value: meal for meal in meal_day.meals}
    for meal_type in MealType:
        if meal_type.value not in meals_by_type:
            meal = Meal(type=meal_type)
            meal_day.meals.append(meal)
            meals_by_type[meal_type.value] = meal
    return meals_by_type


def _preview_dinner_import_days(db: Session, days: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    counts = {
        "create": 0,
        "update": 0,
        "unchanged": 0,
        "conflict": 0,
        "skipped": 0,
        "invalid": 0,
    }

    for day in days:
        meal_day = (
            db.query(MealDay)
            .options(joinedload(MealDay.meals))
            .filter(MealDay.date == day["date"])
            .first()
        )
        dinner = None
        if meal_day:
            dinner = next(
                (meal for meal in meal_day.meals if meal.type == MealType.dinner),
                None,
            )

        existing_description = dinner.description if dinner else None
        incoming_description = day["description"]
        has_conflict = bool(
            existing_description and existing_description.strip() != incoming_description
        )

        if not meal_day:
            action = "create"
        elif (
            existing_description == incoming_description
            and bool(dinner.is_favorite if dinner else False) == day["is_favorite"]
            and bool(dinner.is_takeout if dinner else False) == day["is_takeout"]
            and bool(dinner.is_leftover if dinner else False) == day["is_leftover"]
            and (dinner.cooking_user if dinner else None) == day["cooking_user"]
        ):
            action = "unchanged"
        else:
            action = "update"

        counts[action] += 1
        if has_conflict:
            counts["conflict"] += 1

        rows.append(
            {
                "date": day["date_text"],
                "action": action,
                "has_conflict": has_conflict,
                "existing_description": existing_description,
                "incoming_description": incoming_description,
                "cooking_user": day["cooking_user"],
                "is_favorite": day["is_favorite"],
                "is_takeout": day["is_takeout"],
                "is_leftover": day["is_leftover"],
            }
        )

    return {"counts": counts, "rows": rows}


def _apply_dinner_import_days(db: Session, days: list[dict[str, Any]]) -> dict[str, Any]:
    preview = _preview_dinner_import_days(db, days)

    for day in days:
        meal_day, created_day = _ensure_meal_day_for_import(db, day["date"])
        meals_by_type = _ensure_meals_by_type(meal_day)
        dinner = meals_by_type["dinner"]

        dinner.description = day["description"]
        dinner.cooking_user = day["cooking_user"]
        dinner.is_favorite = day["is_favorite"]
        dinner.is_takeout = day["is_takeout"]
        dinner.is_leftover = day["is_leftover"]

        if created_day or day["has_is_starred"]:
            meal_day.is_starred = day["is_starred"]
        if created_day or day["has_is_sammy_working"]:
            meal_day.is_sammy_working = day["is_sammy_working"]

    db.commit()
    return preview


# --------- HTML VIEWS --------------------------
@app.get("/", response_class=HTMLResponse)
def read_index(request: Request, db: Session = Depends(get_db)):
    """
    Homepage HTML — displays next N days of meals.
    """
    days = _fetch_or_create_current_window(db)

    # Define template configuration: show_days_until_payday, show_meal_metrics
    template_config = {
        "title": "Home",
        "show_days_until_payday": True,
        "show_meal_metrics": True,
        "days_are_stale": False,
        "show_quick_tray": True,
    }

    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request, "days": days, "template_config": template_config},
    )


@app.get("/backwards", response_class=HTMLResponse)
def backwards_index(request: Request, db: Session = Depends(get_db)):
    """
    Homepage HTML — displays last N days of meals.
    """
    today = _today_in_app_timezone()
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
        request,
        "index.html",
        {"request": request, "days": days, "template_config": template_config},
    )


# --------- API VIEWS --------------------------
def _update_days_from_payload(days: list[dict], db: Session):
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

            meal_fields = day.get("meals", {}).get(meal_type, {})

            if "is_takeout" in meal_fields:
                logger.info(
                    "Updating %s for %s: is_takeout=%s -> %s",
                    meal_type,
                    meal_day.date,
                    meal.is_takeout,
                    meal_fields.get("is_takeout"),
                )
                meal.is_takeout = is_truthy(meal_fields.get("is_takeout"))

            if "is_leftover" in meal_fields:
                logger.info(
                    "Updating %s for %s: is_leftover=%s -> %s",
                    meal_type,
                    meal_day.date,
                    meal.is_leftover,
                    meal_fields.get("is_leftover"),
                )
                meal.is_leftover = is_truthy(meal_fields.get("is_leftover"))

            if "cooking_user" in meal_fields:
                logger.info(
                    "Updating %s for %s: cooking_user=%s -> %s",
                    meal_type,
                    meal_day.date,
                    meal.cooking_user,
                    meal_fields.get("cooking_user"),
                )
                meal.cooking_user = meal_fields.get("cooking_user", None)

            if "is_favorite" in meal_fields:
                logger.info(
                    "Updating %s for %s: is_favorite=%s -> %s",
                    meal_type,
                    meal_day.date,
                    meal.is_favorite,
                    meal_fields.get("is_favorite"),
                )
                meal.is_favorite = is_truthy(meal_fields.get("is_favorite"))


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("on", "true", "1")
    return False


@app.post("/api/save", response_class=JSONResponse)
def api_save(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """
    Accepts:
      {"day": {...}}  or  {"days": [{...}, ...]}
    Updates the database and returns a JSON response.
    """
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
def get_favorites(limit: int = 200, db: Session = Depends(get_db)):
    safe_limit = max(1, min(limit, 500))
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


@app.get("/api/veggies", response_class=JSONResponse)
def get_veggies_eaten(db: Session = Depends(get_db)):
    today = _today_in_app_timezone()

    veggies = None
    with open("./static/veggies.json", "r") as f:
        veggies = json.load(f)

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
    today = _today_in_app_timezone()

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
    include_leftovers: Optional[bool] = False,
    limit: int = 60,
    db: Session = Depends(get_db),
):
    term = (query or "").strip()
    if not term:
        return {"results": []}

    safe_limit = max(1, min(limit, 200))
    use_favorites_filter = is_truthy(favorites_only) or is_truthy(only_favorites)

    normalized_description = func.lower(func.trim(Meal.description))
    latest_match_ids = (
        db.query(func.max(Meal.id).label("meal_id"))
        .filter(Meal.description.isnot(None))
        .filter(Meal.description != "")
        .filter(Meal.description.ilike(f"%{term}%"))
    )
    if use_favorites_filter:
        latest_match_ids = latest_match_ids.filter(Meal.is_favorite == True)
    if not is_truthy(include_takeout):
        latest_match_ids = latest_match_ids.filter(Meal.is_takeout == False)
    if not is_truthy(include_leftovers):
        latest_match_ids = latest_match_ids.filter(Meal.is_leftover == False)

    latest_match_ids = (
        latest_match_ids.group_by(normalized_description)
        .order_by(func.max(Meal.id).desc())
        .limit(safe_limit)
        .subquery()
    )

    rows = (
        db.query(Meal.description)
        .join(latest_match_ids, Meal.id == latest_match_ids.c.meal_id)
        .order_by(latest_match_ids.c.meal_id.desc())
        .all()
    )

    return {"results": [text.strip() for (text,) in rows if text and text.strip()]}


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
        request,
        "search.html",
        {"request": request, "template_config": template_config},
    )


@app.get("/import", response_class=HTMLResponse)
def get_import_page(request: Request):
    template_config = {
        "title": "Import",
        "show_days_until_payday": False,
        "show_meal_metrics": False,
        "days_are_stale": False,
        "show_quick_tray": False,
    }

    return templates.TemplateResponse(
        request,
        "import.html",
        {
            "request": request,
            "template_config": template_config,
            "schema_version": DINNER_IMPORT_SCHEMA_VERSION,
        },
    )


@app.get("/export", response_class=HTMLResponse)
def get_export_page(request: Request, db: Session = Depends(get_db)):
    meal_days = _fetch_meal_days_for_export(db)
    export_summary = _build_export_summary(meal_days)

    template_config = {
        "title": "Export",
        "show_days_until_payday": False,
        "show_meal_metrics": False,
        "days_are_stale": False,
        "show_quick_tray": False,
    }

    return templates.TemplateResponse(
        request,
        "export.html",
        {
            "request": request,
            "template_config": template_config,
            "export_summary": export_summary,
        },
    )


@app.get("/share/current-window", response_class=HTMLResponse)
def get_current_window_share_page(request: Request, db: Session = Depends(get_db)):
    meal_days = _fetch_or_create_current_window(db)
    share_context = _share_window_context(meal_days)

    template_config = {
        "title": "Share",
        "show_days_until_payday": False,
        "show_meal_metrics": False,
        "days_are_stale": False,
        "show_quick_tray": False,
    }

    return templates.TemplateResponse(
        request,
        "share_current_window.html",
        {
            "request": request,
            "template_config": template_config,
            "share": share_context,
        },
    )


@app.post("/api/import/dinner-plan", response_class=JSONResponse)
def import_dinner_plan(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    dry_run = is_truthy(payload.get("dry_run", True))
    plan = payload.get("plan")
    if plan is None:
        raise HTTPException(status_code=422, detail="Missing 'plan' field.")

    days = _validate_dinner_import_plan(plan)

    result = _preview_dinner_import_days(db, days)
    if not dry_run:
        result = _apply_dinner_import_days(db, days)
    return {
        "status": "preview" if dry_run else "imported",
        "dry_run": dry_run,
        **result,
    }


@app.get("/api/export/meals.json")
def export_meals_json(db: Session = Depends(get_db)):
    meal_days = _fetch_meal_days_for_export(db)
    payload = {
        "generated_at": _format_storage_timestamp(_now_utc()),
        "meal_day_count": len(meal_days),
        "meal_count": sum(len(meal_day.meals) for meal_day in meal_days),
        "meal_days": [_serialize_meal_day(meal_day) for meal_day in meal_days],
    }

    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="meal-planner-export.json"'
        },
    )


@app.get("/api/export/meals.csv")
def export_meals_csv(db: Session = Depends(get_db)):
    meal_days = _fetch_meal_days_for_export(db)

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
            "is_leftover",
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
                    bool(meal.is_leftover),
                ]
            )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="meal-planner-export.csv"'
        },
    )


@app.get("/api/share/current-window.pdf")
def share_current_window_pdf(db: Session = Depends(get_db)):
    meal_days = _fetch_or_create_current_window(db)
    date_range = _share_date_range(meal_days)
    pdf_bytes = _generate_current_window_pdf(meal_days)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{date_range["filename"]}"'
        },
    )


@app.post("/api/share/current-window/send", response_class=JSONResponse)
def send_current_window_share_email(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db),
):
    payload = payload or {}
    note = payload.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise HTTPException(status_code=422, detail="note must be a string.")

    recipient = payload.get("recipient") or os.getenv("MAILGUN_TO_EMAIL", "")
    recipient = _validate_single_email_address(recipient)

    if not _mailgun_send_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="A share email is already being sent. Try again in a moment.",
        )

    meal_days = _fetch_or_create_current_window(db)
    try:
        _check_mailgun_send_rate_limit()
        return _send_mailgun_share_email(meal_days, note.strip(), recipient)
    finally:
        _mailgun_send_lock.release()


@app.get("/api/how-many-times", response_class=JSONResponse)
def get_how_many_times_eat_out(db: Session = Depends(get_db)):
    # Get count of meals where is_takeout is True in the last 7 days
    seven_days_ago = _today_in_app_timezone() - timedelta(days=7)
    count = (
        db.query(Meal)
        .join(MealDay, Meal.meal_day_id == MealDay.id)
        .filter(Meal.is_takeout == True)
        .filter(MealDay.date >= seven_days_ago)
        .count()
    )
    return {"count": count}


@app.get("/api/rotation-suggestions")
def rotation_suggestions(meal_type: Optional[str] = None, db: Session = Depends(get_db)):
    # Get recent meals from the last 3 days
    recent_cutoff = _today_in_app_timezone() - timedelta(days=3)
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
