"""Chaos experiments with measured outcomes.

Each experiment:
  1. records a steady-state baseline (client-side success rate and latency),
  2. injects one fault,
  3. keeps probing the service like a real client while the system recovers,
  4. writes a markdown report with availability during the fault and time-to-recover.

Experiments
  pod-kill       delete one random orders-service pod                       (Kubernetes)
  node-drain     cordon + drain the node of one pod, then uncordon          (Kubernetes)
  latency        inject latency through /admin/chaos for N seconds           (any)
  errors         inject an error rate through /admin/chaos for N seconds     (any)
  container-kill docker kill the container (restart policy brings it back)  (docker compose)

    python chaos/chaos.py pod-kill --url http://localhost:8000
    python chaos/chaos.py latency  --url http://localhost:8000 --latency-ms 600 --duration 120
    python chaos/chaos.py container-kill --url http://localhost:8000 --container observability-reliability-stack-orders-service-1
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

NS, DEPLOY, LABEL = "shop", "orders-service", "app=orders-service"
REPORTS = Path(__file__).resolve().parent / "reports"


# --------------------------------------------------------------------------- probing
@dataclass
class Sample:
    t: float
    ok: bool
    ms: float


@dataclass
class Prober:
    """Sends one request every `interval` seconds, like a user, and records the outcome."""

    url: str
    interval: float = 0.2
    samples: list[Sample] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)

    def _one(self) -> Sample:
        start = time.perf_counter()
        body = json.dumps({"sku": "CHAOS-PROBE", "qty": 1}).encode()
        req = urllib.request.Request(f"{self.url}/orders", data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                ok = r.status < 500
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            ok = False
        return Sample(time.time(), ok, (time.perf_counter() - start) * 1000)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self._one())
            self._stop.wait(self.interval)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def window(self, start: float, end: float) -> list[Sample]:
        return [s for s in self.samples if start <= s.t <= end]


def summarize(samples: list[Sample]) -> dict:
    if not samples:
        return {"requests": 0, "availability": None, "p95_ms": None}
    lat = sorted(s.ms for s in samples if s.ok) or [0.0]
    return {
        "requests": len(samples),
        "availability": round(sum(s.ok for s in samples) / len(samples), 4),
        "p50_ms": round(statistics.median(lat), 1),
        "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
    }


def longest_outage(samples: list[Sample]) -> float:
    """Longest continuous run of failed probes, in seconds (what users actually felt)."""
    worst, start = 0.0, None
    for s in samples:
        if not s.ok and start is None:
            start = s.t
        elif s.ok and start is not None:
            worst, start = max(worst, s.t - start), None
    if start is not None and samples:
        worst = max(worst, samples[-1].t - start)
    return round(worst, 2)


# --------------------------------------------------------------------------- helpers
def sh(*args: str, check: bool = True) -> str:
    return subprocess.run(args, check=check, capture_output=True, text=True).stdout.strip()


def kubectl_json(*args: str) -> dict:
    return json.loads(sh("kubectl", *args, "-o", "json"))


def ready_replicas() -> tuple[int, int]:
    d = kubectl_json("-n", NS, "get", "deploy", DEPLOY)
    return d["status"].get("readyReplicas", 0), d["spec"]["replicas"]


def all_ready() -> bool:
    ready, desired = ready_replicas()
    return ready == desired


def wait_until(predicate, timeout: float = 300, step: float = 0.5) -> float:
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return round(time.time() - start, 2)
        time.sleep(step)
    raise TimeoutError("system did not recover within timeout")


def set_chaos(url: str, latency_ms: int = 0, error_rate: float = 0.0) -> None:
    body = json.dumps({"latency_ms": latency_ms, "error_rate": error_rate}).encode()
    req = urllib.request.Request(f"{url}/admin/chaos", data=body, method="PUT", headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/readyz", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- experiments
def exp_pod_kill(args) -> dict:
    pods = kubectl_json("-n", NS, "get", "pods", "-l", LABEL)["items"]
    victim = random.choice(pods)["metadata"]["name"]
    t0 = time.time()
    sh("kubectl", "-n", NS, "delete", "pod", victim, "--wait=false")
    time.sleep(1)
    recovery = wait_until(all_ready) + 1
    return {"fault": f"deleted pod {victim}", "fault_start": t0, "recovery_seconds": recovery,
            "recovery_definition": "ready replicas back to desired count"}


def exp_node_drain(args) -> dict:
    pods = kubectl_json("-n", NS, "get", "pods", "-l", LABEL)["items"]
    node = random.choice(pods)["spec"]["nodeName"]
    t0 = time.time()
    sh("kubectl", "drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=180s")
    try:
        wait_until(all_ready)
    finally:
        sh("kubectl", "uncordon", node)
    return {"fault": f"drained node {node} (PDB minAvailable=2 enforced)", "fault_start": t0,
            "recovery_seconds": round(time.time() - t0, 2),
            "recovery_definition": "drain finished and all replicas ready on other nodes"}


def exp_injection(args, latency_ms: int, error_rate: float) -> dict:
    t0 = time.time()
    set_chaos(args.url, latency_ms, error_rate)
    try:
        time.sleep(args.duration)
    finally:
        set_chaos(args.url, 0, 0.0)
    return {"fault": f"latency_ms={latency_ms} error_rate={error_rate} for {args.duration}s",
            "fault_start": t0, "recovery_seconds": 0.0,
            "recovery_definition": "fault removed manually; check that SLO alerts fired in Alertmanager"}


def exp_container_kill(args) -> dict:
    t0 = time.time()
    sh("docker", "kill", args.container)
    time.sleep(1)
    recovery = wait_until(lambda: http_ok(args.url)) + 1
    return {"fault": f"docker kill {args.container}", "fault_start": t0, "recovery_seconds": recovery,
            "recovery_definition": "container restarted by restart policy and /readyz returns 200"}


# --------------------------------------------------------------------------- report
def write_report(name: str, result: dict, baseline: dict, during: dict, after: dict, outage: float) -> Path:
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"{stamp}-{name}.md"
    verdict = "PASS" if (during["availability"] or 0) >= 0.99 else "NEEDS WORK"
    path.write_text(f"""# Chaos experiment: {name}

