from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, mapped_column

engine = create_engine(
    "sqlite:///./veloocity.db",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def enable_foreign_keys(connection, connection_record):
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()


class Base(DeclarativeBase):
    pass


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(100), nullable=False)
    email = mapped_column(String(254), unique=True, nullable=False)
    balance = mapped_column(Float, default=0.0, nullable=False)


class Station(Base):
    __tablename__ = "stations"

    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(100), nullable=False)
    location = mapped_column(String(255), nullable=False)


class Bike(Base):
    __tablename__ = "bikes"

    id = mapped_column(Integer, primary_key=True)
    model = mapped_column(String(100), nullable=False)
    station_id = mapped_column(
        ForeignKey("stations.id"), nullable=False
    )
    status = mapped_column(
        Enum(
            "available",
            "rented",
            "broken",
            name="bike_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default="available",
        nullable=False,
    )


class Rental(Base):
    __tablename__ = "rentals"

    id = mapped_column(Integer, primary_key=True)
    user_id = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    bike_id = mapped_column(
        ForeignKey("bikes.id"), nullable=False
    )
    start_time = mapped_column(
        DateTime, default=utc_now, nullable=False
    )
    end_time = mapped_column(DateTime, nullable=True)
    total_cost = mapped_column(Float, default=0.0, nullable=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(title="VelooCity", lifespan=lifespan)


@app.get("/")
def home():
    return {
        "message": "VelooCity работает",
        "tables": inspect(engine).get_table_names(),
    }

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

SessionLocal = sessionmaker(bind=engine)


def get_db():
    with SessionLocal() as db:
        yield db


class ApiError(Exception):
    def __init__(self, status_code, detail, error_code):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error_code": exc.error_code,
        },
    )


class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=254)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    balance: float


class DepositRequest(BaseModel):
    amount: float = Field(
        gt=0,
        multiple_of=0.01,
        allow_inf_nan=False,
    )


@app.post(
    "/users",
    response_model=UserResponse,
    status_code=201,
    tags=["Пользователи"],
    summary="Регистрация пользователя",
)
def create_user(data: UserCreate, db: Session = Depends(get_db)):
    user = User(
        name=data.name,
        email=str(data.email).lower(),
        balance=0.0,
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(
            409,
            "Пользователь с таким email уже существует",
            "EMAIL_ALREADY_EXISTS",
        )

    db.refresh(user)
    return user


@app.get(
    "/users/{user_id}",
    response_model=UserResponse,
    tags=["Пользователи"],
    summary="Получение пользователя",
)
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)

    if user is None:
        raise ApiError(
            404,
            "Пользователь не найден",
            "USER_NOT_FOUND",
        )

    return user


@app.patch(
    "/users/{user_id}/deposit",
    response_model=UserResponse,
    tags=["Пользователи"],
    summary="Пополнение баланса",
)
def deposit(
    user_id: int,
    data: DepositRequest,
    db: Session = Depends(get_db),
):
    result = db.execute(
        update(User)
        .where(User.id == user_id)
        .values(balance=func.round(User.balance + data.amount, 2))
    )

    if result.rowcount == 0:
        raise ApiError(
            404,
            "Пользователь не найден",
            "USER_NOT_FOUND",
        )

    db.commit()
    return db.get(User, user_id)


from typing import Literal, Optional

from fastapi import Query
from sqlalchemy import select

BikeStatus = Literal["available", "rented", "broken"]


class StationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=255)


class StationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    location: str
    available_bikes: int = 0


class BikeCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    model: str = Field(min_length=1, max_length=100)
    station_id: int = Field(gt=0)
    status: Literal["available", "broken"] = "available"


class BikeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model: str
    station_id: int
    status: BikeStatus


@app.post(
    "/stations",
    response_model=StationResponse,
    status_code=201,
    tags=["Станции"],
    summary="Создание станции",
)
def create_station(
    data: StationCreate,
    db: Session = Depends(get_db),
):
    station = Station(**data.model_dump())
    db.add(station)
    db.commit()
    db.refresh(station)
    return station


@app.get(
    "/stations",
    response_model=list[StationResponse],
    tags=["Станции"],
    summary="Список станций и количество свободных велосипедов",
)
def get_stations(db: Session = Depends(get_db)):
    query = (
        select(
            Station.id,
            Station.name,
            Station.location,
            func.count(Bike.id).label("available_bikes"),
        )
        .select_from(Station)
        .outerjoin(
            Bike,
            (Bike.station_id == Station.id)
            & (Bike.status == "available"),
        )
        .group_by(Station.id, Station.name, Station.location)
        .order_by(Station.id)
    )

    return db.execute(query).mappings().all()


