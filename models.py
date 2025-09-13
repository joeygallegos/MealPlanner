# models.py
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    Date,
    Boolean,
    String,
    Enum,
    Text,
    ForeignKey,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import enum
import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from urllib.parse import quote_plus

# Load environment variables from .env file
load_dotenv()

Base = declarative_base()


class MealType(enum.Enum):
    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"


class MealDay(Base):
    __tablename__ = "meal_days"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, unique=True)
    is_starred = Column(Boolean, default=False)
    is_sammy_working = Column(Boolean, default=False)
    meals = relationship("Meal", back_populates="day", cascade="all, delete-orphan")


class Meal(Base):
    __tablename__ = "meals"
    id = Column(Integer, primary_key=True, index=True)
    meal_day_id = Column(Integer, ForeignKey("meal_days.id"), nullable=False)
    type = Column(Enum(MealType), nullable=False)
    description = Column(Text)
    cooking_user = Column(String(10), nullable=True)  # NEW: 'Joey' or 'Sam'
    is_favorite = Column(Boolean, default=False, nullable=False)
    is_takeout = Column(Boolean, default=False, nullable=False)
    day = relationship("MealDay", back_populates="meals")


# Database connection URL; you can configure it using environment variables.
username = os.getenv("DB_USER", "")
password = quote_plus(os.getenv("DB_PASS", ""))
host = os.getenv("DB_HOST", "")
port = os.getenv("DB_PORT", "")
database = os.getenv("DB_NAME", "")
DATABASE_URL = f"mysql+pymysql://{username}:{password}@{host}:{port}/{database}"
print("DATABASE_URL:", DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
