# Observability & Reliability Stack (Prometheus · Grafana · Loki · Kubernetes)

A production-style monitoring setup for a microservice, plus **chaos experiments that measure how
fast it recovers**. Built around SRE practice: RED metrics, SLOs with error budgets,
multi-window burn-rate alerting, structured logs, runbooks, and tested failure modes.

```
              ┌───────────── orders-service (FastAPI, 3 replicas) ─────────────┐
 loadgen ───► │ /metrics (RED + dependency latency)   JSON logs → stdout/file  │
              └───────┬────────────────────────────────────────┬───────────────┘
                      │ scrape 15s                             │ tail
                ┌─────▼──────┐  SLO recording +    ┌───────────▼─┐
                │ Prometheus │  burn-rate alerts   │  Promtail   │
                └──┬──────┬──┘                     └──────┬──────┘
         alerts    │      │ queries                       │ push
       ┌───────────▼┐   ┌─▼──────────────────────┐   ┌────▼───┐
       │Alertmanager│   │ Grafana: RED + SLO +   │◄──│  Loki  │
       └─────┬──────┘   │ error budget + logs    │   └────────┘
             │ webhook  └────────────────────────┘
             ▼
   AIOps triage agent (sister project) → ITSM ticket
```

## SLOs

| SLO | Target | Error budget (30d) | Page when | Ticket when |
|---|---|---|---|---|
| Availability (non-5xx) | 99.5 % | 0.5 % | 14.4x burn over 1h & 5m, or 6x over 6h & 30m | 3x over 1d & 2h, or 1x over 3d & 6h |
| Latency (< 300 ms) | 95 % | 5 % | 14.4x / 6x burn (same windows) | p95 > 300 ms for 10m |

Rules live in one file, `prometheus/rules/slo.yml`, used by both Docker Compose and Kubernetes
(the setup script wraps them into a `PrometheusRule`). They are **unit-tested with `promtool`**:
a 10 % error rate must page, 0.3 % must not, and a dead target must raise `ServiceDown`.
`ServiceDown` inhibits the burn-rate alerts so on-call gets one page, not five.

## Reliability built into the deployment (`k8s/app.yaml`)

- 3 replicas spread across nodes (`topologySpreadConstraints`), **PDB `minAvailable: 2`**
- startup / readiness / liveness probes on separate endpoints (`/healthz` vs `/readyz`)
- graceful shutdown: `preStop` delay so endpoints are removed before the pod stops, readiness flips to
  "draining", uvicorn finishes in-flight requests
- HPA on CPU, resource requests/limits, non-root read-only container

## Quick start (Docker Compose, ~2 minutes)

```bash
docker compose up -d --build
open http://localhost:3000            # dashboard "orders-service - RED & SLO"
```

Then break it and watch the dashboard, alerts and logs react:

```bash
python chaos/chaos.py errors  --error-rate 0.2 --duration 300   # SLOErrorBudgetBurn fires after ~2 min
python chaos/chaos.py latency --latency-ms 600 --duration 300   # SLOLatencyBurn / HighLatency
python chaos/chaos.py container-kill                           # restart policy recovery time
```

## Full Kubernetes setup (kind, 4 nodes)

```bash
./k8s/setup.sh          # kind cluster + kube-prometheus-stack + Loki + Promtail + app + SLO rules + dashboard
python chaos/chaos.py pod-kill   --url http://localhost:8000
python chaos/chaos.py node-drain --url http://localhost:8000
```

## Chaos experiments (`chaos/chaos.py`)

Each run measures a **steady-state baseline**, injects one fault, keeps probing like a real user
(5 req/s), and writes a markdown report to `chaos/reports/` with:

- success rate and p50/p95 latency **before / during / after** the fault
- **time to recover** (ready replicas back to desired, or `/readyz` healthy)
- **longest user-visible outage** in seconds
- PASS if clients saw ≥ 99 % success during the fault

| Experiment | Fault | What it proves |
|---|---|---|
| `pod-kill` | delete a random pod | replicas + readiness keep the service up |
| `node-drain` | cordon + drain a node | PDB and topology spread prevent an outage during maintenance |
| `container-kill` | `docker kill` | restart policy recovery time (single instance → visible outage) |
| `errors` | inject a 5xx rate | burn-rate alerts fire and route to the triage agent |
| `latency` | inject latency | latency SLO alerts fire; dashboard shows it is *our* service, not the dependency |

Example from a local run (`errors`, 30 % injected for 6 s): baseline 100 % → **68 %** during the fault →
100 % after.

## Repository layout

```
app/          orders-service (FastAPI + prometheus_client), tests
loadgen/      async traffic generator
prometheus/   scrape config, SLO recording + alert rules, promtool unit tests
alertmanager/ routing, inhibition, webhook to the triage agent
loki/         Loki + Promtail (JSON parsing, low-cardinality labels)
grafana/      provisioned datasources + RED/SLO dashboard (with chaos annotations from Loki)
k8s/          kind cluster, app manifests (PDB, HPA, probes, ServiceMonitor), Helm values, setup.sh
chaos/        chaos experiment runner + reports
runbooks/     ServiceDown, SLOErrorBudgetBurn, high latency
```

## Verified

- `pytest`: 5 app tests (RED metrics, bounded route labels, fault injection, readiness draining)
- `promtool check rules` (22 rules) and `promtool test rules` (3 scenarios) pass
- `amtool check-config`, `loki -verify-config`, `promtail -check-syntax` pass
- `kubeconform -strict` validates the Kubernetes manifests
