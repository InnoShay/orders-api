import time
import uuid
from fastapi import FastAPI, Request, Header, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOTAL_ORDERS = 53
RATE_LIMIT = 20          # max requests per window
RATE_WINDOW = 10         # seconds

app = FastAPI()

# CORS — allow all origins so the grader page can call directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# Fixed catalog of orders (IDs 1..T)
ORDERS = [{"id": i, "item": f"item_{i}", "quantity": (i % 5) + 1} for i in range(1, TOTAL_ORDERS + 1)]

# Idempotency store: key -> response body
idempotency_store: dict[str, dict] = {}

# Rate-limit buckets: client_id -> list of request timestamps
rate_buckets: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# Rate-limit helper
# ---------------------------------------------------------------------------
def check_rate_limit(client_id: str) -> float | None:
    """Return None if allowed, or seconds to wait (Retry-After) if limited."""
    now = time.time()
    window_start = now - RATE_WINDOW

    # Get or create bucket, prune old entries
    timestamps = rate_buckets.get(client_id, [])
    timestamps = [t for t in timestamps if t > window_start]
    rate_buckets[client_id] = timestamps

    if len(timestamps) >= RATE_LIMIT:
        # Earliest timestamp that still counts – client must wait until it expires
        retry_after = timestamps[0] - window_start
        return max(retry_after, 1.0)

    timestamps.append(now)
    return None


# ---------------------------------------------------------------------------
# Middleware — per-client rate limiting
# ---------------------------------------------------------------------------
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_id = request.headers.get("x-client-id")

    if client_id:
        retry_after = check_rate_limit(client_id)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"error": "rate limited"},
                headers={"Retry-After": str(int(retry_after))},
            )

    return await call_next(request)


# ---------------------------------------------------------------------------
# POST /orders  — idempotent order creation
# ---------------------------------------------------------------------------
@app.post("/orders", status_code=201)
async def create_order(
    request: Request,
    idempotency_key: str = Header(None, alias="Idempotency-Key"),
):
    # If we've seen this key before, return the same response
    if idempotency_key and idempotency_key in idempotency_store:
        return JSONResponse(
            status_code=201,
            content=idempotency_store[idempotency_key],
        )

    # Create a new order
    order = {"id": str(uuid.uuid4())}

    # Store for idempotency
    if idempotency_key:
        idempotency_store[idempotency_key] = order

    return JSONResponse(status_code=201, content=order)


# ---------------------------------------------------------------------------
# GET /orders  — cursor-based pagination over the fixed catalog
# ---------------------------------------------------------------------------
@app.get("/orders")
async def list_orders(
    limit: int = Query(default=10),
    cursor: str = Query(default=None),
):
    # Cursor is the *next* order ID to return (1-indexed), base-10 string
    # Default start = 1
    if cursor:
        try:
            start_id = int(cursor)
        except ValueError:
            start_id = 1
    else:
        start_id = 1

    # Clamp
    start_id = max(1, min(start_id, TOTAL_ORDERS + 1))

    # Slice
    start_idx = start_id - 1  # convert to 0-indexed
    end_idx = min(start_idx + limit, TOTAL_ORDERS)
    page = ORDERS[start_idx:end_idx]

    # Next cursor
    next_id = end_idx + 1  # back to 1-indexed
    next_cursor = str(next_id) if next_id <= TOTAL_ORDERS else None

    return {"items": page, "next_cursor": next_cursor}
