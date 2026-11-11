# ServiceDown – Prometheus cannot scrape orders-service

**Severity:** critical · burn-rate and latency alerts for the same service are inhibited while this fires.

## Check
1. `kubectl -n shop get pods` – CrashLoopBackOff? Pending? 0 ready?
2. `kubectl -n shop describe pod <pod>` – OOMKilled, image pull error, failed probes, unschedulable?
3. `kubectl -n shop logs <pod> --previous`
4. `kubectl -n shop get endpoints orders-service` – no addresses means no ready pods.
5. Docker compose: `docker compose ps` and `docker compose logs orders-service`

## Mitigate
1. Bad release → `kubectl -n shop rollout undo deploy/orders-service`
2. OOMKilled → raise memory limit, redeploy
3. Pending (no capacity) → `kubectl get nodes`; uncordon nodes left cordoned after a drain
4. Only Prometheus can't reach it (pods healthy) → check ServiceMonitor labels and NetworkPolicy

## Expected recovery (measured with chaos experiments)
| Fault | Recovery | User impact |
|---|---|---|
| One pod killed | see `chaos/reports/*-pod-kill.md` | none expected with 3 replicas |
| Node drained | see `chaos/reports/*-node-drain.md` | none expected (PDB minAvailable=2) |
