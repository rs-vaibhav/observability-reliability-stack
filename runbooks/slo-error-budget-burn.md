# SLOErrorBudgetBurn – orders-service

**Severity:** critical (page) · **SLO:** 99.5% of requests non-5xx over 30 days

## What it means
The 5xx ratio is high enough that, if it continues, the whole monthly error budget is gone in
under ~2 days (14.4x burn over 1h, or 6x over 6h). Users are seeing failed orders right now.

## Check (5 minutes)
1. Grafana → *orders-service – RED & SLO*. Is the 5xx line above the red page threshold? Since when?
2. Annotations on the dashboard: was a chaos injection or deploy made at that time?
3. Loki: `{service="orders-service", level="ERROR"} | json | line_format "{{.status}} {{.route}} {{.msg}}"`
   – one route or all? one pod or all?
4. `kubectl -n shop get pods -o wide` – restarts? all on one node?
5. Dependency panel – did inventory latency jump first?

## Mitigate
1. Recent deploy → `kubectl -n shop rollout undo deploy/orders-service`
2. One bad pod → `kubectl -n shop delete pod <pod>` (the Deployment replaces it)
3. Injected fault left on → `curl -X PUT localhost:8000/admin/chaos -d '{"latency_ms":0,"error_rate":0}' -H 'content-type: application/json'`
4. Dependency outage → escalate to the owning team; consider shedding non-critical traffic

## After
- Note how much budget was burned (dashboard gauge) in the incident ticket.
- If budget < 25%: freeze risky releases for this service until it recovers.
