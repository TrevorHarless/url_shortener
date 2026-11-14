import hashlib
import secrets
from contextlib import asynccontextmanager
from datetime import datetime

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy import DateTime, String, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/shortener"
REDIS_URL = "redis://localhost:6379/0"
BASE_URL = "http://localhost:8000"
CACHE_TTL = 60 * 60  # 1 hour
SHORT_CODE_LENGTH = 7
BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# --- urls db ---------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class ShortURL(Base):
    __tablename__ = "short_urls"
    id: Mapped[int] = mapped_column(primary_key=True)
    short_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    long_url: Mapped[str] = mapped_column(String(2048))
    short_url: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


# --- hash generator for short_codes ----------------------------------------

def generate_short_code(long_url: str) -> str:
    """Hash the long URL plus a random salt and base62-encode the digest.

    The salt means the same URL shortened twice gets two different codes,
    and a retry after a collision produces a fresh code.
    """
    salt = secrets.token_bytes(8)
    digest = hashlib.sha256(long_url.encode() + salt).digest()
    n = int.from_bytes(digest, "big")
    chars = []
    for _ in range(SHORT_CODE_LENGTH):
        n, rem = divmod(n, 62)
        chars.append(BASE62[rem])
    return "".join(chars)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.redis = redis.from_url(REDIS_URL, decode_responses=True)
    yield
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


async def get_db():
    async with SessionLocal() as session:
        yield session


def get_redis() -> redis.Redis:
    return app.state.redis


class URLCreate(BaseModel):
    long_url: HttpUrl
    created_by: str | None = None


class URLResponse(BaseModel):
    short_code: str
    long_url: str
    short_url: str
    created_at: datetime
    created_by: str | None


@app.post("/urls", response_model=URLResponse, status_code=201)
async def create_url(payload: URLCreate, db: AsyncSession = Depends(get_db)):
    long_url = str(payload.long_url)

    for _ in range(5):
        short_code = generate_short_code(long_url)
        row = ShortURL(
            short_code=short_code,
            long_url=long_url,
            short_url=f"{BASE_URL}/{short_code}",
            created_by=payload.created_by,
        )
        db.add(row)
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
    else:
        raise HTTPException(500, "Could not generate a unique short code")

    await db.refresh(row) 
    return URLResponse(
        short_code=row.short_code,
        long_url=row.long_url,
        short_url=row.short_url,
        created_at=row.created_at,
        created_by=row.created_by,
    )

@app.get("/{short_code}")
async def redirect(
    short_code: str,
    db: AsyncSession = Depends(get_db),
    cache: redis.Redis = Depends(get_redis),
):
    # 1. Check cache (short_code: long_url)
    if long_url := await cache.get(short_code):
        return RedirectResponse(long_url)

    # 2. No hit: query urls db
    result = await db.execute(
        select(ShortURL.long_url).where(ShortURL.short_code == short_code)
    )
    long_url = result.scalar_one_or_none()
    if long_url is None:
        raise HTTPException(404, "Short URL not found")

    # 3. Populate cache with a 1 hr TTL
    await cache.set(short_code, long_url, ex=CACHE_TTL)
    return RedirectResponse(long_url)
