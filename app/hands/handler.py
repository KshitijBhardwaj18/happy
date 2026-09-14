"""Happy's hands: the only code allowed to change the cluster.

Runs as a Lambda behind an AgentCore Gateway so that every write is an MCP tool call that
AgentCore Policy can permit or forbid before it reaches Kubernetes. Deliberately stdlib-only
(plus boto3, which Lambda provides): it reads a kubeconfig JSON from SSM Parameter Store and
talks to the Kubernetes API over HTTPS with the client certificate from that kubeconfig.

Tool name arrives in context.client_context.custom["bedrockAgentCoreToolName"] as
"<Target>___<tool>"; the event is the tool's input object.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import ssl
import tempfile
import urllib.error
import urllib.request

import boto3

SSM_PARAM = os.environ.get("HAPPY_KUBECONFIG_PARAM", "/happy/kubeconfig")
_CTX: dict | None = None


def _kubeconfig() -> dict:
    global _CTX
    if _CTX:
        return _CTX
    raw = boto3.client("ssm").get_parameter(Name=SSM_PARAM, WithDecryption=True)["Parameter"]["Value"]
    cfg = json.loads(raw)
    cluster = cfg["clusters"][0]["cluster"]
    user = cfg["users"][0]["user"]
    tmp = tempfile.mkdtemp()

    def _write(name: str, b64: str) -> str:
        path = os.path.join(tmp, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path

    ctx = ssl.create_default_context(cafile=_write("ca.crt", cluster["certificate-authority-data"]))
    ctx.load_cert_chain(_write("client.crt", user["client-certificate-data"]), _write("client.key", user["client-key-data"]))
    _CTX = {"server": cluster["server"].rstrip("/"), "ssl": ctx}
    return _CTX


def _k8s(method: str, path: str, body: dict | None = None, content_type: str = "application/strategic-merge-patch+json") -> dict:
    kc = _kubeconfig()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(kc["server"] + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, context=kc["ssl"], timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from e


def _deploy_path(ns: str, name: str, suffix: str = "") -> str:
    return f"/apis/apps/v1/namespaces/{ns}/deployments/{name}{suffix}"


def rollout_restart(name: str, namespace: str = "shop") -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _k8s("PATCH", _deploy_path(namespace, name), {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}})
    return {"ok": True, "action": "rollout_restart", "deployment": name, "namespace": namespace, "restartedAt": now}


def scale_deployment(name: str, replicas: int, namespace: str = "shop") -> dict:
    _k8s("PATCH", _deploy_path(namespace, name, "/scale"), {"spec": {"replicas": int(replicas)}}, "application/merge-patch+json")
    return {"ok": True, "action": "scale_deployment", "deployment": name, "namespace": namespace, "replicas": int(replicas)}


def _replicasets(ns: str, name: str) -> list[dict]:
    items = _k8s("GET", f"/apis/apps/v1/namespaces/{ns}/replicasets")["items"]
    owned = [rs for rs in items if any(o.get("kind") == "Deployment" and o.get("name") == name for o in rs["metadata"].get("ownerReferences", []))]
    return sorted(owned, key=lambda rs: int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0")))


def rollback_deployment(name: str, namespace: str = "shop", revision: int | None = None) -> dict:
    rss = _replicasets(namespace, name)
    if len(rss) < 2 and revision is None:
        return {"ok": False, "error": f"{name} has no previous revision to roll back to"}
    if revision is None:
        target = rss[-2]
    else:
        matches = [rs for rs in rss if int(rs["metadata"]["annotations"].get("deployment.kubernetes.io/revision", "0")) == int(revision)]
        if not matches:
            return {"ok": False, "error": f"revision {revision} not found for {name}"}
        target = matches[0]
    tpl = target["spec"]["template"]
    tpl.setdefault("metadata", {}).setdefault("labels", {}).pop("pod-template-hash", None)
    _k8s("PATCH", _deploy_path(namespace, name), {"spec": {"template": tpl}}, "application/merge-patch+json")
    rev = target["metadata"]["annotations"].get("deployment.kubernetes.io/revision")
    image = tpl["spec"]["containers"][0].get("image")
    return {"ok": True, "action": "rollback_deployment", "deployment": name, "namespace": namespace, "rolled_back_to_revision": rev, "image": image}


def set_image(name: str, image: str, namespace: str = "shop") -> dict:
    dep = _k8s("GET", _deploy_path(namespace, name))
    container = dep["spec"]["template"]["spec"]["containers"][0]["name"]
    _k8s("PATCH", _deploy_path(namespace, name), {"spec": {"template": {"spec": {"containers": [{"name": container, "image": image}]}}}})
    return {"ok": True, "action": "set_image", "deployment": name, "namespace": namespace, "image": image}


def delete_namespace(namespace: str) -> dict:
    _k8s("DELETE", f"/api/v1/namespaces/{namespace}")
    return {"ok": True, "action": "delete_namespace", "namespace": namespace}


TOOLS = {
    "rollout_restart": rollout_restart,
    "scale_deployment": scale_deployment,
    "rollback_deployment": rollback_deployment,
    "set_image": set_image,
    "delete_namespace": delete_namespace,
}


def handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName", "") or (event or {}).get("tool", "")
    tool = full.split("___")[-1]
    fn = TOOLS.get(tool)
    if fn is None:
        return {"ok": False, "error": f"unknown tool {full!r}", "known": sorted(TOOLS)}
    args = {k: v for k, v in (event or {}).items() if k != "tool"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"ok": False, "error": f"bad arguments for {tool}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:500]}
