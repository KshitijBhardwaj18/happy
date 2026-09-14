from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

import tools.k8s_read as k8s_read
import tools.k8s_write as k8s_write


# ---------------------------------------------------------------------------
# Fake Kubernetes object builders (SimpleNamespace mirrors the attribute shape
# of the real kubernetes client models closely enough for our code to read).
# ---------------------------------------------------------------------------


def make_container_status(ready=True, restart_count=0, waiting_reason=None, terminated_reason=None):
    return SimpleNamespace(
        ready=ready,
        restart_count=restart_count,
        state=SimpleNamespace(waiting=SimpleNamespace(reason=waiting_reason) if waiting_reason else None),
        last_state=SimpleNamespace(
            terminated=SimpleNamespace(reason=terminated_reason) if terminated_reason else None
        ),
    )


def make_pod(name, phase="Running", labels=None, container_statuses=None, start_time=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels or {}),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=container_statuses if container_statuses is not None else [],
            start_time=start_time,
        ),
    )


def make_deployment(name, replicas=2, ready_replicas=2, match_labels=None, annotations=None, uid="uid-1", containers=None):
    containers = containers or [SimpleNamespace(name="app", image="python:3.12-slim", env=[], resources=None)]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, uid=uid, annotations=annotations or {}),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels=match_labels or {"app": name}),
            template=SimpleNamespace(spec=SimpleNamespace(containers=containers)),
        ),
        status=SimpleNamespace(ready_replicas=ready_replicas, conditions=[]),
    )


def make_replica_set(name, revision, image, owner_uid="uid-1", owner_name="checkout", replicas=2, change_cause=None):
    annotations = {"deployment.kubernetes.io/revision": str(revision)}
    if change_cause:
        annotations["kubernetes.io/change-cause"] = change_cause
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            annotations=annotations,
            owner_references=[SimpleNamespace(uid=owner_uid, name=owner_name)],
        ),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(name="app", image=image)])),
        ),
    )


@pytest.fixture
def fake_clients(monkeypatch):
    core = MagicMock(name="CoreV1Api")
    apps = MagicMock(name="AppsV1Api")
    monkeypatch.setattr(k8s_read, "_clients", lambda: (core, apps))
    monkeypatch.setattr(k8s_write, "_clients", lambda: (core, apps))
    monkeypatch.setattr(k8s_read, "load_settings", lambda: SimpleNamespace(watch_namespaces=["shop"]))
    return core, apps


# ---------------------------------------------------------------------------
# cluster_health
# ---------------------------------------------------------------------------


class TestClusterHealth:
    def test_all_healthy(self, fake_clients):
        core, apps = fake_clients
        dep = make_deployment("frontend", replicas=2, ready_replicas=2)
        apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[dep])
        pods = [
            make_pod("frontend-abc", labels={"app": "frontend"}, container_statuses=[make_container_status()]),
            make_pod("frontend-def", labels={"app": "frontend"}, container_statuses=[make_container_status()]),
        ]
        core.list_namespaced_pod.return_value = SimpleNamespace(items=pods)

        result = k8s_read.cluster_health()

        assert result["healthy"] is True
        assert result["deployments"] == [
            {
                "name": "frontend",
                "namespace": "shop",
                "ready": 2,
                "desired": 2,
                "restarts": 0,
                "waiting_reasons": [],
                "healthy": True,
            }
        ]

    def test_unhealthy_deployment_marks_overall_unhealthy(self, fake_clients):
        core, apps = fake_clients
        dep = make_deployment("checkout", replicas=2, ready_replicas=1)
        apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[dep])
        pods = [
            make_pod(
                "checkout-abc",
                labels={"app": "checkout"},
                container_statuses=[make_container_status(ready=False, restart_count=5, waiting_reason="CrashLoopBackOff")],
            ),
        ]
        core.list_namespaced_pod.return_value = SimpleNamespace(items=pods)

        result = k8s_read.cluster_health()

        assert result["healthy"] is False
        [entry] = result["deployments"]
        assert entry["healthy"] is False
        assert entry["restarts"] == 5
        assert entry["waiting_reasons"] == ["CrashLoopBackOff"]


# ---------------------------------------------------------------------------
# list_unhealthy_pods
# ---------------------------------------------------------------------------


