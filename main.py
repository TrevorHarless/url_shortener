import secrets
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy import String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/shortener"
REDIS_URL = "redis://localhost:6379/0"
CACHE_TTL = 60 * 60 * 24  # 1 day

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class ShortURL(Base):
    __tablename__ = "short_urls"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    original_url: Mapped[str] = mapped_column(String(2048))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables (use Alembic for real migrations later)
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
    url: HttpUrl


class URLResponse(BaseModel):
    short_code: str
    short_url: str
    original_url: str


@app.post("/shorten", response_model=URLResponse, status_code=201)
async def create_short_url(
    payload: URLCreate,
    db: AsyncSession = Depends(get_db),
    cache: redis.Redis = Depends(get_redis),
):
    original = str(payload.url)

    # Let the DB's unique constraint catch collisions, retry a few times
    for _ in range(5):
        code = secrets.token_urlsafe(4)[:6]
        db.add(ShortURL(code=code, original_url=original))
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
    else:
        raise HTTPException(500, "Could not generate a unique code")

    # Optional: warm the cache since new links often get clicked right away
    await cache.set(f"url:{code}", original, ex=CACHE_TTL)

    return URLResponse(
        short_code=code,
        short_url=f"http://localhost:8000/{code}",
        original_url=original,
    )


@app.get("/{code}")
async def redirect(
    code: str,
    db: AsyncSession = Depends(get_db),
    cache: redis.Redis = Depends(get_redis),
):
    key = f"url:{code}"

    # 1. Check cache
    if cached := await cache.get(key):
        return RedirectResponse(cached)

    # 2. Cache miss: hit Postgres
    result = await db.execute(select(ShortURL.original_url).where(ShortURL.code == code))
    original = result.scalar_one_or_none()
    if original is None:
        raise HTTPException(404, "Short URL not found")

    # 3. Populate cache for next time
    await cache.set(key, original, ex=CACHE_TTL)
    return RedirectResponse(original)