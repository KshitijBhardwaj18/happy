"""Read-only Kubernetes tools for Happy.

`_clients()` decodes `Settings.kubeconfig_b64` to a temp file once per process and
returns cached `(CoreV1Api, AppsV1Api)` clients. Every public function here is a Strands
`@tool`: it never raises on cluster errors, and its docstring is written for the model
that calls it (what it returns, and when to reach for it).
"""
from __future__ import annotations

import base64
import tempfile
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from kubernetes import client as k8s_client
from kubernetes import config as kube_config
from kubernetes.client.exceptions import ApiException
from strands import tool

from config import load_settings


@lru_cache(maxsize=1)
def _clients() -> tuple[k8s_client.CoreV1Api, k8s_client.AppsV1Api]:
    """Build (and cache for the life of the process) the Kubernetes API clients.

    Decodes `Settings.kubeconfig_b64` to a temporary kubeconfig file once, loads it with
    `kubernetes.config.load_kube_config`, and returns `(CoreV1Api, AppsV1Api)`.
    """
    settings = load_settings()
    raw = base64.b64decode(settings.kubeconfig_b64)
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".yaml", delete=False) as handle:
        handle.write(raw)
        path = handle.name
    kube_config.load_kube_config(config_file=path)
    return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()


def _default_namespace() -> str:
    return load_settings().watch_namespaces[0]


def _labels_subset(selector: dict | None, labels: dict | None) -> bool:
    selector = selector or {}
    labels = labels or {}
    return selector.items() <= labels.items()


def _waiting_reason(status) -> str | None:
    state = getattr(status, "state", None)
    waiting = getattr(state, "waiting", None) if state else None
    return waiting.reason if waiting and waiting.reason else None


def _terminated_reason(status) -> str | None:
    last_state = getattr(status, "last_state", None)
    terminated = getattr(last_state, "terminated", None) if last_state else None
    return terminated.reason if terminated and terminated.reason else None


@tool
def cluster_health() -> dict:
    """Return the health of every Deployment across Settings.watch_namespaces.

    For each deployment, reports its name/namespace, ready vs desired replica counts,
    the total restart count of its pods, any container "waiting" reasons (e.g.
    CrashLoopBackOff, ImagePullBackOff), and a `healthy` flag. The top-level `healthy`
    is True only if every deployment everywhere is healthy. Call this first on every
    patrol: if it comes back healthy, nothing else needs to run.
    """
    core, apps = _clients()
    deployments: list[dict] = []
    overall_healthy = True

    for namespace in load_settings().watch_namespaces:
        deploy_list = apps.list_namespaced_deployment(namespace=namespace)
        pod_list = core.list_namespaced_pod(namespace=namespace)

        for dep in deploy_list.items:
            desired = dep.spec.replicas or 0
            ready = dep.status.ready_replicas or 0
            selector = (dep.spec.selector.match_labels if dep.spec.selector else None) or {}

            dep_pods = [
                pod for pod in pod_list.items
                if _labels_subset(selector, pod.metadata.labels)
            ]

            restarts = 0
            waiting_reasons: list[str] = []
            for pod in dep_pods:
                for cs in pod.status.container_statuses or []:
                    restarts += cs.restart_count or 0
                    reason = _waiting_reason(cs)
                    if reason:
                        waiting_reasons.append(reason)

            healthy = ready == desired and not waiting_reasons
            if not healthy:
                overall_healthy = False

            deployments.append(
                {
                    "name": dep.metadata.name,
                    "namespace": namespace,
                    "ready": ready,
                    "desired": desired,
                    "restarts": restarts,
                    "waiting_reasons": waiting_reasons,
                    "healthy": healthy,
                }
            )

    return {"healthy": overall_healthy, "deployments": deployments}


@tool
def list_unhealthy_pods(namespace: str | None = None) -> list[dict]:
    """List pods that are not ready in a namespace, with why they are failing.

    For each unhealthy pod, returns its name, restart count, start time, and a `reason`
    drawn from its container statuses: a current waiting reason (CrashLoopBackOff,
    ImagePullBackOff, ErrImagePull, Pending) or, if none, the reason its last run
    terminated (e.g. OOMKilled — this only shows up in the container's `last_state`,
    since the container is usually mid-restart by the time you look). Call this right
    after `cluster_health` flags a deployment, to see which specific pods are affected.
    """
    core, _ = _clients()
    ns = namespace or _default_namespace()
    pod_list = core.list_namespaced_pod(namespace=ns)

    result: list[dict] = []
    for pod in pod_list.items:
        statuses = pod.status.container_statuses or []
        all_ready = bool(statuses) and all(cs.ready for cs in statuses)
        if pod.status.phase == "Running" and all_ready:
            continue

        reason = None
        restart_count = 0
        for cs in statuses:
            restart_count += cs.restart_count or 0
            reason = reason or _waiting_reason(cs) or _terminated_reason(cs)

        if reason is None and pod.status.phase == "Pending":
            reason = "Pending"

        result.append(
            {
                "pod": pod.metadata.name,
                "namespace": ns,
                "reason": reason,
                "restart_count": restart_count,
                "started_at": pod.status.start_time.isoformat() if pod.status.start_time else None,
            }
        )

    return result