class TestListUnhealthyPods:
    def test_healthy_pods_are_excluded(self, fake_clients):
        core, _ = fake_clients
        core.list_namespaced_pod.return_value = SimpleNamespace(
            items=[make_pod("frontend-abc", container_statuses=[make_container_status(ready=True)])]
        )

        result = k8s_read.list_unhealthy_pods(namespace="shop")

        assert result == []

    def test_crashloopbackoff_reason_from_waiting_state(self, fake_clients):
        core, _ = fake_clients
        core.list_namespaced_pod.return_value = SimpleNamespace(
            items=[
                make_pod(
                    "inventory-xyz",
                    phase="Running",
                    container_statuses=[
                        make_container_status(ready=False, restart_count=3, waiting_reason="CrashLoopBackOff")
                    ],
                )
            ]
        )

        [pod] = k8s_read.list_unhealthy_pods(namespace="shop")

        assert pod["pod"] == "inventory-xyz"
        assert pod["reason"] == "CrashLoopBackOff"
        assert pod["restart_count"] == 3

    def test_oomkilled_reason_extracted_from_last_state(self, fake_clients):
        core, _ = fake_clients
        core.list_namespaced_pod.return_value = SimpleNamespace(
            items=[
                make_pod(
                    "checkout-oom",
                    phase="Running",
                    container_statuses=[
                        make_container_status(ready=False, restart_count=1, terminated_reason="OOMKilled")
                    ],
                )
            ]
        )

        [pod] = k8s_read.list_unhealthy_pods(namespace="shop")

        assert pod["pod"] == "checkout-oom"
        assert pod["reason"] == "OOMKilled"

    def test_pending_pod_without_container_statuses(self, fake_clients):
        core, _ = fake_clients
        core.list_namespaced_pod.return_value = SimpleNamespace(
            items=[make_pod("frontend-new", phase="Pending", container_statuses=[])]
        )

        [pod] = k8s_read.list_unhealthy_pods(namespace="shop")

        assert pod["reason"] == "Pending"


# ---------------------------------------------------------------------------
# get_pod_logs
# ---------------------------------------------------------------------------


class TestGetPodLogs:
    def test_success_splits_lines(self, fake_clients):
        core, _ = fake_clients
        core.read_namespaced_pod_log.return_value = "line one\nline two\n"

        result = k8s_read.get_pod_logs(pod="checkout-abc", namespace="shop")

        assert result == {"ok": True, "lines": ["line one", "line two"]}

    def test_previous_flag_is_passed_through(self, fake_clients):
        core, _ = fake_clients
        core.read_namespaced_pod_log.return_value = "crash log\n"

        k8s_read.get_pod_logs(pod="checkout-abc", namespace="shop", previous=True, tail=50)

        _, kwargs = core.read_namespaced_pod_log.call_args
        assert kwargs["previous"] is True
        assert kwargs["tail_lines"] == 50

    def test_api_error_handled_gracefully(self, fake_clients):
        core, _ = fake_clients
        core.read_namespaced_pod_log.side_effect = ApiException(status=400, reason="previous terminated container not found")

        result = k8s_read.get_pod_logs(pod="checkout-abc", namespace="shop", previous=True)

        assert result["ok"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# get_events
# ---------------------------------------------------------------------------


class TestGetEvents:
    def test_filters_and_sorts_newest_first(self, fake_clients):
        core, _ = fake_clients
        now = datetime.now(timezone.utc)
        old_event = SimpleNamespace(
            type="Warning",
            reason="BackOff",
            message="old event",
            involved_object=SimpleNamespace(name="checkout-abc"),
            last_timestamp=now - timedelta(hours=2),
            event_time=None,
            first_timestamp=None,
        )
        recent_event = SimpleNamespace(
            type="Warning",
            reason="Unhealthy",
            message="recent event",
            involved_object=SimpleNamespace(name="checkout-abc"),
            last_timestamp=now - timedelta(minutes=5),
            event_time=None,
            first_timestamp=None,
        )
        core.list_namespaced_event.return_value = SimpleNamespace(items=[old_event, recent_event])

        result = k8s_read.get_events(namespace="shop", since_minutes=30)

        assert len(result) == 1
        assert result[0]["message"] == "recent event"


# ---------------------------------------------------------------------------
# describe_deployment
# ---------------------------------------------------------------------------


class TestDescribeDeployment:
    def test_returns_image_env_and_revision(self, fake_clients):
        _, apps = fake_clients
        containers = [
            SimpleNamespace(
                name="app",
                image="python:3.12-slim",
                env=[SimpleNamespace(name="ERROR_RATE", value="0.5")],
                resources=SimpleNamespace(limits={"memory": "256Mi"}, requests={"memory": "64Mi"}),
            )
        ]
        dep = make_deployment("checkout", annotations={"deployment.kubernetes.io/revision": "3"}, containers=containers)
        apps.read_namespaced_deployment.return_value = dep

        result = k8s_read.describe_deployment(name="checkout", namespace="shop")

        assert result["image"] == "python:3.12-slim"
        assert result["env"] == {"ERROR_RATE": "0.5"}
        assert result["limits"] == {"memory": "256Mi"}
        assert result["revision"] == 3


# ---------------------------------------------------------------------------
# rollout_history
# ---------------------------------------------------------------------------


class TestRolloutHistory:
    def test_orders_revisions_ascending(self, fake_clients):
        _, apps = fake_clients
        dep = make_deployment("checkout", uid="dep-uid")
        apps.read_namespaced_deployment.return_value = dep
        rs_list = [
            make_replica_set("checkout-rs3", revision=3, image="myapp:v3", owner_uid="dep-uid"),
            make_replica_set("checkout-rs1", revision=1, image="myapp:v1", owner_uid="dep-uid"),
            make_replica_set("checkout-rs2", revision=2, image="myapp:v2", owner_uid="dep-uid"),
            make_replica_set("other-rs", revision=1, image="other:v1", owner_uid="other-uid", owner_name="other"),
        ]
        apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=rs_list)

        result = k8s_read.rollout_history(name="checkout", namespace="shop")

        assert [entry["revision"] for entry in result] == [1, 2, 3]
        assert [entry["image"] for entry in result] == ["myapp:v1", "myapp:v2", "myapp:v3"]


