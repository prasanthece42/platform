"""A durable SQLite ledger with validated amounts and idempotent creation."""

import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_cents: Annotated[int, Field(strict=True, gt=0, le=100_000_000)]
    currency: Literal["USD", "CAD", "EUR", "GBP"] = "USD"
    description: Annotated[str, Field(max_length=200)] = ""


class Payment(PaymentCreate):
    id: str
    status: Literal["recorded", "cancelled"]
    created_at: str


def create_app(database_path: Path | None = None) -> FastAPI:
    database = database_path or Path(os.environ.get("PAYMENTS_DATABASE_PATH", "data/payments.db"))

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.parent.mkdir(parents=True, exist_ok=True)
        with connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    currency TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('recorded', 'cancelled')),
                    created_at TEXT NOT NULL
                )
            """)
        yield

    api = FastAPI(
        title="Team Payment API",
        version="0.1.0",
        description="Create, view and cancel payment records. This demo never charges money.",
        lifespan=lifespan,
    )

    @api.get("/health")
    def health() -> dict[str, str]:
        with connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @api.post("/payments", response_model=Payment, status_code=201)
    def create_payment(
        body: PaymentCreate,
        response: Response,
        idempotency_key: Annotated[
            str, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
        ],
    ) -> Payment:
        with connection() as conn:
            identifier = str(uuid4())
            created = datetime.now(UTC).isoformat()
            cursor = conn.execute(
                """INSERT INTO payments VALUES (?, ?, ?, ?, ?, 'recorded', ?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (
                    identifier,
                    idempotency_key,
                    body.amount_cents,
                    body.currency,
                    body.description,
                    created,
                ),
            )
            row = conn.execute(
                "SELECT id, amount_cents, currency, description, status, created_at FROM payments WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            payment = Payment.model_validate(dict(row)) if row else None
            if payment is None:
                raise HTTPException(500, "Payment record could not be retrieved")
            if any(getattr(payment, key) != value for key, value in body.model_dump().items()):
                raise HTTPException(
                    409, "This idempotency key was used with different payment details"
                )
            if cursor.rowcount == 0:
                response.status_code = 200
            response.headers["Location"] = f"/payments/{payment.id}"
            return payment

    @api.get("/payments", response_model=list[Payment])
    def list_payments(limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[Payment]:
        with connection() as conn:
            rows = conn.execute(
                "SELECT id, amount_cents, currency, description, status, created_at FROM payments ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Payment.model_validate(dict(row)) for row in rows]

    @api.get("/payments/{payment_id}", response_model=Payment)
    def get_payment(payment_id: str) -> Payment:
        with connection() as conn:
            row = conn.execute(
                "SELECT id, amount_cents, currency, description, status, created_at FROM payments WHERE id = ?",
                (payment_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Payment record not found")
        return Payment.model_validate(dict(row))

    @api.post("/payments/{payment_id}/cancel", response_model=Payment)
    def cancel_payment(payment_id: str) -> Payment:
        with connection() as conn:
            cursor = conn.execute(
                "UPDATE payments SET status = 'cancelled' WHERE id = ?", (payment_id,)
            )
            if cursor.rowcount == 0:
                raise HTTPException(404, "Payment record not found")
            row = conn.execute(
                "SELECT id, amount_cents, currency, description, status, created_at FROM payments WHERE id = ?",
                (payment_id,),
            ).fetchone()
        return Payment.model_validate(dict(row))

    return api


app = create_app()
