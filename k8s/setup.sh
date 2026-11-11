#!/usr/bin/env bash
# Builds a 4-node kind cluster with the full observability stack and the orders-service.
# Requires: docker, kind, kubectl, helm, python3 (with pyyaml)
set -euo pipefail
cd "$(dirname "$0")/.."

kind get clusters | grep -q '^reliability$' || kind create cluster --config k8s/kind-cluster.yaml

echo ">> building images"
docker build -t orders-service:local app
docker build -t orders-loadgen:local loadgen
kind load docker-image orders-service:local orders-loadgen:local --name reliability

echo ">> installing kube-prometheus-stack, loki, promtail"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update >/dev/null
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f k8s/values-kube-prometheus-stack.yaml --wait --timeout 10m
helm upgrade --install loki grafana/loki -n monitoring -f k8s/values-loki.yaml --wait --timeout 10m
helm upgrade --install promtail grafana/promtail -n monitoring \
  --set "config.clients[0].url=http://loki-gateway.monitoring.svc.cluster.local/loki/api/v1/push" --wait

echo ">> SLO rules -> PrometheusRule (single source of truth: prometheus/rules/slo.yml)"
python3 - <<'EOF' | kubectl apply -f -
import yaml
groups = yaml.safe_load(open("prometheus/rules/slo.yml"))["groups"]
groups += yaml.safe_load(open("k8s/k8s-rules.yml"))["groups"]
# in-cluster the scrape job is named after the Service
for g in groups:
    for r in g["rules"]:
        r["expr"] = r["expr"].replace('job="orders-service"', 'job="orders-service", namespace="shop"')
print(yaml.safe_dump({
    "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
    "metadata": {"name": "orders-slo", "namespace": "shop", "labels": {"release": "monitoring"}},
    "spec": {"groups": groups},
}, sort_keys=False))
EOF

echo ">> dashboard"
kubectl -n monitoring create configmap orders-slo-dashboard \
  --from-file=grafana/dashboards/orders-slo.json --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply -f -

echo ">> app"
kubectl apply -f k8s/app.yaml
kubectl -n shop rollout status deploy/orders-service --timeout=180s

cat <<MSG

Ready.
  orders-service  http://localhost:8000/docs
  Grafana         http://localhost:3000  (admin / admin) -> dashboard "orders-service - RED & SLO"
  Prometheus      http://localhost:9090

Run chaos experiments:
  python chaos/chaos.py pod-kill   --url http://localhost:8000
  python chaos/chaos.py node-drain --url http://localhost:8000
MSG
