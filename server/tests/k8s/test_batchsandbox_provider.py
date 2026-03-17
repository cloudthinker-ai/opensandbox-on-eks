# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Unit tests for BatchSandboxProvider.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from kubernetes.client import ApiException

from src.api.schema import ImageSpec, NetworkPolicy, NetworkRule
from src.services.k8s.batchsandbox_provider import BatchSandboxProvider


class TestBatchSandboxProvider:
    """BatchSandboxProvider unit tests"""

    # ===== Initialization Tests =====

    def test_init_without_template_creates_provider(self, mock_k8s_client):
        """
        Test case: Verify normal initialization without template
        """
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)

        assert provider.k8s_client == mock_k8s_client
        assert provider.template_manager._template is None
        assert provider.group == "sandbox.opensandbox.io"
        assert provider.version == "v1alpha1"
        assert provider.plural == "batchsandboxes"

    def test_init_with_template_loads_template(self, mock_k8s_client, tmp_path):
        """
        Test case: Verify correct loading with template
        """
        template_file = tmp_path / "template.yaml"
        template_file.write_text("spec:\n  replicas: 1")

        provider = BatchSandboxProvider(mock_k8s_client, str(template_file))

        assert provider.template_manager._template is not None

    def test_init_sets_crd_constants_correctly(self, mock_k8s_client):
        """
        Test case: Verify CRD constants set correctly
        """
        provider = BatchSandboxProvider(mock_k8s_client)

        assert provider.group == "sandbox.opensandbox.io"
        assert provider.version == "v1alpha1"
        assert provider.plural == "batchsandboxes"

    # ===== Workload Creation Tests =====

    def test_create_workload_builds_correct_manifest(self, mock_k8s_client):
        """
        Test case: Verify created manifest structure is correct
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)

        result = provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={"FOO": "bar"},
            resource_limits={"cpu": "1", "memory": "1Gi"},
            labels={"opensandbox.io/id": "test-id"},
            expires_at=expires_at,
            execd_image="execd:latest"
        )

        assert result == {"name": "test-id", "uid": "test-uid"}

        # Verify API call
        call_args = mock_api.create_namespaced_custom_object.call_args
        body = call_args.kwargs["body"]

        assert body["apiVersion"] == "sandbox.opensandbox.io/v1alpha1"
        assert body["kind"] == "BatchSandbox"
        assert body["metadata"]["name"] == "test-id"
        assert body["metadata"]["namespace"] == "test-ns"
        assert body["spec"]["replicas"] == 1
        assert body["spec"]["expireTime"] == "2025-12-31T10:00:00+00:00"
        assert "template" in body["spec"]
        assert "initContainers" in body["spec"]["template"]["spec"]
        assert "containers" in body["spec"]["template"]["spec"]
        assert "volumes" in body["spec"]["template"]["spec"]

    def test_create_workload_builds_execd_init_container(self, mock_k8s_client):
        """
        Test case: Verify execd init container built correctly
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:test"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        init_container = body["spec"]["template"]["spec"]["initContainers"][0]

        assert init_container["name"] == "execd-installer"
        assert init_container["image"] == "execd:test"
        assert init_container["command"] == ["/bin/sh", "-c"]
        assert "bootstrap.sh" in init_container["args"][0]
        assert init_container["volumeMounts"][0]["name"] == "opensandbox-bin"

    def test_create_workload_execd_init_no_extra_commands(self, mock_k8s_client):
        """
        Test case: execd-installer init container should not include extra
        chown commands or extra volume mounts.
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:test"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        init_containers = body["spec"]["template"]["spec"]["initContainers"]

        assert len(init_containers) == 1
        assert init_containers[0]["name"] == "execd-installer"

        # Should NOT include chown command
        script = init_containers[0]["args"][0]
        assert "chown" not in script

        # Should only have opensandbox-bin mount
        mount_names = [m["name"] for m in init_containers[0]["volumeMounts"]]
        assert mount_names == ["opensandbox-bin"]

        # Should NOT have securityContext (no root needed)
        assert "securityContext" not in init_containers[0]

    def test_create_workload_wraps_entrypoint_with_bootstrap(self, mock_k8s_client):
        """
        Test case: Verify user entrypoint is wrapped with bootstrap
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/usr/bin/python", "app.py"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        main_container = body["spec"]["template"]["spec"]["containers"][0]

        assert main_container["command"] == [
            "/opt/opensandbox/bin/bootstrap.sh",
            "/usr/bin/python",
            "app.py"
        ]

    def test_create_workload_converts_env_to_list(self, mock_k8s_client):
        """
        Test case: Verify environment variable dict converted to list.
        Also verifies EXECD environment variable is automatically injected.
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={"FOO": "bar", "BAZ": "qux"},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        env_vars = body["spec"]["template"]["spec"]["containers"][0]["env"]

        # Should have user env vars plus EXECD and EXECD_ACCESS_TOKEN
        assert len(env_vars) == 4
        env_dict = {e["name"]: e["value"] for e in env_vars}
        assert env_dict["FOO"] == "bar"
        assert env_dict["BAZ"] == "qux"
        # Verify EXECD is automatically injected
        assert env_dict["EXECD"] == "/opt/opensandbox/bin/execd"
        # Verify EXECD_ACCESS_TOKEN is injected
        assert "EXECD_ACCESS_TOKEN" in env_dict

    def test_create_workload_merges_template_volumes_and_mounts(self, mock_k8s_client, tmp_path):
        """
        Test case: Verify template volumes/volumeMounts are merged into runtime manifest
        """
        template_file = tmp_path / "template.yaml"
        template_file.write_text(
            """
spec:
  template:
    spec:
      volumes:
        - name: sandbox-shared-data
          emptyDir: {}
      containers:
        - name: sandbox
          image: ubuntu:latest
          volumeMounts:
            - name: sandbox-shared-data
              mountPath: /data
"""
        )
        provider = BatchSandboxProvider(mock_k8s_client, str(template_file))
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        spec = body["spec"]["template"]["spec"]

        volume_names = [v["name"] for v in spec["volumes"]]
        assert "sandbox-shared-data" in volume_names
        assert "opensandbox-bin" in volume_names

        # Runtime container should stay intact (template image should not override)
        container = spec["containers"][0]
        assert container["name"] == "sandbox"
        assert container["image"] == "python:3.11"

        mount_names = [m["name"] for m in container["volumeMounts"]]
        assert "sandbox-shared-data" in mount_names
        assert "opensandbox-bin" in mount_names

    def test_create_workload_dedupes_template_volume_and_mount_names(self, mock_k8s_client, tmp_path):
        """
        Test case: Verify template entries do not duplicate runtime volumes/volumeMounts
        """
        template_file = tmp_path / "template.yaml"
        template_file.write_text(
            """
spec:
  template:
    spec:
      volumes:
        - name: opensandbox-bin
          emptyDir: {}
        - name: sandbox-shared-data
          emptyDir: {}
      containers:
        - name: sandbox
          volumeMounts:
            - name: opensandbox-bin
              mountPath: /opt/opensandbox/bin
            - name: sandbox-shared-data
              mountPath: /data
