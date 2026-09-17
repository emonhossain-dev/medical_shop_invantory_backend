import uvicorn
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.api import router
from contextlib import asynccontextmanager
from app.core.scheduler import start_scheduler, shutdown_scheduler
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text


@asynccontextmanager
async def lifespan(app:FastAPI):
    print("Starting Application")
    print("Start Scheduler")
    start_scheduler()
    yield
    print("Exiting Application")
    shutdown_scheduler()
    print("shutdown_scheduler")

app = FastAPI(
    title="Simple API",
    description="Simple API",
    version="1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api/v1")

app.mount("/static", StaticFiles(directory="static"), name="static")





@app.get("/", tags=["root"])
async def root():
    return {"message": "Hello World"}


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "database": "disconnected", "error": str(e)}
        )


if __name__ == '__main__':
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )