"""Steady background traffic so dashboards and SLOs have data.

    python loadgen.py --url http://localhost:8000 --rps 20
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time

import httpx


async def worker(client: httpx.AsyncClient, url: str, interval: float, stats: dict) -> None:
    while True:
        start = time.perf_counter()
        try:
            if random.random() < 0.7:
                r = await client.post(f"{url}/orders", json={"sku": f"SKU-{random.randint(1, 50)}", "qty": random.randint(1, 3)})
            else:
                r = await client.get(f"{url}/orders/{random.randint(1, 10_000)}")
            stats["ok" if r.status_code < 500 else "err"] += 1
        except httpx.HTTPError:
            stats["err"] += 1
        await asyncio.sleep(max(0.0, interval - (time.perf_counter() - start)))


async def main(url: str, rps: float, concurrency: int) -> None:
    stats = {"ok": 0, "err": 0}
    async with httpx.AsyncClient(timeout=5) as client:
        tasks = [asyncio.create_task(worker(client, url, concurrency / rps, stats)) for _ in range(concurrency)]
        while True:
            await asyncio.sleep(10)
            total = stats["ok"] + stats["err"]
            print(f"sent={total} errors={stats['err']} ({(stats['err'] / total * 100) if total else 0:.1f}%)", flush=True)
            if all(t.done() for t in tasks):
                break


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--rps", type=float, default=20)
    p.add_argument("--concurrency", type=int, default=10)
    a = p.parse_args()
    asyncio.run(main(a.url.rstrip("/"), a.rps, a.concurrency))