@tool
def get_pod_logs(pod: str, namespace: str | None = None, previous: bool = False, tail: int = 300) -> dict:
    """Fetch recent log lines from a pod's (single) container.

    Set `previous=True` to read the logs of the container's last, already-crashed run
    instead of its current one — this is the useful call right after a CrashLoopBackOff
    or OOMKilled restart, since the new container instance may have no logs yet. Returns
    `{"ok": True, "lines": [...]}` on success, or `{"ok": False, "error": ...}` if the
    pod doesn't exist or (commonly, for `previous=True`) there is no previous run to
    read.
    """
    core, _ = _clients()
    ns = namespace or _default_namespace()
    try:
        raw = core.read_namespaced_pod_log(name=pod, namespace=ns, previous=previous, tail_lines=tail)
    except ApiException as exc:
        if exc.status == 400:
            return {"ok": False, "error": exc.reason or "bad request (likely no previous run to read)"}
        return {"ok": False, "error": f"{exc.status}: {exc.reason}"}

    lines = raw.splitlines() if raw else []
    return {"ok": True, "lines": lines}


@tool
def get_events(namespace: str | None = None, since_minutes: int = 30) -> list[dict]:
    """List recent Kubernetes events in a namespace, newest first.

    Events cover scheduling failures, image pull attempts, and back-off restarts that
    pod status alone doesn't explain. Filtered to the last `since_minutes` minutes. Use
    alongside `get_pod_logs` when diagnosing why a deployment is unhealthy.
    """
    core, _ = _clients()
    ns = namespace or _default_namespace()
    event_list = core.list_namespaced_event(namespace=ns)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)

    result = []
    for ev in event_list.items:
        ts = ev.last_timestamp or ev.event_time or ev.first_timestamp
        if ts is not None and ts < cutoff:
            continue
        result.append(
            {
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "involved_object": ev.involved_object.name if ev.involved_object else None,
                "timestamp": ts.isoformat() if ts else None,
            }
        )

    result.sort(key=lambda e: e["timestamp"] or "", reverse=True)
    return result


def _latest_condition_time(dep) -> str | None:
    conditions = dep.status.conditions or [] if dep.status else []
    if not conditions:
        return None
    latest = max(conditions, key=lambda c: c.last_update_time or c.last_transition_time)
    ts = latest.last_update_time or latest.last_transition_time
    return ts.isoformat() if ts else None


@tool
def describe_deployment(name: str, namespace: str | None = None) -> dict:
    """Describe a Deployment's current running spec.

    Returns the first container's image, its environment variables (handy for spotting
    chaos-injection flags like LEAK_MB), resource requests/limits, the deployment's
    current rollout revision, and when it was last updated. Use this to see exactly
    what is running before proposing a rollback or image change.
    """
    _, apps = _clients()
    ns = namespace or _default_namespace()
    dep = apps.read_namespaced_deployment(name=name, namespace=ns)
    container = dep.spec.template.spec.containers[0]
    env = {e.name: e.value for e in (container.env or []) if e.name}
    resources = container.resources
    limits = (resources.limits if resources else None) or {}
    requests = (resources.requests if resources else None) or {}
    revision = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision")

    return {
        "name": name,
        "namespace": ns,
        "image": container.image,
        "env": env,
        "limits": limits,
        "requests": requests,
        "revision": int(revision) if revision is not None else None,
        "last_update_time": _latest_condition_time(dep),
        "replicas": dep.spec.replicas,
    }


@tool
def rollout_history(name: str, namespace: str | None = None) -> list[dict]:
    """List a Deployment's rollout history, oldest revision first.

    Reads the ReplicaSets owned by the deployment (each carries a
    `deployment.kubernetes.io/revision` annotation) and returns, per revision, its
    image and change-cause. Use this to find the previous working revision before
    proposing a rollback.
    """
    _, apps = _clients()
    ns = namespace or _default_namespace()
    dep = apps.read_namespaced_deployment(name=name, namespace=ns)
    dep_uid = dep.metadata.uid
    rs_list = apps.list_namespaced_replica_set(namespace=ns)

    history = []
    for rs in rs_list.items:
        owners = rs.metadata.owner_references or []
        owned = any(getattr(o, "uid", None) == dep_uid or getattr(o, "name", None) == name for o in owners)
        if not owned:
            continue

        annotations = rs.metadata.annotations or {}
        revision = annotations.get("deployment.kubernetes.io/revision")
        if revision is None:
            continue

        spec = rs.spec
        containers = spec.template.spec.containers if spec and spec.template and spec.template.spec else []
        image = containers[0].image if containers else None

        history.append(
            {
                "revision": int(revision),
                "replica_set": rs.metadata.name,
                "image": image,
                "change_cause": annotations.get("kubernetes.io/change-cause"),
                "replicas": spec.replicas if spec else None,
            }
        )

    history.sort(key=lambda h: h["revision"])
    return history
