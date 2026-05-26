"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.database import init_db
from app.routes import router

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialized (app v%s)", __version__)
    yield


app = FastAPI(
    title="Agentic Financial Fraud Investigation Platform",
    description=(
        "Multi-agent fraud detection: Investigator (Bot A) + Compliance Report (Bot B). "
        "Phase 1: local SQLite. Phase 2: BigQuery + GCS + Cloud Run."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
def root():
    return {
        "service": "Agentic Financial Fraud Investigation Platform",
        "version": __version__,
        "docs": "/docs",
        "endpoints": [
            "POST /generate-transactions",
            "POST /sync-to-bigquery",
            "POST /run-fraud-investigation",
            "GET /findings",
            "GET /reports/latest",
        ],
        "adk_cli": "adk web adk_agents/fraud_detective",
    }