"""
        )
        provider = BatchSandboxProvider(mock_k8s_client, str(template_file))
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        spec = body["spec"]["template"]["spec"]

        volume_names = [v["name"] for v in spec["volumes"]]
        assert volume_names.count("opensandbox-bin") == 1
        assert "sandbox-shared-data" in volume_names

        mount_names = [m["name"] for m in spec["containers"][0]["volumeMounts"]]
        assert mount_names.count("opensandbox-bin") == 1
        assert "sandbox-shared-data" in mount_names

    def test_create_workload_no_resources_without_template(self, mock_k8s_client):
        """
        Test case: Without a template, the sandbox container has no resources set.
        Resources come solely from the template, not the API resource_limits parameter.
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={"cpu": "1", "memory": "1Gi"},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        container = body["spec"]["template"]["spec"]["containers"][0]

        # resource_limits from API are ignored; no template means no resources
        assert "resources" not in container

    def test_create_workload_disables_service_account_and_service_links(self, mock_k8s_client):
        """
        Test case: Verify automountServiceAccountToken=False and enableServiceLinks=False
        are set in the pod spec to prevent K8s env var and token leakage.
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]

        assert pod_spec["automountServiceAccountToken"] is False
        assert pod_spec["enableServiceLinks"] is False

    def test_create_workload_applies_template_resources(self, mock_k8s_client, tmp_path):
        """
        Test case: Template resources with separate requests/limits are applied
        to the sandbox container (Burstable QoS).
        """
        template_file = tmp_path / "template.yaml"
        template_file.write_text("""
spec:
  template:
    spec:
      containers:
        - name: sandbox
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
            limits:
              cpu: "1"
              memory: "2Gi"
