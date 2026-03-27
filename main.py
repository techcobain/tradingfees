import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from fees import analyze_fees, simulate_fees
from hyperliquid import fetch_all_fills, fetch_portfolio, fetch_spot_meta, fetch_user_fees

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
VALID_WINDOWS = {"all", "7d", "30d", "90d", "1yr"}
WINDOW_MS = {
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
    "90d": 90 * 24 * 60 * 60 * 1000,
    "1yr": 365 * 24 * 60 * 60 * 1000,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        headers={"Content-Type": "application/json"},
        timeout=httpx.Timeout(30, connect=10),
    )
    logger.info("HTTP client created")
    yield
    await app.state.http_client.aclose()
    logger.info("HTTP client closed")


app = FastAPI(title="Fee Savings Calculator", lifespan=lifespan)


class ORJSONResponse(Response):
    media_type = "application/json"

    def render(self, content) -> bytes:
        return orjson.dumps(content)


@app.post("/api/analyze", response_class=ORJSONResponse)
async def analyze(request: Request):
    body = await request.json()
    address = body.get("address", "").strip().lower()
    window = body.get("window", "all")

    if not ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address. Must be 0x followed by 40 hex characters.")
    if window not in VALID_WINDOWS:
        raise HTTPException(status_code=400, detail="Invalid time window.")

    client = request.app.state.http_client

    try:
        user_fees_data, portfolio_data, fills_data, spot_meta = await _fetch_data(client, address)
    except httpx.HTTPStatusError as e:
        logger.error(f"HL API error: {e}")
        raise HTTPException(status_code=502, detail="Hyperliquid API returned an error. Please try again.")
    except httpx.NetworkError as e:
        logger.error(f"HL API network error: {e}")
        raise HTTPException(status_code=502, detail="Hyperliquid API is temporarily unreachable. Please try again.")
    except httpx.TimeoutException:
        logger.error("HL API timeout")
        raise HTTPException(status_code=504, detail="Hyperliquid API timed out. Please try again.")

    fills = _filter_fills_by_window(fills_data["fills"], window)
    fills_data = {**fills_data, "fills": fills}
    result = analyze_fees(
        user_fees_data=user_fees_data,
        portfolio_data=portfolio_data,
        fills_data=fills_data,
        spot_meta=spot_meta,
        address=address,
        window=window,
    )
    result["window"] = window
    return result


@app.post("/api/simulate", response_class=ORJSONResponse)
async def simulate(request: Request):
    body = await request.json()
    window = body.get("window", "all")

    try:
        estimated_volume = float(body.get("estimated_volume", 0))
        taker_ratio = float(body.get("taker_ratio", 0.5))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid simulation inputs.")

    if window not in VALID_WINDOWS:
        raise HTTPException(status_code=400, detail="Invalid time window.")
    if estimated_volume <= 0:
        raise HTTPException(status_code=400, detail="Estimated volume must be greater than 0.")
    if taker_ratio < 0 or taker_ratio > 1:
        raise HTTPException(status_code=400, detail="Taker ratio must be between 0 and 1.")

    result = simulate_fees(estimated_volume=estimated_volume, taker_ratio=taker_ratio, window=window)
    result["window"] = window
    return result


async def _fetch_data(client: httpx.AsyncClient, address: str):
    """Fetch user fees and fills from Hyperliquid API."""
    # Fetch all HL data in parallel.
    import asyncio

    fees_task = asyncio.create_task(fetch_user_fees(client, address))
    portfolio_task = asyncio.create_task(fetch_portfolio(client, address))
    fills_task = asyncio.create_task(fetch_all_fills(client, address))
    spot_meta_task = asyncio.create_task(fetch_spot_meta(client))

    user_fees_data = await fees_task
    portfolio_data = await portfolio_task
    fills = await fills_task
    spot_meta = await spot_meta_task

    return user_fees_data, portfolio_data, fills, spot_meta


def _filter_fills_by_window(fills: list[dict], window: str) -> list[dict]:
    if window == "all" or not fills:
        return fills

    latest_time = max(int(fill["time"]) for fill in fills)
    cutoff = latest_time - WINDOW_MS[window]
    return [fill for fill in fills if int(fill["time"]) >= cutoff]


@app.get("/")
async def index():
    return FileResponse("static/index.html")


# Mount static files AFTER the root route
app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