# ---------------------------------------------------------------------------
# k8s_write
# ---------------------------------------------------------------------------


class TestRolloutRestart:
    def test_patches_restart_annotation(self, fake_clients):
        _, apps = fake_clients

        result = k8s_write.rollout_restart(name="inventory", namespace="shop")

        assert result["ok"] is True
        _, kwargs = apps.patch_namespaced_deployment.call_args
        annotations = kwargs["body"]["spec"]["template"]["metadata"]["annotations"]
        assert "kubectl.kubernetes.io/restartedAt" in annotations

    def test_api_error_returns_not_ok(self, fake_clients):
        _, apps = fake_clients
        apps.patch_namespaced_deployment.side_effect = ApiException(status=404, reason="not found")

        result = k8s_write.rollout_restart(name="missing", namespace="shop")

        assert result == {"ok": False, "error": "404: not found"}


class TestScaleDeployment:
    def test_patches_replicas(self, fake_clients):
        _, apps = fake_clients

        result = k8s_write.scale_deployment(name="frontend", namespace="shop", replicas=3)

        assert result["ok"] is True
        _, kwargs = apps.patch_namespaced_deployment.call_args
        assert kwargs["body"]["spec"]["replicas"] == 3


class TestRollbackDeployment:
    def _setup(self, apps, current_revision=3):
        dep = make_deployment(
            "checkout", uid="dep-uid", annotations={"deployment.kubernetes.io/revision": str(current_revision)}
        )
        apps.read_namespaced_deployment.return_value = dep
        rs_list = [
            make_replica_set("checkout-rs1", revision=1, image="myapp:v1", owner_uid="dep-uid"),
            make_replica_set("checkout-rs2", revision=2, image="myapp:v2", owner_uid="dep-uid"),
            make_replica_set("checkout-rs3", revision=3, image="myapp:v3", owner_uid="dep-uid"),
        ]
        apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=rs_list)
        return dep

    def test_no_revision_picks_previous_relative_to_current(self, fake_clients):
        _, apps = fake_clients
        self._setup(apps, current_revision=3)

        result = k8s_write.rollback_deployment(name="checkout", namespace="shop")

        assert result == {"ok": True, "name": "checkout", "namespace": "shop", "rolled_back_to_revision": 2}
        _, kwargs = apps.patch_namespaced_deployment.call_args
        patched_template = kwargs["body"]["spec"]["template"]
        assert patched_template.spec.containers[0].image == "myapp:v2"

    def test_explicit_revision_is_honored(self, fake_clients):
        _, apps = fake_clients
        self._setup(apps, current_revision=3)

        result = k8s_write.rollback_deployment(name="checkout", namespace="shop", revision=1)

        assert result["ok"] is True
        assert result["rolled_back_to_revision"] == 1
        _, kwargs = apps.patch_namespaced_deployment.call_args
        assert kwargs["body"]["spec"]["template"].spec.containers[0].image == "myapp:v1"

    def test_unknown_revision_is_an_error(self, fake_clients):
        _, apps = fake_clients
        self._setup(apps, current_revision=3)

        result = k8s_write.rollback_deployment(name="checkout", namespace="shop", revision=99)

        assert result["ok"] is False
        assert "99" in result["error"]

    def test_no_earlier_revision_is_an_error(self, fake_clients):
        _, apps = fake_clients
        self._setup(apps, current_revision=1)
        apps.list_namespaced_replica_set.return_value = SimpleNamespace(
            items=[make_replica_set("checkout-rs1", revision=1, image="myapp:v1", owner_uid="dep-uid")]
        )

        result = k8s_write.rollback_deployment(name="checkout", namespace="shop")

        assert result["ok"] is False


class TestSetImage:
    def test_patches_first_container_image(self, fake_clients):
        _, apps = fake_clients
        apps.read_namespaced_deployment.return_value = make_deployment("frontend")

        result = k8s_write.set_image(name="frontend", namespace="shop", image="python:3.12-slim-fixed")

        assert result["ok"] is True
        _, kwargs = apps.patch_namespaced_deployment.call_args
        containers = kwargs["body"]["spec"]["template"]["spec"]["containers"]
        assert containers == [{"name": "app", "image": "python:3.12-slim-fixed"}]


class TestDeleteNamespace:
    def test_success(self, fake_clients):
        core, _ = fake_clients

        result = k8s_write.delete_namespace("shop")

        assert result == {"ok": True, "namespace": "shop"}
        core.delete_namespace.assert_called_once_with(name="shop")

    def test_api_error_returns_not_ok(self, fake_clients):
        core, _ = fake_clients
        core.delete_namespace.side_effect = ApiException(status=403, reason="forbidden")

        result = k8s_write.delete_namespace("shop")

        assert result == {"ok": False, "error": "403: forbidden"}