- **When:** {stamp}
- **Fault:** {result['fault']}
- **Recovery time:** {result['recovery_seconds']} s ({result['recovery_definition']})
- **Longest user-visible outage:** {outage} s
- **Verdict (>= 99% client success during fault):** {verdict}

| Phase | Requests | Success rate | p50 ms | p95 ms |
|---|---|---|---|---|
| Baseline | {baseline['requests']} | {baseline['availability']} | {baseline.get('p50_ms')} | {baseline.get('p95_ms')} |
| During fault + recovery | {during['requests']} | {during['availability']} | {during.get('p50_ms')} | {during.get('p95_ms')} |
| After recovery | {after['requests']} | {after['availability']} | {after.get('p50_ms')} | {after.get('p95_ms')} |

## Follow-ups
- [ ] Did the expected alert fire (Alertmanager / Grafana)? Link it here.
- [ ] Did the runbook steps work as written? Update the runbook if not.
""")
    return path


EXPERIMENTS = {
    "pod-kill": exp_pod_kill,
    "node-drain": exp_node_drain,
    "latency": lambda a: exp_injection(a, a.latency_ms, 0.0),
    "errors": lambda a: exp_injection(a, 0, a.error_rate),
    "container-kill": exp_container_kill,
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("experiment", choices=EXPERIMENTS)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--baseline", type=float, default=15, help="seconds of steady state before the fault")
    p.add_argument("--settle", type=float, default=15, help="seconds to observe after recovery")
    p.add_argument("--duration", type=float, default=60, help="injection duration (latency/errors)")
    p.add_argument("--latency-ms", type=int, default=600)
    p.add_argument("--error-rate", type=float, default=0.2)
    p.add_argument("--container", default="observability-reliability-stack-orders-service-1")
    args = p.parse_args()
    args.url = args.url.rstrip("/")

    prober = Prober(args.url)
    prober.start()
    print(f"[chaos] steady state for {args.baseline}s ...")
    t_start = time.time()
    time.sleep(args.baseline)
    baseline = summarize(prober.window(t_start, time.time()))
    if not baseline["availability"] or baseline["availability"] < 0.99:
        prober.stop()
        raise SystemExit(f"[chaos] aborting: service is not healthy before the experiment {baseline}")

    print(f"[chaos] injecting fault: {args.experiment}")
    result = EXPERIMENTS[args.experiment](args)
    t_recovered = time.time()
    print(f"[chaos] recovered in {result['recovery_seconds']}s, observing for {args.settle}s ...")
    time.sleep(args.settle)
    prober.stop()

    during_samples = prober.window(result["fault_start"], t_recovered)
    during = summarize(during_samples)
    after = summarize(prober.window(t_recovered, time.time()))
    path = write_report(args.experiment, result, baseline, during, after, longest_outage(during_samples))
    print(json.dumps({"baseline": baseline, "during": during, "after": after, **result}, indent=2, default=str))
    print(f"[chaos] report: {path}")


if __name__ == "__main__":
    main()
