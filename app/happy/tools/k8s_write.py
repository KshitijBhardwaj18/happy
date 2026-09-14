"""Cluster write actions for Happy.

These are PLAIN functions, not Strands `@tool`s — a later PR (trust-ladder / hands)
wraps them behind approval and audit logic before exposing them to the model. Every
function here returns `{"ok": True, ...}` or `{"ok": False, "error": ...}` and never
raises for a Kubernetes API error.
"""
from __future__ import annotations

from datetime import datetime, timezone

from kubernetes.client.exceptions import ApiException

from tools.k8s_read import _clients


def rollout_restart(name: str, namespace: str) -> dict:
    """Trigger a rolling restart of a Deployment by patching a restart annotation."""
    _, apps = _clients()
    restarted_at = datetime.now(timezone.utc).isoformat()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": restarted_at,
                    }
                }
            }
        }
    }
    try:
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        return {"ok": False, "error": f"{exc.status}: {exc.reason}"}
    return {"ok": True, "name": name, "namespace": namespace, "restarted_at": restarted_at}


def scale_deployment(name: str, namespace: str, replicas: int) -> dict:
    """Set a Deployment's replica count."""
    _, apps = _clients()
    body = {"spec": {"replicas": replicas}}
    try:
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        return {"ok": False, "error": f"{exc.status}: {exc.reason}"}
    return {"ok": True, "name": name, "namespace": namespace, "replicas": replicas}


def _owned_replica_sets_by_revision(apps, name: str, namespace: str):
    """Return `[(revision, ReplicaSet), ...]` owned by the deployment, plus the deployment itself."""
    dep = apps.read_namespaced_deployment(name=name, namespace=namespace)
    dep_uid = dep.metadata.uid
    rs_list = apps.list_namespaced_replica_set(namespace=namespace)

    owned = []
    for rs in rs_list.items:
        owners = rs.metadata.owner_references or []
        is_owned = any(
            getattr(owner, "uid", None) == dep_uid or getattr(owner, "name", None) == name
            for owner in owners
        )
        if not is_owned:
            continue
        annotations = rs.metadata.annotations or {}
        revision = annotations.get("deployment.kubernetes.io/revision")
        if revision is None:
            continue
        owned.append((int(revision), rs))

    owned.sort(key=lambda pair: pair[0])
    return owned, dep


def rollback_deployment(name: str, namespace: str, revision: int | None = None) -> dict:
    """Roll a Deployment back to a prior revision's pod template.

    Finds the ReplicaSet for `revision` (or, if omitted, the revision immediately
    before the deployment's current one) among the ReplicaSets it owns, and patches the
    deployment's pod template with that ReplicaSet's template.
    """
    _, apps = _clients()
    try:
        owned, dep = _owned_replica_sets_by_revision(apps, name, namespace)
    except ApiException as exc:
        return {"ok": False, "error": f"{exc.status}: {exc.reason}"}

    if not owned:
        return {"ok": False, "error": f"no rollout history found for deployment {name!r}"}

    current_revision_str = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
    current_revision = int(current_revision_str) if current_revision_str is not None else owned[-1][0]

    target_rs = None
    if revision is not None:
        target_rs = next((rs for rev, rs in owned if rev == revision), None)
        if target_rs is None:
            return {"ok": False, "error": f"revision {revision} not found for deployment {name!r}"}
    else:
        earlier = [(rev, rs) for rev, rs in owned if rev < current_revision]
        if not earlier:
            return {"ok": False, "error": "no earlier revision to roll back to"}
        revision, target_rs = max(earlier, key=lambda pair: pair[0])

    template = target_rs.spec.template if target_rs.spec else None
    if template is None:
        return {"ok": False, "error": f"revision {revision} has no pod template"}

    body = {"spec": {"template": template}}
    try:
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        return {"ok": False, "error": f"{exc.status}: {exc.reason}"}

    return {"ok": True, "name": name, "namespace": namespace, "rolled_back_to_revision": revision}


def set_image(name: str, namespace: str, image: str) -> dict:
    """Set the image of a Deployment's first container."""
    _, apps = _clients()
    try:
        dep = apps.read_namespaced_deployment(name=name, namespace=namespace)
        container_name = dep.spec.template.spec.containers[0].name
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": container_name, "image": image},
                        ]
                    }
                }
            }
        }
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        return {"ok": False, "error": f"{exc.status}: {exc.reason}"}
    return {"ok": True, "name": name, "namespace": namespace, "image": image}


def delete_namespace(namespace: str) -> dict:
    """Delete a namespace outright. Forbidden-tier: destructive and irreversible."""
    core, _ = _clients()
    try:
        core.delete_namespace(name=namespace)
    except ApiException as exc:
        return {"ok": False, "error": f"{exc.status}: {exc.reason}"}
    return {"ok": True, "namespace": namespace}
