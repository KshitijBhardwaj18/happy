"""Local CLI for Happy: `uv run happy {patrol,digest,handoff,investigate} [--session ID] [--service NAME]`.

Loads `.env` from the repo root (this file lives at `<repo>/app/happy/cli.py`) before
touching `config.Settings`, so `KUBECONFIG_B64`, Slack, and GitHub credentials are
available the same way whether Happy runs locally or as an entry point script.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")


def _run_investigate(service: str | None) -> dict:
    if not service:
        return {"error": "--service is required for investigate mode"}

    from config import load_settings
    from fingerprint import incident_fingerprint
    from runner import investigate_stub
    from tools.k8s_read import get_pod_logs, list_unhealthy_pods

    settings = load_settings()
    namespace = settings.watch_namespaces[0]
    matches = [pod for pod in list_unhealthy_pods(namespace) if pod.get("pod", "").startswith(service)]

    lines: list[str] = []
    if matches:
        current = get_pod_logs(pod=matches[0]["pod"], namespace=namespace)
        if current.get("ok"):
            lines = current.get("lines", [])

    fp = incident_fingerprint(service=service, namespace=namespace, reason="manual", log_lines=lines)
    report = investigate_stub(service, namespace, fp)
    return report.model_dump()


def main() -> None:
    parser = argparse.ArgumentParser(prog="happy", description="Happy: the on-call agent's local CLI.")
    parser.add_argument("mode", choices=["patrol", "digest", "handoff", "investigate"])
    parser.add_argument("--session", dest="session_id", default=None, help="Session id to use/resume")
    parser.add_argument("--service", dest="service", default=None, help="Service name (investigate mode)")
    args = parser.parse_args()

    from runner import run_digest, run_handoff, run_patrol

    if args.mode == "patrol":
        result = run_patrol(session_id=args.session_id)
    elif args.mode == "digest":
        result = run_digest()
    elif args.mode == "handoff":
        result = run_handoff()
    else:
        result = _run_investigate(args.service)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