""")
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=str(template_file)
        )
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test", "uid": "uid"}
        }

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest"
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        container = body["spec"]["template"]["spec"]["containers"][0]

        assert container["resources"]["requests"] == {"cpu": "250m", "memory": "512Mi"}
        assert container["resources"]["limits"] == {"cpu": "1", "memory": "2Gi"}

    # ===== Workload Query Tests =====

    def test_get_workload_finds_existing_sandbox(
        self, mock_k8s_client, mock_batchsandbox_list_response
    ):
        """
        Test case: Verify successfully querying existing sandbox
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.return_value = mock_batchsandbox_list_response["items"][0]

        result = provider.get_workload("test-id", "test-ns")

        assert result is not None
        assert result["metadata"]["name"] == "test-id"

    def test_get_workload_returns_none_when_not_found(self, mock_k8s_client):
        """
        Test case: Verify None returned when not found
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.side_effect = [
            ApiException(status=404),
            ApiException(status=404),
        ]

        result = provider.get_workload("test-id", "test-ns")

        assert result is None

    def test_get_workload_falls_back_to_legacy_name(self, mock_k8s_client):
        """
        Test case: Verify legacy sandbox-<id> name is used when primary lookup 404s
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.side_effect = [
            ApiException(status=404),
            {"metadata": {"name": "sandbox-test-id"}},
        ]

        result = provider.get_workload("test-id", "test-ns")

        assert result["metadata"]["name"] == "sandbox-test-id"
        assert mock_api.get_namespaced_custom_object.call_args_list[0].kwargs["name"] == "test-id"
        assert mock_api.get_namespaced_custom_object.call_args_list[1].kwargs["name"] == "sandbox-test-id"

    def test_get_workload_handles_404_gracefully(self, mock_k8s_client):
        """
        Test case: Verify None returned on 404 exception
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()

        # Mock 404 exception
        error = ApiException(status=404)
        mock_api.get_namespaced_custom_object.side_effect = [error, error]

        result = provider.get_workload("test-id", "test-ns")

        assert result is None

    def test_get_workload_reraises_non_404_exceptions(self, mock_k8s_client):
        """
        Test case: Verify non-404 exceptions are re-raised
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()

        # Mock 500 exception
        error = ApiException(status=500)
        mock_api.get_namespaced_custom_object.side_effect = error

        with pytest.raises(ApiException) as exc_info:
            provider.get_workload("test-id", "test-ns")

        assert exc_info.value.status == 500

    def test_get_workload_prefers_informer_cache(self, mock_k8s_client, monkeypatch):
        """
        Test case: Use informer cache when synced to avoid direct API call
        """
        cached = {"metadata": {"name": "test-id"}}

        class FakeInformer:
            def __init__(self):
                self.started = False
                self.has_synced = True

            def start(self):
                self.started = True

            def get(self, name):
                return cached if name == "test-id" else None

            def update_cache(self, obj):
                self.updated = obj

        fake_informer = FakeInformer()
        provider = BatchSandboxProvider(
            mock_k8s_client,
            enable_informer=True,
            informer_factory=lambda ns: fake_informer,
        )

        result = provider.get_workload("test-id", "test-ns")

        assert result == cached
        assert fake_informer.started is True
        mock_k8s_client.get_custom_objects_api().get_namespaced_custom_object.assert_not_called()

    def test_get_workload_logs_unexpected_errors(self, mock_k8s_client):
        """
        Test case: Verify unexpected errors are re-raised
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.side_effect = RuntimeError("Unexpected")

        with pytest.raises(RuntimeError, match="Unexpected"):
            provider.get_workload("test-id", "test-ns")

    def test_create_workload_updates_informer_cache(self, mock_k8s_client):
        """
        Test case: informer cache is updated immediately after create
        """
        created_body = {"metadata": {"name": "test-id", "uid": "test-uid"}}

        class FakeInformer:
            def __init__(self):
                self.started = False
                self.updated = None

            def start(self):
                self.started = True

            def update_cache(self, obj):
                self.updated = obj

        fake_informer = FakeInformer()
        provider = BatchSandboxProvider(
            mock_k8s_client,
            enable_informer=True,
            informer_factory=lambda ns: fake_informer,
        )
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = created_body

        expires_at = datetime(2025, 12, 31, tzinfo=timezone.utc)

        result = provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={"FOO": "bar"},
            resource_limits={"cpu": "1", "memory": "1Gi"},
            labels={"opensandbox.io/id": "test-id"},
            expires_at=expires_at,
            execd_image="execd:latest",
        )

        assert result == {"name": "test-id", "uid": "test-uid"}
        assert fake_informer.updated == created_body
        assert fake_informer.started is True

    def test_get_informer_single_instance_per_namespace(self, mock_k8s_client):
        """
        Test case: informer is created only once per namespace even with repeated calls
        """

        class FakeInformer:
            def __init__(self):
                self.started = 0

            def start(self):
                self.started += 1

            def update_cache(self, obj):
                self.updated = obj

        factory_calls = {"count": 0}

        def factory(ns):
            factory_calls["count"] += 1
            return FakeInformer()

        provider = BatchSandboxProvider(
            mock_k8s_client,
            enable_informer=True,
            informer_factory=factory,
        )

        informer1 = provider._get_informer("test-ns")
        informer2 = provider._get_informer("test-ns")

        assert informer1 is informer2
        assert factory_calls["count"] == 1
        assert informer1.started == 1

    # ===== Workload List Tests =====

    def test_list_workloads_returns_items(
        self, mock_k8s_client, mock_batchsandbox_list_response
    ):
        """
        Test case: Verify list query returns results
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.list_namespaced_custom_object.return_value = mock_batchsandbox_list_response

        result = provider.list_workloads("test-ns", "opensandbox.io/id")

        assert len(result) == 1
        assert result[0]["metadata"]["name"] == "test-id"

    def test_list_workloads_returns_empty_on_404(self, mock_k8s_client):
        """
        Test case: Verify empty list returned on 404
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.list_namespaced_custom_object.side_effect = ApiException(status=404)

        result = provider.list_workloads("test-ns", "opensandbox.io/id")

        assert result == []

    # ===== Workload Deletion Tests =====

    def test_delete_workload_deletes_existing_sandbox(
        self, mock_k8s_client, mock_batchsandbox_list_response
    ):
        """
        Test case: Verify successfully deleting existing sandbox
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.return_value = mock_batchsandbox_list_response["items"][0]

        provider.delete_workload("test-id", "test-ns")

        mock_api.delete_namespaced_custom_object.assert_called_once_with(
            group="sandbox.opensandbox.io",
            version="v1alpha1",
            namespace="test-ns",
            plural="batchsandboxes",
            name="test-id",
            grace_period_seconds=10
        )

    def test_delete_workload_raises_when_not_found(self, mock_k8s_client):
        """
        Test case: Verify exception raised when not found
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.side_effect = [
            ApiException(status=404),
            ApiException(status=404),
        ]

        with pytest.raises(Exception) as exc_info:
            provider.delete_workload("test-id", "test-ns")

        assert "not found" in str(exc_info.value)

    def test_delete_workload_sets_grace_period(
        self, mock_k8s_client, mock_batchsandbox_list_response
    ):
        """
        Test case: Verify graceful deletion (grace period = 10)
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.return_value = mock_batchsandbox_list_response["items"][0]

        provider.delete_workload("test-id", "test-ns")

        call_kwargs = mock_api.delete_namespaced_custom_object.call_args.kwargs
        assert call_kwargs["grace_period_seconds"] == 10

    # ===== Expiration Time Management Tests =====

    def test_update_expiration_patches_spec(
        self, mock_k8s_client, mock_batchsandbox_list_response
    ):
        """
        Test case: Verify expiration time update
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.get_namespaced_custom_object.return_value = mock_batchsandbox_list_response["items"][0]

        expires_at = datetime(2025, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
        provider.update_expiration("test-id", "test-ns", expires_at)

        call_kwargs = mock_api.patch_namespaced_custom_object.call_args.kwargs
        assert call_kwargs["body"] == {
            "spec": {"expireTime": "2025-12-31T00:00:00+00:00"}
        }

    def test_get_expiration_parses_iso_format(self):
        """
        Test case: Verify parsing ISO format time
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "spec": {"expireTime": "2025-12-31T10:00:00+00:00"}
        }

        result = provider.get_expiration(workload)

        assert result == datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)

    def test_get_expiration_handles_z_suffix(self):
        """
        Test case: Verify handling time with Z suffix
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "spec": {"expireTime": "2025-12-31T10:00:00Z"}
        }

        result = provider.get_expiration(workload)

        assert result == datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)

    def test_get_expiration_returns_none_on_invalid_format(self):
        """
        Test case: Verify None returned on invalid format
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "spec": {"expireTime": "invalid-date"}
        }

        # Should return None and not raise exception
        result = provider.get_expiration(workload)

        assert result is None

    def test_get_expiration_returns_none_when_missing(self):
        """
        Test case: Verify None returned when missing
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {"spec": {}}

        result = provider.get_expiration(workload)

        assert result is None

    # ===== Status Retrieval Tests =====

    def test_get_status_running_with_ip(self):
        """
        Test case: Verify status when Pod is Ready and has IP
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "status": {"replicas": 1, "ready": 1, "allocated": 1},
            "metadata": {
                "annotations": {
                    "sandbox.opensandbox.io/endpoints": '["10.0.0.1"]'
                },
                "creationTimestamp": "2025-12-24T10:00:00Z"
            }
        }

        result = provider.get_status(workload)

        assert result["state"] == "Running"
        assert result["reason"] == "READY_WITH_IP"
        assert "IP assigned" in result["message"]

    def test_get_status_pending_ready_without_ip(self):
        """
        Test case: Verify status when Pod is Ready but has no IP (should be Pending)
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "status": {"replicas": 1, "ready": 1, "allocated": 1},
            "metadata": {"creationTimestamp": "2025-12-24T10:00:00Z"}
        }

        result = provider.get_status(workload)

        assert result["state"] == "Pending"
        assert result["reason"] == "POD_READY_NO_IP"

    def test_get_status_pending_scheduled(self):
        """
        Test case: Verify Pod is scheduled but not Ready
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "status": {"replicas": 1, "ready": 0, "allocated": 1},
            "metadata": {"creationTimestamp": "2025-12-24T10:00:00Z"}
        }

        result = provider.get_status(workload)

        assert result["state"] == "Pending"
        assert result["reason"] == "POD_SCHEDULED"

    def test_get_status_pending_unallocated(self):
        """
        Test case: Verify Pod is not scheduled
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "status": {"replicas": 1, "ready": 0, "allocated": 0},
            "metadata": {"creationTimestamp": "2025-12-24T10:00:00Z"}
        }

        result = provider.get_status(workload)

        assert result["state"] == "Pending"
        assert result["reason"] == "BATCHSANDBOX_PENDING"

    # ===== Endpoint Information Tests =====

    def test_get_endpoint_info_parses_json_annotation(self):
        """
        Test case: Verify parsing IP from annotation
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "metadata": {
                "annotations": {
                    "sandbox.opensandbox.io/endpoints": '["10.0.0.1"]'
                }
            }
        }

        result = provider.get_endpoint_info(workload, 8080, "sandbox-123")

        assert result.endpoint == "10.0.0.1:8080"
        assert result.headers is None

    def test_get_endpoint_info_uses_first_ip(self):
        """
        Test case: Verify using first IP when multiple IPs exist
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "metadata": {
                "annotations": {
                    "sandbox.opensandbox.io/endpoints": '["10.0.0.1", "10.0.0.2"]'
                }
            }
        }

        result = provider.get_endpoint_info(workload, 8080, "sandbox-123")

        assert result.endpoint == "10.0.0.1:8080"
        assert result.headers is None

    def test_get_endpoint_info_returns_none_when_missing(self):
        """
        Test case: Verify None returned when annotation is missing
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {"metadata": {"annotations": {}}}

        result = provider.get_endpoint_info(workload, 8080, "sandbox-123")

        assert result is None

    def test_get_endpoint_info_returns_none_on_invalid_json(self):
        """
        Test case: Verify None returned on invalid JSON
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "metadata": {
                "annotations": {
                    "sandbox.opensandbox.io/endpoints": "invalid-json"
                }
            }
        }

        result = provider.get_endpoint_info(workload, 8080, "sandbox-123")

        assert result is None

    def test_get_endpoint_info_returns_none_on_empty_array(self):
        """
        Test case: Verify None returned on empty array
        """
        provider = BatchSandboxProvider(MagicMock())
        workload = {
            "metadata": {
                "annotations": {
                    "sandbox.opensandbox.io/endpoints": "[]"
                }
            }
        }

        result = provider.get_endpoint_info(workload, 8080, "sandbox-123")

        assert result is None

    # ===== Pool-based Creation Tests =====

    def test_create_workload_poolref_ignores_image_spec(self, mock_k8s_client):
        """
        Test that pool-based creation ignores image_spec parameter.

        Pool already defines the image, so image_spec is not used even if provided.
        This verifies backward compatibility - no error is raised.
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test-id", "uid": "test-uid"}
        }

        result = provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["python", "app.py"],
            env={},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest",
            extensions={"poolRef": "my-pool"}
        )

        # Should succeed and return workload info
        assert result == {"name": "sandbox-test-id", "uid": "test-uid"}

        # Verify poolRef is used
        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        assert body["spec"]["poolRef"] == "my-pool"

    def test_create_workload_poolref_ignores_resource_limits(self, mock_k8s_client):
        """
        Test that pool-based creation ignores resource_limits parameter.

        Pool already defines the resources, so resource_limits is not used even if provided.
        This verifies backward compatibility - no error is raised.
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test-id", "uid": "test-uid"}
        }

        result = provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri=""),
            entrypoint=["python", "app.py"],
            env={},
            resource_limits={"cpu": "1", "memory": "1Gi"},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest",
            extensions={"poolRef": "my-pool"}
        )

        # Should succeed and return workload info
        assert result == {"name": "sandbox-test-id", "uid": "test-uid"}

        # Verify poolRef is used
        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        assert body["spec"]["poolRef"] == "my-pool"

    def test_create_workload_poolref_allows_entrypoint_and_env(self, mock_k8s_client):
        """
        Test that pool-based creation allows customizing entrypoint and env.

        Verifies taskTemplate structure is correctly generated with user's entrypoint and env.
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "sandbox-test-id", "uid": "test-uid"}
        }

        result = provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri=""),
            entrypoint=["python", "app.py"],
            env={"FOO": "bar"},
            resource_limits={},
            labels={},
            expires_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
            execd_image="execd:latest",
            extensions={"poolRef": "my-pool"}
        )

        assert result == {"name": "sandbox-test-id", "uid": "test-uid"}

        # Verify the call
        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        assert body["spec"]["poolRef"] == "my-pool"
        assert "taskTemplate" in body["spec"]

        # Verify taskTemplate structure
        task_template = body["spec"]["taskTemplate"]
        assert "spec" in task_template
        assert "process" in task_template["spec"]
        command = task_template["spec"]["process"]["command"]
        assert command[0] == "/bin/sh"
        assert command[1] == "-c"
        # Command should contain bootstrap.sh execution
        # Example: /opt/opensandbox/bin/bootstrap.sh python app.py &
        assert "/opt/opensandbox/bin/bootstrap.sh python app.py" in command[2]
        assert command[2].endswith(" &")
        env_list = task_template["spec"]["process"]["env"]
        env_dict = {e["name"]: e["value"] for e in env_list}
        assert env_dict["FOO"] == "bar"
        # Pool sandboxes should also have EXECD_ACCESS_TOKEN injected
        assert "EXECD_ACCESS_TOKEN" in env_dict
        assert len(env_dict["EXECD_ACCESS_TOKEN"]) == 64

    def test_build_task_template_with_env(self, mock_k8s_client):
        """
        Test _build_task_template with environment variables.

        Verifies:
        - Command uses shell wrapper: /bin/sh -c "..."
        - Entrypoint executed via bootstrap.sh in background (&)
        - Env list formatted correctly for K8s

        Generated command example:
        /bin/sh -c "/opt/opensandbox/bin/bootstrap.sh /usr/bin/python app.py &"
        """
        provider = BatchSandboxProvider(mock_k8s_client)

        result = provider._build_task_template(
            entrypoint=["/usr/bin/python", "app.py"],
            env={"KEY1": "value1", "KEY2": "value2"}
        )

        assert "spec" in result
        assert "process" in result["spec"]
        process_task = result["spec"]["process"]

        # Verify command structure
        command = process_task["command"]
        assert command[0] == "/bin/sh"
        assert command[1] == "-c"
        # Should execute via bootstrap.sh in background (&)
        assert "/opt/opensandbox/bin/bootstrap.sh" in command[2]
        assert "/usr/bin/python" in command[2]
        assert "app.py" in command[2]
        # Should end with & (run in background)
        assert command[2].endswith("&")

        # Verify env list
        assert process_task["env"] == [
            {"name": "KEY1", "value": "value1"},
            {"name": "KEY2", "value": "value2"}
        ]

    def test_build_task_template_without_env(self, mock_k8s_client):
        """
        Test _build_task_template without environment variables.

        Verifies command is wrapped in shell and executes via bootstrap.sh in background.

        Generated command example:
        /bin/sh -c "/opt/opensandbox/bin/bootstrap.sh /usr/bin/python app.py &"
        """
        provider = BatchSandboxProvider(mock_k8s_client)

        result = provider._build_task_template(
            entrypoint=["/usr/bin/python", "app.py"],
            env={}
        )

        assert "spec" in result
        assert "process" in result["spec"]
        process_task = result["spec"]["process"]
        assert process_task["env"] == []
        # Without env, command directly calls bootstrap.sh in background
        command = process_task["command"]
        assert command[0] == "/bin/sh"
        assert command[1] == "-c"
        # Check escaped entrypoint
        assert "/opt/opensandbox/bin/bootstrap.sh" in command[2]
        assert "/usr/bin/python" in command[2]
        assert "app.py" in command[2]
        assert command[2].endswith(" &")

    def test_build_task_template_uses_default_env_path(self, mock_k8s_client):
        """
        Test that taskTemplate executes bootstrap.sh properly.

        Verifies:
        - Entrypoint is properly escaped
        - Command runs in background
        """
        provider = BatchSandboxProvider(mock_k8s_client)

        result = provider._build_task_template(
            entrypoint=["python", "app.py"],
            env={"TEST_VAR": "test_value"}
        )

        command = result["spec"]["process"]["command"][2]
        # Should execute bootstrap.sh in background
        assert "/opt/opensandbox/bin/bootstrap.sh" in command
        assert "python" in command
        assert "app.py" in command
        assert command.endswith(" &")

    def test_build_task_template_escapes_special_characters(self, mock_k8s_client):
        """
        Test that taskTemplate properly escapes arguments with spaces, quotes, and special chars.

        This prevents shell injection and ensures arguments are preserved correctly.
        For example: ['python', '-c', 'print("a b")'] should work correctly.
        """
        provider = BatchSandboxProvider(mock_k8s_client)

        result = provider._build_task_template(
            entrypoint=["python", "-c", 'print("hello world")'],
            env={"KEY": "value with spaces", "QUOTE": "it's fine"}
        )

        command = result["spec"]["process"]["command"][2]

        # Verify entrypoint args are properly escaped
        assert "python" in command
        assert "-c" in command
        # The python code with spaces and quotes should be properly escaped
        assert "'print(" in command or '"print(' in command  # Escaped

        # Verify env is passed through env list, not in command
        env_list = result["spec"]["process"]["env"]
        assert {"name": "KEY", "value": "value with spaces"} in env_list
        assert {"name": "QUOTE", "value": "it's fine"} in env_list

    def test_create_workload_poolref_builds_correct_manifest(self, mock_k8s_client):
        """
        Test complete pool-based BatchSandbox manifest structure.

        Verifies:
        - Basic metadata (apiVersion, kind, name, labels)
        - Pool-specific fields (poolRef, taskTemplate, expireTime)
        - No template field (pool mode doesn't use pod template)
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri=""),
            entrypoint=["python", "app.py"],
            env={"FOO": "bar"},
            resource_limits={},
            labels={"test": "label"},
            expires_at=expires_at,
            execd_image="execd:latest",
            extensions={"poolRef": "test-pool"}
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]

        # Verify basic structure
        assert body["apiVersion"] == "sandbox.opensandbox.io/v1alpha1"
        assert body["kind"] == "BatchSandbox"
        assert body["metadata"]["name"] == "test-id"
        assert body["metadata"]["labels"] == {"test": "label"}

        # Verify pool-specific fields
        assert body["spec"]["replicas"] == 1
        assert body["spec"]["poolRef"] == "test-pool"
        assert body["spec"]["expireTime"] == "2025-12-31T10:00:00+00:00"
        assert "taskTemplate" in body["spec"]

        # Verify no template field (pool-based doesn't use template)
        assert "template" not in body["spec"]


class TestBatchSandboxProviderEgress:
    """BatchSandboxProvider egress sidecar tests"""

    def test_create_workload_without_network_policy_no_sidecar(self, mock_k8s_client):
        """
        Test case: Verify no sidecar is added when network_policy is None
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            network_policy=None,
            egress_image=None,
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]
        containers = pod_spec["containers"]

        # Should only have main container
        assert len(containers) == 1
        assert containers[0]["name"] == "sandbox"
        # Should not have securityContext with sysctls
        assert "securityContext" not in pod_spec or "sysctls" not in pod_spec.get("securityContext", {})

    def test_create_workload_with_network_policy_adds_sidecar(self, mock_k8s_client):
        """
        Test case: Verify egress sidecar is added when network_policy is provided
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="pypi.org")],
        )

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            network_policy=network_policy,
            egress_image="opensandbox/egress:v1.0.1",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]
        containers = pod_spec["containers"]

        # Should have both main container and sidecar
        assert len(containers) == 2

        # Find sidecar container
        sidecar = next((c for c in containers if c["name"] == "egress"), None)
        assert sidecar is not None
        assert sidecar["image"] == "opensandbox/egress:v1.0.1"

        # Verify sidecar has environment variable
        env_vars = {e["name"]: e["value"] for e in sidecar.get("env", [])}
        assert "OPENSANDBOX_EGRESS_RULES" in env_vars

        # Verify sidecar has NET_ADMIN capability
        assert "securityContext" in sidecar
        assert "capabilities" in sidecar["securityContext"]
        assert "add" in sidecar["securityContext"]["capabilities"]
        assert "NET_ADMIN" in sidecar["securityContext"]["capabilities"]["add"]

    def test_create_workload_with_network_policy_drops_net_admin_from_main_container(self, mock_k8s_client):
        """
        Test case: Verify main container drops NET_ADMIN when network_policy is enabled
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            network_policy=network_policy,
            egress_image="opensandbox/egress:v1.0.1",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]
        containers = pod_spec["containers"]

        # Find main container
        main_container = next((c for c in containers if c["name"] == "sandbox"), None)
        assert main_container is not None

        # Verify main container has securityContext
        assert "securityContext" in main_container
        assert "capabilities" in main_container["securityContext"]
        assert "drop" in main_container["securityContext"]["capabilities"]
        assert "NET_ADMIN" in main_container["securityContext"]["capabilities"]["drop"]

    def test_create_workload_without_egress_image_no_sidecar(self, mock_k8s_client):
        """
        Test case: Verify no sidecar is added when egress_image is None even if network_policy exists
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            network_policy=network_policy,
            egress_image=None,
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]
        containers = pod_spec["containers"]

        # Should only have main container
        assert len(containers) == 1
        assert containers[0]["name"] == "sandbox"

    def test_egress_sidecar_contains_network_policy_in_env(self, mock_k8s_client):
        """
        Test case: Verify sidecar environment variable contains serialized network policy
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[
                NetworkRule(action="allow", target="pypi.org"),
                NetworkRule(action="deny", target="*.malicious.com"),
            ],
        )

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            network_policy=network_policy,
            egress_image="opensandbox/egress:v1.0.1",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]
        containers = pod_spec["containers"]

        sidecar = next((c for c in containers if c["name"] == "egress"), None)
        assert sidecar is not None

        env_vars = {e["name"]: e["value"] for e in sidecar.get("env", [])}
        assert "OPENSANDBOX_EGRESS_RULES" in env_vars

        # Verify the environment variable contains valid JSON with network policy
        import json
        policy_json = json.loads(env_vars["OPENSANDBOX_EGRESS_RULES"])
        assert policy_json["defaultAction"] == "deny"
        assert len(policy_json["egress"]) == 2
        assert policy_json["egress"][0]["action"] == "allow"
        assert policy_json["egress"][0]["target"] == "pypi.org"

    def test_main_container_no_security_context_without_network_policy(self, mock_k8s_client):
        """
        Test case: Verify main container has no securityContext when network_policy is None
        """
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            network_policy=None,
            egress_image=None,
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]
        containers = pod_spec["containers"]

        main_container = containers[0]
        # Main container should have allowPrivilegeEscalation: false but no capabilities drop
        sc = main_container.get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False
        assert "capabilities" not in sc

    def test_create_workload_with_network_policy_works_with_template(self, mock_k8s_client, tmp_path):
        """
        Test case: Verify egress sidecar works correctly when template is provided
        """
        template_file = tmp_path / "template.yaml"
        template_file.write_text(
            """
spec:
  template:
    spec:
      volumes:
        - name: sandbox-shared-data
          emptyDir: {}
"""
        )
        provider = BatchSandboxProvider(mock_k8s_client, str(template_file))
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            network_policy=network_policy,
            egress_image="opensandbox/egress:v1.0.1",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        pod_spec = body["spec"]["template"]["spec"]
        containers = pod_spec["containers"]

        # Should have both main container and sidecar
        assert len(containers) == 2

        # Verify sidecar exists
        sidecar = next((c for c in containers if c["name"] == "egress"), None)
        assert sidecar is not None

        # Verify template volumes are still merged
        volume_names = [v["name"] for v in pod_spec["volumes"]]
        assert "sandbox-shared-data" in volume_names
        assert "opensandbox-bin" in volume_names

    # ===== SANDBOX_USER Injection Tests =====

    def test_overlay_config_injects_sandbox_user(self, mock_k8s_client):
        """When filesystem_persistence=True and sandbox_user is set,
        SANDBOX_USER env var should be present on the sandbox container."""
        provider = BatchSandboxProvider(
            mock_k8s_client,
            filesystem_persistence=True,
            sandbox_user="user",
        )
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        sandbox = body["spec"]["template"]["spec"]["containers"][0]
        env_names = {e["name"]: e["value"] for e in sandbox.get("env", [])}
        assert env_names.get("SANDBOX_USER") == "user"
        assert env_names.get("OVERLAY_PERSIST") == "1"

    def test_overlay_config_drops_all_and_adds_minimal_capabilities(self, mock_k8s_client):
        """When filesystem_persistence=True, capabilities should drop ALL
        and only add the minimal set needed for overlay setup."""
        provider = BatchSandboxProvider(
            mock_k8s_client,
            filesystem_persistence=True,
            sandbox_user="user",
        )
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        sandbox = body["spec"]["template"]["spec"]["containers"][0]
        caps = sandbox["securityContext"]["capabilities"]
        assert "ALL" in caps["drop"]
        # Minimal set for overlay setup + privilege drop
        for required_cap in ["SYS_ADMIN", "CHOWN", "DAC_OVERRIDE", "FOWNER",
                             "SETUID", "SETGID", "SETPCAP", "KILL"]:
            assert required_cap in caps["add"], f"{required_cap} missing from add list"
        # Dangerous capabilities should NOT be in the add list
        for dangerous_cap in ["NET_RAW", "NET_ADMIN", "SYS_PTRACE", "MKNOD",
                              "NET_BIND_SERVICE", "AUDIT_WRITE"]:
            assert dangerous_cap not in caps["add"], f"{dangerous_cap} should not be added"

    def test_overlay_config_no_sandbox_user_when_none(self, mock_k8s_client):
        """When filesystem_persistence=True and sandbox_user=None,
        no SANDBOX_USER env var should be set."""
        provider = BatchSandboxProvider(
            mock_k8s_client,
            filesystem_persistence=True,
            sandbox_user=None,
        )
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        sandbox = body["spec"]["template"]["spec"]["containers"][0]
        env_names = [e["name"] for e in sandbox.get("env", [])]
        assert "SANDBOX_USER" not in env_names

    def test_no_sandbox_user_when_persistence_disabled(self, mock_k8s_client):
        """When filesystem_persistence=False, no SANDBOX_USER env var
        should be set regardless of sandbox_user config."""
        provider = BatchSandboxProvider(
            mock_k8s_client,
            filesystem_persistence=False,
            sandbox_user="user",
        )
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "test-uid"}
        }

        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
        )

        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        sandbox = body["spec"]["template"]["spec"]["containers"][0]
        env_names = [e["name"] for e in sandbox.get("env", [])]
        assert "SANDBOX_USER" not in env_names
        assert "OVERLAY_PERSIST" not in env_names

    # ===== Security Hardening Tests =====

    def test_volume_type_validation_rejects_hostpath(self, mock_k8s_client):
        """hostPath volumes should be rejected."""
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)
        vol = {"name": "bad", "hostPath": {"path": "/etc"}}
        assert provider._is_safe_volume(vol) is False

    def test_volume_type_validation_rejects_nfs(self, mock_k8s_client):
        """nfs volumes should be rejected."""
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)
        vol = {"name": "bad", "nfs": {"server": "evil", "path": "/"}}
        assert provider._is_safe_volume(vol) is False

    def test_volume_type_validation_allows_emptydir(self, mock_k8s_client):
        """emptyDir volumes should be allowed."""
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)
        vol = {"name": "ok", "emptyDir": {}}
        assert provider._is_safe_volume(vol) is True

    def test_volume_type_validation_allows_configmap(self, mock_k8s_client):
        """configMap volumes should be allowed."""
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)
        vol = {"name": "ok", "configMap": {"name": "my-config"}}
        assert provider._is_safe_volume(vol) is True

    def test_volume_type_validation_allows_pvc(self, mock_k8s_client):
        """persistentVolumeClaim volumes should be allowed."""
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)
        vol = {"name": "ok", "persistentVolumeClaim": {"claimName": "my-pvc"}}
        assert provider._is_safe_volume(vol) is True

    def test_non_overlay_sets_allow_privilege_escalation_false(self, mock_k8s_client):
        """Non-overlay sandbox containers should have allowPrivilegeEscalation: false."""
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test", "uid": "uid-1"},
        }
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=None,
            enable_informer=False, filesystem_persistence=False,
        )
        expires_at = datetime(2025, 12, 31, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="test-id", namespace="ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"], env={}, resource_limits={},
            labels={}, expires_at=expires_at, execd_image="execd:latest",
        )
        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        sc = body["spec"]["template"]["spec"]["containers"][0].get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False

    def test_overlay_mode_does_not_block_privilege_escalation(self, mock_k8s_client):
        """Overlay sandbox containers must not set allowPrivilegeEscalation: false."""
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test", "uid": "uid-1"},
        }
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=None,
            enable_informer=False, filesystem_persistence=True,
        )
        expires_at = datetime(2025, 12, 31, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="test-id", namespace="ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"], env={}, resource_limits={},
            labels={}, expires_at=expires_at, execd_image="execd:latest",
        )
        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        sc = body["spec"]["template"]["spec"]["containers"][0].get("securityContext", {})
        assert "allowPrivilegeEscalation" not in sc

    def test_execd_token_injected_in_env_and_secret(self, mock_k8s_client):
        """Execd access token should be generated, stored in env + K8s Secret (not annotation)."""
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_core_api = mock_k8s_client.get_core_v1_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "test", "uid": "uid-1"},
        }
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=None,
            enable_informer=False,
        )
        expires_at = datetime(2025, 12, 31, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="test-id", namespace="ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"], env={}, resource_limits={},
            labels={}, expires_at=expires_at, execd_image="execd:latest",
        )
        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]

        # Token should NOT be in annotation
        annotations = body["metadata"].get("annotations", {})
        assert "opensandbox.io/execd-token" not in annotations

        # Token should be in container env
        container = body["spec"]["template"]["spec"]["containers"][0]
        env_dict = {e["name"]: e["value"] for e in container.get("env", [])}
        assert "EXECD_ACCESS_TOKEN" in env_dict
        token = env_dict["EXECD_ACCESS_TOKEN"]
        assert len(token) == 64  # 32 bytes hex

        # Token should be stored in a K8s Secret
        mock_core_api.create_namespaced_secret.assert_called_once()
        secret_body = mock_core_api.create_namespaced_secret.call_args.kwargs["body"]
        assert secret_body["metadata"]["name"] == "test-id-execd-token"
        assert secret_body["stringData"]["token"] == token
        assert secret_body["metadata"]["labels"]["opensandbox.io/purpose"] == "execd-token"

    def test_get_execd_token_reads_from_secret(self, mock_k8s_client):
        """get_execd_token should read from K8s Secret, not CR annotation."""
        import base64
        mock_core_api = mock_k8s_client.get_core_v1_api()
        mock_secret = MagicMock()
        mock_secret.data = {"token": base64.b64encode(b"abc123").decode()}
        mock_core_api.read_namespaced_secret.return_value = mock_secret

        provider = BatchSandboxProvider(mock_k8s_client, enable_informer=False)
        result = provider.get_execd_token("test-id", "ns")

        assert result == "abc123"
        mock_core_api.read_namespaced_secret.assert_called_once_with(
            name="test-id-execd-token", namespace="ns"
        )

    def test_get_execd_token_returns_none_on_404(self, mock_k8s_client):
        """get_execd_token should return None when secret not found."""
        mock_core_api = mock_k8s_client.get_core_v1_api()
        mock_core_api.read_namespaced_secret.side_effect = ApiException(status=404)

        provider = BatchSandboxProvider(mock_k8s_client, enable_informer=False)
        result = provider.get_execd_token("test-id", "ns")

        assert result is None

    def test_delete_workload_cleans_up_execd_token_secret(self, mock_k8s_client):
        """delete_workload should clean up the execd token Secret."""
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_core_api = mock_k8s_client.get_core_v1_api()
        mock_api.get_namespaced_custom_object.return_value = {
            "metadata": {"name": "test-id", "uid": "uid-1"},
        }

        provider = BatchSandboxProvider(mock_k8s_client, enable_informer=False)
        provider.delete_workload("test-id", "ns")

        mock_core_api.delete_namespaced_secret.assert_any_call(
            name="test-id-execd-token", namespace="ns"
        )
        mock_core_api.delete_namespaced_secret.assert_any_call(
            name="test-id-egress-token", namespace="ns"
        )

    def test_pool_workload_creates_token_secret_and_injects_env(self, mock_k8s_client):
        """Pool-based sandbox creation should generate token, create Secret, and inject env."""
        mock_api = mock_k8s_client.get_custom_objects_api()
        mock_core_api = mock_k8s_client.get_core_v1_api()
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "pool-sb", "uid": "uid-pool"},
        }

        provider = BatchSandboxProvider(mock_k8s_client, enable_informer=False)
        expires_at = datetime(2025, 12, 31, tzinfo=timezone.utc)
        provider.create_workload(
            sandbox_id="pool-sb", namespace="ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"], env={"FOO": "bar"}, resource_limits={},
            labels={"app": "test"}, expires_at=expires_at, execd_image="execd:latest",
            extensions={"poolRef": "my-pool"},
        )

        # Secret should be created
        mock_core_api.create_namespaced_secret.assert_called_once()
        secret_body = mock_core_api.create_namespaced_secret.call_args.kwargs["body"]
        assert secret_body["metadata"]["name"] == "pool-sb-execd-token"
        token = secret_body["stringData"]["token"]
        assert len(token) == 64

        # Token should be injected as env var in taskTemplate
        body = mock_api.create_namespaced_custom_object.call_args.kwargs["body"]
        env_list = body["spec"]["taskTemplate"]["spec"]["process"]["env"]
        env_dict = {e["name"]: e["value"] for e in env_list}
        assert env_dict["EXECD_ACCESS_TOKEN"] == token

    def test_is_safe_volume_rejects_no_type_keys(self):
        """_is_safe_volume should reject volumes with no type keys (only 'name')."""
        assert BatchSandboxProvider._is_safe_volume({"name": "foo"}) is False

    def test_is_safe_volume_rejects_empty_dict(self):
        """_is_safe_volume should reject completely empty volume dict."""
        assert BatchSandboxProvider._is_safe_volume({}) is False

    def test_build_main_container_liveness_probe_with_network_policy(self, mock_k8s_client):
        """Test that liveness probe is added when has_network_policy=True."""
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)
        container = provider._build_main_container(
            image_spec=ImageSpec(uri="test:latest"),
            entrypoint=["sleep", "infinity"],
            env={},
            has_network_policy=True,
        )
        assert container.liveness_probe is not None
        assert container.liveness_probe._exec is not None
        assert "wget" in container.liveness_probe._exec.command
        assert "http://127.0.0.1:18080/healthz" in container.liveness_probe._exec.command
        assert container.liveness_probe.initial_delay_seconds == 5
        assert container.liveness_probe.period_seconds == 5
        assert container.liveness_probe.failure_threshold == 3
        # Readiness probe should always be present
        assert container.readiness_probe is not None
        assert container.readiness_probe.http_get is not None
        assert container.readiness_probe.http_get.path == "/healthz"
        assert container.readiness_probe.http_get.port == 44772
        assert container.readiness_probe.initial_delay_seconds == 1
        assert container.readiness_probe.period_seconds == 1
        assert container.readiness_probe.failure_threshold == 30

    def test_build_main_container_no_liveness_probe_without_network_policy(self, mock_k8s_client):
        """Test that liveness probe is NOT added when has_network_policy=False."""
        provider = BatchSandboxProvider(mock_k8s_client, template_file_path=None)
        container = provider._build_main_container(
            image_spec=ImageSpec(uri="test:latest"),
            entrypoint=["sleep", "infinity"],
            env={},
            has_network_policy=False,
        )
        assert container.liveness_probe is None
        # Readiness probe should still be present even without network policy
        assert container.readiness_probe is not None
        assert container.readiness_probe.http_get is not None
        assert container.readiness_probe.http_get.path == "/healthz"
        assert container.readiness_probe.http_get.port == 44772

    def test_resume_workload_updates_execd_image(self, mock_k8s_client):
        """Test that resume_workload patches execd-installer init container image to current version."""
        new_execd_image = "ghcr.io/opensandbox/execd:v2.0.0"
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=None, execd_image=new_execd_image
        )

        # Simulate a stored BatchSandbox CRD with an old execd image
        old_batchsandbox = {
            "metadata": {
                "name": "test-sandbox",
                "namespace": "default",
                "labels": {"opensandbox.io/id": "test-sandbox"},
                "annotations": {
                    "sandbox.opensandbox.io/paused": "true",
                },
            },
            "spec": {
                "replicas": 0,
                "template": {
                    "spec": {
                        "initContainers": [
                            {
                                "name": "execd-installer",
                                "image": "ghcr.io/opensandbox/execd:v1.0.0",
                                "volumeMounts": [{"name": "execd", "mountPath": "/execd"}],
                            },
                            {
                                "name": "other-init",
                                "image": "busybox:latest",
                            },
                        ],
                        "containers": [
                            {"name": "sandbox", "image": "python:3.11"}
                        ],
                    }
                },
            },
        }

        mock_custom_api = mock_k8s_client.get_custom_objects_api()
        mock_custom_api.get_namespaced_custom_object.return_value = old_batchsandbox
        mock_custom_api.patch_namespaced_custom_object.return_value = old_batchsandbox

        provider.resume_workload("test-sandbox", "default")

        # Verify the patch body was called with updated init containers
        call_args = mock_custom_api.patch_namespaced_custom_object.call_args
        body = call_args.kwargs.get("body") or call_args[1].get("body")

        init_containers = body["spec"]["template"]["spec"]["initContainers"]
        execd_container = next(ic for ic in init_containers if ic["name"] == "execd-installer")
        other_container = next(ic for ic in init_containers if ic["name"] == "other-init")

        assert execd_container["image"] == new_execd_image
        assert other_container["image"] == "busybox:latest"
        assert body["spec"]["replicas"] == 1

    def test_resume_workload_without_execd_image_keeps_original(self, mock_k8s_client):
        """Test that resume_workload preserves original image when execd_image is not set."""
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=None, execd_image=None
        )

        old_image = "ghcr.io/opensandbox/execd:v1.0.0"
        old_batchsandbox = {
            "metadata": {
                "name": "test-sandbox",
                "namespace": "default",
                "labels": {"opensandbox.io/id": "test-sandbox"},
                "annotations": {
                    "sandbox.opensandbox.io/paused": "true",
                },
            },
            "spec": {
                "replicas": 0,
                "template": {
                    "spec": {
                        "initContainers": [
                            {
                                "name": "execd-installer",
                                "image": old_image,
                            },
                        ],
                        "containers": [
                            {"name": "sandbox", "image": "python:3.11"}
                        ],
                    }
                },
            },
        }

        mock_custom_api = mock_k8s_client.get_custom_objects_api()
        mock_custom_api.get_namespaced_custom_object.return_value = old_batchsandbox
        mock_custom_api.patch_namespaced_custom_object.return_value = old_batchsandbox

        provider.resume_workload("test-sandbox", "default")

        call_args = mock_custom_api.patch_namespaced_custom_object.call_args
        body = call_args.kwargs.get("body") or call_args[1].get("body")

        init_containers = body["spec"]["template"]["spec"]["initContainers"]
        execd_container = next(ic for ic in init_containers if ic["name"] == "execd-installer")
        assert execd_container["image"] == old_image

    def test_resume_workload_applies_template_resources(self, mock_k8s_client, tmp_path):
        """Test that resume_workload applies current template resources to the sandbox container."""
        template_file = tmp_path / "template.yaml"
        template_file.write_text("""
spec:
  template:
    spec:
      containers:
        - name: sandbox
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2"
              memory: "4Gi"
""")
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=str(template_file)
        )

        old_batchsandbox = {
            "metadata": {
                "name": "test-sandbox",
                "namespace": "default",
                "labels": {"opensandbox.io/id": "test-sandbox"},
                "annotations": {
                    "sandbox.opensandbox.io/paused": "true",
                },
            },
            "spec": {
                "replicas": 0,
                "template": {
                    "spec": {
                        "initContainers": [
                            {"name": "execd-installer", "image": "execd:v1"},
                        ],
                        "containers": [
                            {
                                "name": "sandbox",
                                "image": "python:3.11",
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "256Mi"},
                                    "limits": {"cpu": "100m", "memory": "256Mi"},
                                },
                            }
                        ],
                    }
                },
            },
        }

        mock_custom_api = mock_k8s_client.get_custom_objects_api()
        mock_custom_api.get_namespaced_custom_object.return_value = old_batchsandbox
        mock_custom_api.patch_namespaced_custom_object.return_value = old_batchsandbox

        provider.resume_workload("test-sandbox", "default")

        call_args = mock_custom_api.patch_namespaced_custom_object.call_args
        body = call_args.kwargs.get("body") or call_args[1].get("body")

        containers = body["spec"]["template"]["spec"]["containers"]
        sandbox = next(c for c in containers if c["name"] == "sandbox")

        # Resources should be updated to new template values
        assert sandbox["resources"]["requests"] == {"cpu": "500m", "memory": "1Gi"}
        assert sandbox["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}

    def test_resume_workload_drops_containers_not_in_template(self, mock_k8s_client, tmp_path):
        """Test that resume_workload drops containers no longer in template but keeps template sidecars."""
        # Template still has a 'logging' sidecar but NOT 'dind'
        template_file = tmp_path / "template.yaml"
        template_file.write_text("""
spec:
  template:
    spec:
      containers:
        - name: sandbox
          image: python:3.11
        - name: logging
          image: fluentbit:v2.0.0
""")
        provider = BatchSandboxProvider(
            mock_k8s_client, template_file_path=str(template_file), execd_image=None
        )

        # Old pod spec has sandbox + dind + logging (dind was removed from template)
        old_batchsandbox = {
            "metadata": {
                "name": "test-sandbox",
                "namespace": "default",
                "labels": {"opensandbox.io/id": "test-sandbox"},
                "annotations": {
                    "sandbox.opensandbox.io/paused": "true",
                },
            },
            "spec": {
                "replicas": 0,
                "template": {
                    "spec": {
                        "initContainers": [
                            {"name": "execd-installer", "image": "execd:v1"},
                        ],
                        "containers": [
                            {"name": "sandbox", "image": "python:3.11"},
                            {"name": "dind", "image": "docker:dind-rootless"},
                            {"name": "logging", "image": "fluentbit:v1.0.0"},
                        ],
                    }
                },
            },
        }

        mock_custom_api = mock_k8s_client.get_custom_objects_api()
        mock_custom_api.get_namespaced_custom_object.return_value = old_batchsandbox
        mock_custom_api.patch_namespaced_custom_object.return_value = old_batchsandbox

        provider.resume_workload("test-sandbox", "default")

        call_args = mock_custom_api.patch_namespaced_custom_object.call_args
        body = call_args.kwargs.get("body") or call_args[1].get("body")

        containers = body["spec"]["template"]["spec"]["containers"]
        container_names = [c["name"] for c in containers]

        assert "sandbox" in container_names
        assert "logging" in container_names
        assert "dind" not in container_names
        # Verify logging sidecar image was updated to current template version
        logging_container = next(c for c in containers if c["name"] == "logging")
        assert logging_container["image"] == "fluentbit:v2.0.0"

    def test_config_hash_changes_when_template_resources_change(self, mock_k8s_client, tmp_path):
        """Test that _compute_image_config_hash changes when template resources change."""
        template_file = tmp_path / "template.yaml"

        # First: create provider with one set of resources
        template_file.write_text("""
spec:
  template:
    spec:
      containers:
        - name: sandbox
          resources:
            requests:
              cpu: "250m"
            limits:
              cpu: "1"
""")
        provider1 = BatchSandboxProvider(
            mock_k8s_client, template_file_path=str(template_file)
        )
        hash1 = provider1._image_config_hash

        # Second: create provider with different resources
        template_file.write_text("""
spec:
  template:
    spec:
      containers:
        - name: sandbox
          resources:
            requests:
              cpu: "500m"
            limits:
              cpu: "2"
""")
        provider2 = BatchSandboxProvider(
            mock_k8s_client, template_file_path=str(template_file)
        )
        hash2 = provider2._image_config_hash

        assert hash1 != hash2, "Config hash should change when template resources change"

    def test_config_hash_stable_when_resources_unchanged(self, mock_k8s_client, tmp_path):
        """Test that _compute_image_config_hash is stable when resources don't change."""
        template_file = tmp_path / "template.yaml"
        template_file.write_text("""
spec:
  template:
    spec:
      containers:
        - name: sandbox
          resources:
            requests:
              cpu: "250m"
            limits:
              cpu: "1"
""")
        provider1 = BatchSandboxProvider(
            mock_k8s_client, template_file_path=str(template_file)
        )
        provider2 = BatchSandboxProvider(
            mock_k8s_client, template_file_path=str(template_file)
        )

        assert provider1._image_config_hash == provider2._image_config_hash
