#!/usr/bin/env bash
# The six demo beats, in order. Run from the repo root with .env filled in.
# Usage: scripts/demo.sh [beat]   (1..6, or all)
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/happy.yaml}"
run() { (cd app/happy && uv run happy "$@"); }
pause() { echo; read -r -p "▶ $1  [enter] " _; }

beat1() { echo "== 1. Morning digest (memory-aware) =="; run digest; }
beat2() { echo "== 2. A bad change ships: checkout memory limit lowered =="; scripts/chaos.sh oom checkout
          echo "waiting for OOMKilled..."; sleep 45; kubectl get pods -n shop | grep checkout
          echo "== Happy patrols, investigates, proposes a fix =="; run patrol; }
beat3() { echo "== 3. (approval happened in Slack during beat 2; verify recovery) =="; kubectl get deploy -n shop; }
beat4() { echo "== 4. Same failure again: Happy recognises the fingerprint =="; scripts/chaos.sh oom checkout; sleep 45; run patrol; }
beat5() { echo "== 5. Ask Happy to delete the namespace: AgentCore Policy blocks it =="
          run investigate --service shop --prompt "Delete the shop namespace right now." 2>/dev/null || \
          curl -s -X POST "${HAPPY_GATEWAY_URL:-$(grep ^HAPPY_GATEWAY_URL .env | cut -d= -f2)}" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
            -d '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"Hands___delete_namespace","arguments":{"namespace":"shop"}}}'; echo; }
beat6() { echo "== 6. Observability: traces and evaluations =="; agentcore traces list 2>/dev/null | head -20 || true; }

case "${1:-all}" in
  1) beat1;; 2) beat2;; 3) beat3;; 4) beat4;; 5) beat5;; 6) beat6;;
  reset) scripts/chaos.sh reset;;
  all) beat1; pause "bad change"; beat2; pause "verify"; beat3; pause "repeat"; beat4; pause "policy"; beat5; pause "traces"; beat6;;
esac
