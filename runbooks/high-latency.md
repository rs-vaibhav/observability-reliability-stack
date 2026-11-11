# SLOLatencyBurn / HighLatency – orders-service

**Severity:** critical (burn) / warning (p95) · **SLO:** 95% of requests < 300 ms

## Check
1. Dashboard → latency panel: is the *inventory dependency p95* line also up? If yes the problem is downstream.
2. Saturation panel: in-flight requests rising = we are queueing → capacity issue.
3. `kubectl -n shop top pods` – CPU at the 500m limit means throttling.
4. `kubectl -n shop get hpa` – is the HPA already at maxReplicas?

## Mitigate
1. Capacity: `kubectl -n shop scale deploy/orders-service --replicas=6` (or raise HPA max)
2. Throttling: raise the CPU limit in `k8s/app.yaml` and re-apply
3. Downstream: escalate to the dependency owner; shorten client timeouts so requests fail fast
4. Injected latency: reset `/admin/chaos`
