import hashlib
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from database import Database

# ──────────────────────────────────────────────────────────────────────
# Structured JSON Logging
# ──────────────────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
        }
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("ash_orders")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# ──────────────────────────────────────────────────────────────────────
# Pydantic Models
# ──────────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    customer_id: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    quantity: int = Field(..., gt=0)


class OrderResponse(BaseModel):
    order_id: str
    status: str


class OrderDetail(BaseModel):
    order_id: str
    customer_id: str
    item_id: str
    quantity: int
    status: str
    created_at: str


class LedgerEntry(BaseModel):
    ledger_id: str
    order_id: str
    customer_id: str
    amount: float
    type: str
    created_at: str

# ──────────────────────────────────────────────────────────────────────
# Application Lifespan (startup / shutdown)
# ──────────────────────────────────────────────────────────────────────

db = Database()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.initialize()
    logger.info("Database initialized")
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="Ash-orders",
    description="Idempotent Order API – CS-218 Assignment 2",
    version="1.0.0",
    lifespan=lifespan,
)

# ──────────────────────────────────────────────────────────────────────
# Middleware – attach a unique Request-ID to every request
# ──────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id

    logger.info(
        "Incoming request: %s %s",
        request.method,
        request.url.path,
        extra={"request_id": request_id},
    )

    start = time.time()
    response: Response = await call_next(request)
    elapsed_ms = round((time.time() - start) * 1000, 2)

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "Response: status=%d elapsed_ms=%.2f",
        response.status_code,
        elapsed_ms,
        extra={"request_id": request_id},
    )
    return response

# ──────────────────────────────────────────────────────────────────────
# Helper – compute a SHA-256 fingerprint of the request body
# ──────────────────────────────────────────────────────────────────────

def compute_fingerprint(body: dict) -> str:
    """Deterministic hash of the request payload."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

# ──────────────────────────────────────────────────────────────────────
# POST /orders  –  Create an order (idempotent)
# ──────────────────────────────────────────────────────────────────────

@app.post("/orders", status_code=201, response_model=OrderResponse)
async def create_order(
    order: OrderRequest,
    request: Request,
    idempotency_key: str = Header(None, alias="Idempotency-Key"),
    x_debug_fail_after_commit: str = Header(None, alias="X-Debug-Fail-After-Commit"),
):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    # Validate Idempotency-Key header
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Missing Idempotency-Key header",
                "message": "All write requests require an Idempotency-Key header.",
            },
        )

    body_dict = order.model_dump()
    fingerprint = compute_fingerprint(body_dict)

    logger.info(
        "Processing order: idempotency_key=%s fingerprint=%s",
        idempotency_key,
        fingerprint,
        extra={"request_id": request_id},
    )

    # Check for an existing idempotency record
    existing = db.get_idempotency_record(idempotency_key)

    if existing:
        # Same key, different payload - 409 Conflict
        if existing["request_fingerprint"] != fingerprint:
            logger.warning(
                "Idempotency key reuse with different payload: key=%s",
                idempotency_key,
                extra={"request_id": request_id},
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Idempotency key conflict",
                    "message": (
                        "This Idempotency-Key has already been used with a "
                        "different request payload."
                    ),
                },
            )

        # Same key + same payload - return the stored response (idempotent replay)
        logger.info(
            "Idempotent replay: key=%s, returning stored response",
            idempotency_key,
            extra={"request_id": request_id},
        )
        stored_response = json.loads(existing["response_body"])
        return JSONResponse(
            status_code=existing["response_status_code"],
            content=stored_response,
        )

    #First request – create order + ledger atomically
    order_id = str(uuid.uuid4())
    ledger_id = str(uuid.uuid4())
    amount = round(order.quantity * 10.00, 2) 

    response_body = {"order_id": order_id, "status": "created"}

    #Making sure the order is created before the erroris displayed
    db.create_order_atomic(
        order_id=order_id,
        customer_id=order.customer_id,
        item_id=order.item_id,
        quantity=order.quantity,
        ledger_id=ledger_id,
        amount=amount,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        response_body=json.dumps(response_body),
        response_status_code=201,
    )

    logger.info(
        "Order created: order_id=%s ledger_id=%s",
        order_id,
        ledger_id,
        extra={"request_id": request_id},
    )

    # Simulate "commit succeeded, response failed" 
    if x_debug_fail_after_commit and x_debug_fail_after_commit.lower() == "true":
        logger.warning(
            "DEBUG: Simulating response failure after commit (order_id=%s)",
            order_id,
            extra={"request_id": request_id},
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Simulated failure",
                "message": "Debug: response dropped after successful commit.",
            },
        )

    return JSONResponse(status_code=201, content=response_body)

# ──────────────────────────────────────────────────────────────────────
# GET /orders/{order_id}  –  Retrieve a single order
# ──────────────────────────────────────────────────────────────────────

@app.get("/orders/{order_id}", response_model=OrderDetail)
async def get_order(order_id: str, request: Request):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    row = db.get_order(order_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": "Order not found", "order_id": order_id},
        )

    logger.info(
        "Order retrieved: order_id=%s",
        order_id,
        extra={"request_id": request_id},
    )
    return OrderDetail(**row)

# ──────────────────────────────────────────────────────────────────────
# GET /orders  –  List all orders
# ──────────────────────────────────────────────────────────────────────

@app.get("/orders")
async def list_orders():
    return db.list_orders()

# ──────────────────────────────────────────────────────────────────────
# GET /ledger  –  List all ledger entries
# ──────────────────────────────────────────────────────────────────────

@app.get("/ledger")
async def list_ledger():
    return db.list_ledger()

# ──────────────────────────────────────────────────────────────────────
# GET /health  –  Health check
# ──────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time()}

# ──────────────────────────────────────────────────────────────────────
# DELETE /reset
# ──────────────────────────────────────────────────────────────────────

@app.delete("/reset")
async def reset():
    db.reset()
    return {"status": "reset", "message": "All tables cleared"}