@app.post(
    "/bikes",
    response_model=BikeResponse,
    status_code=201,
    tags=["Велосипеды"],
    summary="Добавление велосипеда",
)
def create_bike(
    data: BikeCreate,
    db: Session = Depends(get_db),
):
    station = db.get(Station, data.station_id)

    if station is None:
        raise ApiError(
            404,
            "Станция не найдена",
            "STATION_NOT_FOUND",
        )

    bike = Bike(**data.model_dump())
    db.add(bike)
    db.commit()
    db.refresh(bike)
    return bike


@app.get(
    "/bikes",
    response_model=list[BikeResponse],
    tags=["Велосипеды"],
    summary="Список велосипедов с фильтрами",
)
def get_bikes(
    station_id: Optional[int] = Query(default=None, gt=0),
    status: Optional[BikeStatus] = Query(default=None),
    db: Session = Depends(get_db),
):
    query = select(Bike)

    if station_id is not None:
        query = query.where(Bike.station_id == station_id)

    if status is not None:
        query = query.where(Bike.status == status)

    return db.scalars(query.order_by(Bike.id)).all()


from math import ceil

from sqlalchemy import text

PRICE_PER_HOUR = 100


class RentalStartRequest(BaseModel):
    user_id: int = Field(gt=0)
    bike_id: int = Field(gt=0)


class RentalEndRequest(BaseModel):
    rental_id: int = Field(gt=0)


class RentalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    bike_id: int
    start_time: datetime
    end_time: Optional[datetime]
    total_cost: float


@app.post(
    "/rentals/start",
    response_model=RentalResponse,
    status_code=201,
    tags=["Аренда"],
    summary="Начало аренды",
)
def start_rental(
    data: RentalStartRequest,
    db: Session = Depends(get_db),
):
    db.execute(text("BEGIN IMMEDIATE"))

    user = db.get(User, data.user_id)
    if user is None:
        raise ApiError(
            404, "Пользователь не найден", "USER_NOT_FOUND"
        )

    bike = db.get(Bike, data.bike_id)
    if bike is None:
        raise ApiError(
            404, "Велосипед не найден", "BIKE_NOT_FOUND"
        )

    if bike.status == "broken":
        raise ApiError(
            400, "Велосипед сломан", "BIKE_BROKEN"
        )

    if bike.status == "rented":
        raise ApiError(
            409,
            "Велосипед уже арендован",
            "BIKE_ALREADY_RENTED",
        )

    bike.status = "rented"

    rental = Rental(
        user_id=user.id,
        bike_id=bike.id,
        start_time=utc_now(),
        total_cost=0.0,
    )
    db.add(rental)
    db.commit()
    db.refresh(rental)
    return rental


@app.post(
    "/rentals/end",
    response_model=RentalResponse,
    tags=["Аренда"],
    summary="Завершение аренды",
)
def end_rental(
    data: RentalEndRequest,
    db: Session = Depends(get_db),
):
    db.execute(text("BEGIN IMMEDIATE"))

    rental = db.get(Rental, data.rental_id)
    if rental is None:
        raise ApiError(
            404, "Аренда не найдена", "RENTAL_NOT_FOUND"
        )

    if rental.end_time is not None:
        raise ApiError(
            409,
            "Аренда уже завершена",
            "RENTAL_ALREADY_ENDED",
        )

    user = db.get(User, rental.user_id)
    if user is None:
        raise ApiError(
            404, "Пользователь не найден", "USER_NOT_FOUND"
        )

    bike = db.get(Bike, rental.bike_id)
    if bike is None:
        raise ApiError(
            404, "Велосипед не найден", "BIKE_NOT_FOUND"
        )

    rental.end_time = utc_now()

    seconds = (
        rental.end_time - rental.start_time
    ).total_seconds()
    hours = max(1, ceil(seconds / 3600))
    rental.total_cost = float(hours * PRICE_PER_HOUR)

    user.balance = round(user.balance - rental.total_cost, 2)
    bike.status = "available"
    insufficient_funds = user.balance < 0

    # Сохраняем завершение даже при недостатке средств.
    db.commit()
    db.refresh(rental)

    if insufficient_funds:
        raise ApiError(
            400,
            "Аренда завершена. Средств недостаточно, "
            "баланс ушёл в минус",
            "INSUFFICIENT_FUNDS",
        )

    return rental


@app.get(
    "/rentals/{user_id}",
    response_model=list[RentalResponse],
    tags=["Аренда"],
    summary="История аренд пользователя",
)
def get_rentals(
    user_id: int,
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise ApiError(
            404, "Пользователь не найден", "USER_NOT_FOUND"
        )

    query = (
        select(Rental)
        .where(Rental.user_id == user_id)
        .order_by(Rental.start_time.desc(), Rental.id.desc())
    )
    return db.scalars(query).all()