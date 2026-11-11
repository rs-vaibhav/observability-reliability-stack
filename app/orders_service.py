"""orders-service: a small API instrumented for the RED method (Rate, Errors, Duration).

Exposes
  /orders          business endpoint (simulates a downstream call)
  /metrics         Prometheus metrics
  /healthz         liveness  - process is alive
  /readyz          readiness - dependency reachable and not draining
  /admin/chaos     fault injection used by the chaos experiments (latency / error rate)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

SERVICE = os.getenv("SERVICE_NAME", "orders-service")
VERSION = os.getenv("APP_VERSION", "1.0.0")
POD = os.getenv("HOSTNAME", "local")


# ---------- structured JSON logs (picked up by Promtail -> Loki) ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": SERVICE,
            "pod": POD,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "extra_fields", {}))
        return json.dumps(entry)


handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if os.getenv("LOG_FILE"):
    handlers.append(logging.FileHandler(os.environ["LOG_FILE"]))
for h in handlers:
    h.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.handlers, log.propagate = handlers, False
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def jlog(level: int, msg: str, **fields) -> None:
    log.log(level, msg, extra={"extra_fields": fields})


# ---------- metrics ----------
REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "method", "route", "code"])
LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["service", "method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 2, 5),
)
INFLIGHT = Gauge("http_requests_in_flight", "In-flight requests", ["service"])
DEP_LATENCY = Histogram("dependency_duration_seconds", "Downstream call latency", ["service", "dependency"])
CHAOS = Gauge("chaos_injection", "Active fault injection settings", ["service", "kind"])
BUILD = Gauge("app_build_info", "Build information", ["service", "version"])
BUILD.labels(SERVICE, VERSION).set(1)


class ChaosSettings(BaseModel):
    latency_ms: int = Field(0, ge=0, le=10000)
    error_rate: float = Field(0.0, ge=0.0, le=1.0)


chaos = ChaosSettings(
    latency_ms=int(os.getenv("CHAOS_LATENCY_MS", "0")),
    error_rate=float(os.getenv("CHAOS_ERROR_RATE", "0")),
)
state = {"draining": False}

@asynccontextmanager
async def lifespan(_: FastAPI):
    jlog(logging.INFO, "starting", version=VERSION)
    yield
    # Shutdown: Kubernetes has already removed the pod from endpoints during the preStop delay;
    # mark not-ready and let uvicorn finish in-flight requests (--timeout-graceful-shutdown).
    drain()


app = FastAPI(title=SERVICE, version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def red_metrics(request: Request, call_next):
    route = request.scope.get("route").path if request.scope.get("route") else request.url.path
    if request.url.path in ("/metrics", "/healthz", "/readyz"):
        return await call_next(request)
    INFLIGHT.labels(SERVICE).inc()
    start = time.perf_counter()
    code = 500
    try:
        response = await call_next(request)
        code = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - start
        route = request.scope.get("route").path if request.scope.get("route") else route
        REQUESTS.labels(SERVICE, request.method, route, str(code)).inc()
        LATENCY.labels(SERVICE, request.method, route).observe(elapsed)
        INFLIGHT.labels(SERVICE).dec()
        level = logging.ERROR if code >= 500 else logging.INFO
        jlog(level, "request", method=request.method, route=route, status=code, duration_ms=round(elapsed * 1000, 1))


async def call_inventory() -> None:
    """Simulated downstream dependency with realistic latency jitter."""
    with DEP_LATENCY.labels(SERVICE, "inventory").time():
        await asyncio.sleep(random.lognormvariate(-3.5, 0.5))  # ~30 ms median


class OrderIn(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    qty: int = Field(1, ge=1, le=100)


@app.post("/orders", status_code=201)
async def create_order(order: OrderIn):
    if chaos.latency_ms:
        await asyncio.sleep(chaos.latency_ms / 1000 * random.uniform(0.5, 1.5))
    await call_inventory()
    if chaos.error_rate and random.random() < chaos.error_rate:
        raise HTTPException(503, "inventory unavailable (injected fault)")
    return {"order_id": random.randint(100000, 999999), "sku": order.sku, "qty": order.qty, "served_by": POD}


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    await call_inventory()
    if chaos.error_rate and random.random() < chaos.error_rate:
        raise HTTPException(503, "inventory unavailable (injected fault)")
    return {"order_id": order_id, "status": "confirmed"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(response: Response):
    if state["draining"]:
        response.status_code = 503
        return {"status": "draining"}
    return {"status": "ready"}


@app.get("/admin/chaos")
def get_chaos():
    return chaos


@app.put("/admin/chaos")
def set_chaos(settings: ChaosSettings):
    global chaos
    chaos = settings
    CHAOS.labels(SERVICE, "latency_ms").set(settings.latency_ms)
    CHAOS.labels(SERVICE, "error_rate").set(settings.error_rate)
    jlog(logging.WARNING, "chaos settings changed", latency_ms=settings.latency_ms, error_rate=settings.error_rate)
    return chaos


def drain() -> None:
    state["draining"] = True
    jlog(logging.WARNING, "draining: readiness now failing")


@app.post("/admin/drain")
def admin_drain():
    """Take this instance out of rotation without killing it (maintenance)."""
    drain()
    return {"status": "draining"}
