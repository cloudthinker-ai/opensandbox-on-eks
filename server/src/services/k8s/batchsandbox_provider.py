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
BatchSandbox-based workload provider implementation.
"""

import base64
from contextlib import nullcontext
import hashlib
import json
import logging
import secrets
import shlex
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Callable, Set, Tuple
from threading import Lock

from kubernetes.client import (
    V1Container,
    V1EnvVar,
    V1HTTPGetAction,
    V1Probe,
    V1SecurityContext,
    V1VolumeMount,
    ApiException,
)

from src.config import IngressConfig, INGRESS_MODE_GATEWAY
from src.services.constants import (
    ANNOTATION_ENDPOINTS,
    ANNOTATION_IMAGE_CONFIG_HASH,
    ANNOTATION_PAUSED,
    LABEL_SANDBOX_ID,
    ANNOTATION_SNAPSHOT_READY_AT,
    LABEL_EGRESS_SIDECAR,
    LABEL_PURPOSE,
    SANDBOX_COMPONENT_LABEL,
)
from src.services.helpers import format_ingress_endpoint
from src.api.schema import Endpoint, ImageSpec, NetworkPolicy
from src.services.k8s.batchsandbox_template import BatchSandboxTemplateManager
from src.services.k8s.client import K8sClient
from src.services.k8s.egress_helper import (
    apply_egress_to_spec,
    build_egress_sidecar_container,
    build_security_context_for_sandbox_container,
    build_security_context_from_dict,
    serialize_security_context_to_dict,
)
from src.services.k8s.informer import WorkloadInformer
from src.services.k8s.workload_provider import WorkloadProvider

logger = logging.getLogger(__name__)

_ALLOWED_VOLUME_TYPES = frozenset({
    "emptyDir", "persistentVolumeClaim", "configMap",
    "secret", "projected", "downwardAPI",
})


class BatchSandboxProvider(WorkloadProvider):
    """
    Workload provider using BatchSandbox CRD.

    BatchSandbox is a custom resource that manages Pod lifecycle
    and provides additional features like task management.
    """

    def __init__(
        self,
        k8s_client: K8sClient,
        template_file_path: Optional[str] = None,
        ingress_config: Optional[IngressConfig] = None,
        enable_informer: bool = True,
        informer_factory: Optional[Callable[[str], WorkloadInformer]] = None,
        informer_resync_seconds: int = 300,
        informer_watch_timeout_seconds: int = 60,
        storage_class: Optional[str] = None,
        storage_size: str = "20Gi",
        snapshot_enabled: bool = False,
        snapshot_class: Optional[str] = None,
        snapshot_max_retention: int = 1,
        archive_after_seconds: int = 86400,
        filesystem_persistence: bool = False,
        sandbox_user: Optional[str] = "user",
        execd_image: Optional[str] = None,
        egress_image: Optional[str] = None,
    ):
        """
        Initialize BatchSandbox provider.

        Args:
            k8s_client: Kubernetes client wrapper
            template_file_path: Optional path to BatchSandbox CR YAML template file
            storage_class: StorageClass for PVCs (None = cluster default)
            storage_size: Size of sandbox-data PVC (e.g., "20Gi")
            snapshot_enabled: Enable CSI VolumeSnapshot support
            snapshot_class: VolumeSnapshotClass name (required if snapshot_enabled)
            snapshot_max_retention: Max snapshots to retain per sandbox
            archive_after_seconds: Seconds after snapshot-ready before deleting PVC
            filesystem_persistence: Enable full filesystem persistence via OverlayFS
            sandbox_user: Non-root user to drop privileges to after overlay setup
        """
        self.k8s_client = k8s_client
        self.custom_api = k8s_client.get_custom_objects_api()
        self.ingress_config = ingress_config
        self.execd_image = execd_image
        self.egress_image = egress_image
        self.storage_class = storage_class
        self.storage_size = storage_size
        self.snapshot_enabled = snapshot_enabled
        self.filesystem_persistence = filesystem_persistence
        self.sandbox_user = sandbox_user

        # Initialize snapshot manager if enabled
        self.snapshot_manager = None
        if snapshot_enabled:
            if not snapshot_class:
                raise ValueError(
                    "snapshot_class is required when snapshot_enabled is true"
                )
            from src.services.k8s.snapshot_manager import SnapshotManager
            self.snapshot_manager = SnapshotManager(
                custom_api=self.custom_api,
                snapshot_class=snapshot_class,
                max_retention=snapshot_max_retention,
                core_v1_api=k8s_client.get_core_v1_api(),
            )

        # Per-sandbox lock function (injected by KubernetesSandboxService)
        self._get_sandbox_lock_fn: Optional[Callable[[str], threading.Lock]] = None

        # Async snapshot concurrency guards
        self._snapshot_in_progress: Set[str] = set()
        self._snapshot_lock = threading.Lock()

        # Archive lifecycle config
        self.archive_after_seconds = archive_after_seconds
        self._archive_sweep_namespaces: Set[str] = set()
        self._archive_sweep_lock = threading.Lock()

        # CRD constants
        self.group = "sandbox.opensandbox.io"
        self.version = "v1alpha1"
        self.plural = "batchsandboxes"

        # Template manager
        self.template_manager = BatchSandboxTemplateManager(template_file_path)

        # Cache immutable template-derived data (template is loaded once at init)
        self._template_sidecar_images = self._get_template_sidecar_images()
        self._template_sandbox_resources = self._get_template_sandbox_resources()
        self._template_sandbox_image = self._get_template_sandbox_image()
        self._template_pod_spec_overrides = self._get_template_pod_spec_overrides()
        self._image_config_hash = self._compute_image_config_hash()

        self._enable_informer = enable_informer
        self._informer_factory = informer_factory or (
            lambda ns: WorkloadInformer(
                custom_api=self.custom_api,
                group=self.group,
                version=self.version,
                plural=self.plural,
                namespace=ns,
                resync_period_seconds=informer_resync_seconds,
                watch_timeout_seconds=informer_watch_timeout_seconds,
            )
        )
        self._informers: Dict[str, WorkloadInformer] = {}
        self._informers_lock = Lock()

    def set_sandbox_lock_fn(
        self, fn: Callable[[str], threading.Lock]
    ) -> None:
        """Inject the per-sandbox lock factory from KubernetesSandboxService."""
        self._get_sandbox_lock_fn = fn

    def start_background_tasks(self, namespace: str) -> None:
        """Start background tasks on server startup (defense-in-depth).

        Ensures the archive sweep (including snapshot reconciliation) runs
        even if no sandbox is ever paused through the Python API.
        """
        if self.snapshot_manager and self.archive_after_seconds > 0:
            self._ensure_archive_sweep(namespace)

    def create_workload(
        self,
        sandbox_id: str,
        namespace: str,
        image_spec: ImageSpec,
        entrypoint: List[str],
        env: Dict[str, str],
        resource_limits: Dict[str, str],
        labels: Dict[str, str],
        expires_at: datetime,
        execd_image: str,
        extensions: Optional[Dict[str, str]] = None,
        network_policy: Optional[NetworkPolicy] = None,
        egress_image: Optional[str] = None,
        upstream_dns: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a BatchSandbox workload.

        Supports both template-based and pool-based creation:
        - Template mode (default): Creates workload with user-specified image, resources, and env
        - Pool mode (when extensions contains 'poolRef'): Creates workload from pre-warmed pool,
          only entrypoint and env can be customized

        Args:
            sandbox_id: Unique sandbox identifier
            namespace: Kubernetes namespace
            image_spec: Container image specification (not used in pool mode)
            entrypoint: Container entrypoint command
            env: Environment variables
            resource_limits: Ignored; resources come from the template.
                Non-empty values will log a warning.
            labels: Labels to apply
            expires_at: Expiration time
            execd_image: execd daemon image (not used in pool mode)
            extensions: General extension field for additional configuration.
                When contains 'poolRef', enables pool-based creation.
            network_policy: Optional network policy for egress traffic control.
                When provided, an egress sidecar container will be added to the Pod.

        Returns:
            Dict with 'name' and 'uid' of created BatchSandbox
        """
        extensions = extensions or {}

        # If poolRef is provided and not empty, create workload from pool
        if extensions.get("poolRef"):
            # When using pool, only entrypoint and env can be customized
            return self._create_workload_from_pool(
                batchsandbox_name=sandbox_id,
                namespace=namespace,
                labels=labels,
                pool_ref=extensions["poolRef"],
                expires_at=expires_at,
                entrypoint=entrypoint,
                env=env,
            )

        if resource_limits:
            logger.warning(
                "resource_limits parameter is ignored; resources come from the template. "
                "sandbox_id=%s, ignored_limits=%s",
                sandbox_id,
                resource_limits,
            )

        # Generate per-sandbox execd access token
        execd_token = secrets.token_hex(32)

        # Extract extra pod spec fragments from template (volumes, mounts, sidecars, env, resources).
        extra_volumes, extra_mounts, extra_containers, extra_env, extra_resources = self._extract_template_pod_extras()

        # Build init container for execd installation
        init_container = self._build_execd_init_container(execd_image)

        # Build main container with execd support
        main_container = self._build_main_container(
            image_spec=image_spec,
            entrypoint=entrypoint,
            env=env,
            has_network_policy=network_policy is not None,
            is_overlay_mode=self.filesystem_persistence,
            execd_token=execd_token,
        )

        # Build containers list
        containers = [self._container_to_dict(main_container)]

        # Build base pod spec
        pod_spec: Dict[str, Any] = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "initContainers": [self._container_to_dict(init_container)],
            "containers": containers,
            "volumes": [
                {
                    "name": "opensandbox-bin",
                    "emptyDir": {}
                }
            ],
        }

        # Add egress sidecar if network policy is provided
        egress_token = apply_egress_to_spec(
            pod_spec=pod_spec,
            containers=containers,
            network_policy=network_policy,
            egress_image=egress_image,
            upstream_dns=upstream_dns,
        )
        if network_policy and egress_image:
            labels[LABEL_EGRESS_SIDECAR] = "true"

        # Build runtime-generated BatchSandbox manifest
        # This contains only the essential runtime fields
        runtime_manifest = {
            "apiVersion": f"{self.group}/{self.version}",
            "kind": "BatchSandbox",
            "metadata": {
                "name": sandbox_id,
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    ANNOTATION_IMAGE_CONFIG_HASH: self._image_config_hash,
                },
            },
            "spec": {
                "replicas": 1,
                "expireTime": expires_at.isoformat(),
                "template": {
                    "metadata": {
                        "labels": {
                            SANDBOX_COMPONENT_LABEL: "sandbox",
                            **labels,
                        },
                    },
                    "spec": pod_spec,
                },
            },
        }

        # Merge with template to get final manifest
        batchsandbox = self.template_manager.merge_with_runtime_values(runtime_manifest)
        self._merge_pod_spec_extras(batchsandbox, extra_volumes, extra_mounts, extra_containers, extra_env, extra_resources)

        # Create PVC for sandbox data persistence and replace emptyDir with PVC ref
        pvc_name = self._create_sandbox_data_pvc(
            sandbox_id, namespace, labels,
            storage_class=self.storage_class,
            storage_size=self.storage_size,
        )
        self._replace_sandbox_volumes_with_pvc(batchsandbox, pvc_name)

        # Store execd token in a dedicated K8s Secret (not in CR annotation)
        self._create_execd_token_secret(sandbox_id, namespace, labels, execd_token)

        # Store egress token in a dedicated K8s Secret for hot-reload policy push
        if egress_token:
            self._create_egress_token_secret(sandbox_id, namespace, labels, egress_token)

        # When filesystem_persistence is enabled, configure the sandbox container
        # for OverlayFS: CAP_SYS_ADMIN, OVERLAY_PERSIST env var, and full PVC mount.
        if self.filesystem_persistence:
            if network_policy is not None:
                logger.warning(
                    "filesystem_persistence with network_policy: CAP_SYS_ADMIN "
                    "allows the sandbox to bypass egress restrictions at runtime"
                )
            self._apply_overlay_config(batchsandbox)

        # Create BatchSandbox
        created = self.custom_api.create_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            body=batchsandbox,
        )

        informer = self._get_informer(namespace)
        if informer:
            try:
                informer.update_cache(created)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to update informer cache for %s: %s", sandbox_id, exc)

        return {
            "name": created["metadata"]["name"],
            "uid": created["metadata"]["uid"],
        }

    def _create_workload_from_pool(
        self,
        batchsandbox_name: str,
        namespace: str,
        labels: Dict[str, str],
        pool_ref: str,
        expires_at: datetime,
        entrypoint: List[str],
        env: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        Create BatchSandbox workload from a pre-warmed resource pool.

        Pool-based creation uses poolRef to reference an existing pool.
        The pool already defines the pod template, so no additional template is needed.
        Only entrypoint and env can be customized.

        Args:
            batchsandbox_name: Name of the BatchSandbox resource
            namespace: Kubernetes namespace
            labels: Labels to apply
            pool_ref: Reference to the resource pool
            expires_at: Expiration time
            entrypoint: Container entrypoint command (can be customized)
            env: Environment variables (can be customized)

        Returns:
            Dict with 'name' and 'uid' of created BatchSandbox

        Raises:
            SandboxError: If required parameters are invalid
        """
        # Generate per-sandbox execd access token for pool sandboxes
        execd_token = secrets.token_hex(32)
        self._create_execd_token_secret(batchsandbox_name, namespace, labels, execd_token)

        runtime_manifest = {
            "apiVersion": f"{self.group}/{self.version}",
            "kind": "BatchSandbox",
            "metadata": {
                "name": batchsandbox_name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "poolRef": pool_ref,
                "expireTime": expires_at.isoformat(),
                "taskTemplate": self._build_task_template(entrypoint, env, execd_token=execd_token),
            },
        }

        # Pool-based creation does not need template merging
        # Create BatchSandbox directly
        created = self.custom_api.create_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            body=runtime_manifest,
        )

        informer = self._get_informer(namespace)
        if informer:
            try:
                informer.update_cache(created)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to update informer cache for %s: %s", batchsandbox_name, exc)

        return {
            "name": created["metadata"]["name"],
            "uid": created["metadata"]["uid"],
        }

    def _extract_template_pod_extras(
        self,
    ) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]], list[Dict[str, Any]], list[Dict[str, Any]], Dict[str, Any]]:
        """
        Extract extra volumes, volume mounts, sidecar containers, env vars,
        and resource requirements from the BatchSandbox template.

        Returns:
            Tuple of (extra_volumes, extra_mounts, extra_containers, extra_env, extra_resources)
            where extra_containers are non-"sandbox" containers from the template,
            extra_env are env vars from the "sandbox" container in the template,
            and extra_resources are the resource requirements from the "sandbox" container.
        """
        template = self.template_manager.get_base_template()
        spec = template.get("spec", {}) if isinstance(template, dict) else {}
        template_spec = spec.get("template", {}).get("spec", {})
        extra_volumes = template_spec.get("volumes", []) or []

        extra_mounts: list[Dict[str, Any]] = []
        extra_containers: list[Dict[str, Any]] = []
        extra_env: list[Dict[str, Any]] = []
        extra_resources: Dict[str, Any] = {}
        containers = template_spec.get("containers", []) or []
        if containers:
            for container in containers:
                name = container.get("name", "")
                if name == "sandbox":
                    # Extract volumeMounts, env, and resources from the sandbox container
                    extra_mounts = container.get("volumeMounts", []) or []
                    extra_env = container.get("env", []) or []
                    extra_resources = container.get("resources", {}) or {}
                else:
                    # Non-sandbox containers are sidecar containers
                    extra_containers.append(container)

            # Fallback: if no "sandbox" container found, use first container for mounts
            if not extra_mounts and not extra_env and containers:
                first = containers[0]
                if first.get("name") != containers[0].get("name") or not extra_containers:
                    extra_mounts = first.get("volumeMounts", []) or []

        if not isinstance(extra_volumes, list):
            extra_volumes = []
        if not isinstance(extra_mounts, list):
            extra_mounts = []
        if not isinstance(extra_containers, list):
            extra_containers = []
        if not isinstance(extra_env, list):
            extra_env = []
        return extra_volumes, extra_mounts, extra_containers, extra_env, extra_resources

    @staticmethod
    def _is_safe_volume(vol: Dict[str, Any]) -> bool:
        """Reject volumes with dangerous types (hostPath, nfs, etc.)."""
        vol_keys = set(vol.keys()) - {"name"}
        if not vol_keys:
            logger.warning("Rejecting template volume '%s': no volume type specified", vol.get("name"))
            return False
        for key in vol_keys:
            if key not in _ALLOWED_VOLUME_TYPES:
                logger.warning(
                    "Rejecting template volume '%s': disallowed type '%s'",
                    vol.get("name"), key,
                )
                return False
        return True

    def _merge_pod_spec_extras(
        self,
        batchsandbox: Dict[str, Any],
        extra_volumes: list[Dict[str, Any]],
        extra_mounts: list[Dict[str, Any]],
        extra_containers: Optional[list[Dict[str, Any]]] = None,
        extra_env: Optional[list[Dict[str, Any]]] = None,
        extra_resources: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Merge extra volumes, volumeMounts, sidecar containers, env vars,
        and resource requirements into the runtime-generated pod spec.

        This keeps execd injections intact while allowing user templates to
        provide additional mounts, sidecar containers, env vars,
        and resource requirements (requests/limits).
        """
        try:
            spec = batchsandbox["spec"]["template"]["spec"]
        except KeyError:
            return

        # Merge volumes by name (do not overwrite existing runtime volumes).
        volumes = spec.get("volumes", []) or []
        if isinstance(volumes, list) and extra_volumes:
            existing = {v.get("name") for v in volumes if isinstance(v, dict)}
            for vol in extra_volumes:
                if not isinstance(vol, dict):
                    continue
                name = vol.get("name")
                if not name or name in existing:
                    continue
                if not self._is_safe_volume(vol):
                    continue
                volumes.append(vol)
                existing.add(name)
            spec["volumes"] = volumes

        # Merge volumeMounts and env vars into the main container (index 0).
        containers = spec.get("containers", []) or []
        if not containers or not isinstance(containers, list):
            return
        main_container = containers[0]
        mounts = main_container.get("volumeMounts", []) or []
        if isinstance(mounts, list) and extra_mounts:
            existing = {m.get("name") for m in mounts if isinstance(m, dict)}
            for mnt in extra_mounts:
                if not isinstance(mnt, dict):
                    continue
                name = mnt.get("name")
                if not name or name in existing:
                    continue
                mounts.append(mnt)
                existing.add(name)
            main_container["volumeMounts"] = mounts

        # Merge env vars into the main container.
        if extra_env:
            env_list = main_container.get("env", []) or []
            existing_env = {e.get("name") for e in env_list if isinstance(e, dict)}
            for env_var in extra_env:
                if not isinstance(env_var, dict):
                    continue
                name = env_var.get("name")
                if not name or name in existing_env:
                    continue
                env_list.append(env_var)
                existing_env.add(name)
            main_container["env"] = env_list

        # Apply template resource requirements to main container.
        if extra_resources:
            main_container["resources"] = extra_resources

        # Append sidecar containers (by name, do not duplicate).
        if extra_containers:
            existing_names = {c.get("name") for c in containers if isinstance(c, dict)}
            for sidecar in extra_containers:
                if not isinstance(sidecar, dict):
                    continue
                name = sidecar.get("name")
                if not name or name in existing_names:
                    continue
                containers.append(sidecar)
                existing_names.add(name)
            spec["containers"] = containers

    # Todo support empty cmd or env
    def _build_task_template(
        self,
        entrypoint: List[str],
        env: Dict[str, str],
        execd_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build taskTemplate for pool-based BatchSandbox.

        In pool mode, task should use bootstrap.sh to start execd and business process.

        Generated command example:
            /bin/sh -c "/opt/opensandbox/bin/bootstrap.sh python app.py &"

        Note: All entrypoint arguments are properly shell-escaped using shlex.quote
        to prevent shell injection and preserve arguments with spaces or special characters.

        Args:
            entrypoint: Container entrypoint command
            env: Environment variables
            execd_token: Optional execd access token to inject as env var

        Returns:
            Dict: taskTemplate specification with TaskSpec structure
        """
        # Build command: execute bootstrap.sh with entrypoint in background
        # Use shlex.quote to safely escape each entrypoint argument to prevent shell injection
        escaped_entrypoint = ' '.join(shlex.quote(arg) for arg in entrypoint)
        user_process_cmd = f"/opt/opensandbox/bin/bootstrap.sh {escaped_entrypoint} &"

        wrapped_command = ["/bin/sh", "-c", user_process_cmd]

        # Convert env dict to k8s EnvVar format
        env_list = [{"name": k, "value": v} for k, v in env.items()] if env else []

        if execd_token:
            env_list.append({"name": "EXECD_ACCESS_TOKEN", "value": execd_token})

        # Return TaskTemplateSpec structure
        return {
            "spec": {
                "process": {
                    "command": wrapped_command,
                    "env": env_list,
                }
            }
        }

    def _build_execd_init_container(
        self,
        execd_image: str,
        extra_commands: Optional[List[str]] = None,
        extra_volume_mounts: Optional[List[V1VolumeMount]] = None,
    ) -> V1Container:
        """
        Build init container for execd installation.

        This init container copies execd binary and bootstrap.sh script from
        execd image to shared volume, making them available to the main container.

        Optionally runs extra shell commands so that a separate init
        container is not needed.

        Args:
            execd_image: execd container image
            extra_commands: additional shell commands to append to the script
            extra_volume_mounts: additional volume mounts for the init container

        Returns:
            V1Container: Init container spec
        """
        # Copy execd binary and bootstrap.sh from image to shared volume
        # Set 755 permissions so only root can modify (not world-writable)
        script = (
            "cp ./execd /opt/opensandbox/bin/execd && "
            "cp ./bootstrap.sh /opt/opensandbox/bin/bootstrap.sh && "
            "chmod 755 /opt/opensandbox/bin/execd && "
            "chmod 755 /opt/opensandbox/bin/bootstrap.sh && "
            "chmod 755 /opt/opensandbox/bin"
        )

        if extra_commands:
            script += " && " + " && ".join(extra_commands)

        volume_mounts = [
            V1VolumeMount(
                name="opensandbox-bin",
                mount_path="/opt/opensandbox/bin"
            )
        ]
        if extra_volume_mounts:
            volume_mounts.extend(extra_volume_mounts)

        # Run as root when extra commands need elevated privileges.
        security_context = None
        if extra_commands:
            security_context = V1SecurityContext(run_as_user=0)

        return V1Container(
            name="execd-installer",
            image=execd_image,
            command=["/bin/sh", "-c"],
            args=[script],
            volume_mounts=volume_mounts,
            security_context=security_context,
        )

    def _build_main_container(
        self,
        image_spec: ImageSpec,
        entrypoint: List[str],
        env: Dict[str, str],
        has_network_policy: bool = False,
        is_overlay_mode: bool = False,
        execd_token: Optional[str] = None,
    ) -> V1Container:
        """
        Build main container spec with execd support.

        The container will use bootstrap script to start execd in background,
        then execute user's command.  Resources are not set here; they come
        from the template via ``_merge_pod_spec_extras``.

        Args:
            image_spec: Container image specification
            entrypoint: Container entrypoint command
            env: Environment variables
            has_network_policy: Whether network policy is enabled for this sandbox
            is_overlay_mode: Whether overlay filesystem persistence is active
            execd_token: Optional access token for execd authentication

        Returns:
            V1Container: Main container spec
        """
        # Convert env dict to V1EnvVar list and inject EXECD path
        env_vars = [V1EnvVar(name=k, value=v) for k, v in env.items()]
        # Add EXECD environment variable to specify execd binary path
        env_vars.append(V1EnvVar(name="EXECD", value="/opt/opensandbox/bin/execd"))

        # Inject execd access token if provided
        if execd_token:
            env_vars.append(V1EnvVar(name="EXECD_ACCESS_TOKEN", value=execd_token))

        # Wrap entrypoint with bootstrap script to start execd
        wrapped_command = ["/opt/opensandbox/bin/bootstrap.sh"] + entrypoint

        # Always apply security context
        security_context_dict = build_security_context_for_sandbox_container(
            has_network_policy=has_network_policy,
            is_overlay_mode=is_overlay_mode,
        )
        security_context = build_security_context_from_dict(security_context_dict)

        # When egress sidecar is present, add liveness probe that checks the
        # sidecar's /healthz endpoint. Uses exec probe because the policy server
        # binds to 127.0.0.1 (not pod IP), and kubelet HTTP probes connect via pod IP.
        liveness_probe = None
        if has_network_policy:
            from kubernetes.client import V1ExecAction
            probe_dict = self._egress_liveness_probe_dict()
            liveness_probe = V1Probe(
                _exec=V1ExecAction(command=probe_dict["exec"]["command"]),
                initial_delay_seconds=probe_dict["initialDelaySeconds"],
                period_seconds=probe_dict["periodSeconds"],
                failure_threshold=probe_dict["failureThreshold"],
                timeout_seconds=probe_dict["timeoutSeconds"],
            )

        # Readiness probe: execd /healthz (unauthenticated) so kubelet only
        # marks the container Ready once execd is actually listening.
        # failureThreshold=30 with periodSeconds=1 allows up to ~30s for
        # execd startup before the container is marked NotReady.
        readiness_probe = V1Probe(
            http_get=V1HTTPGetAction(path="/healthz", port=44772),
            initial_delay_seconds=1,
            period_seconds=1,
            failure_threshold=30,
            timeout_seconds=1,
        )

        return V1Container(
            name="sandbox",
            image=image_spec.uri,
            command=wrapped_command,
            env=env_vars if env_vars else None,
            volume_mounts=[
                V1VolumeMount(
                    name="opensandbox-bin",
                    mount_path="/opt/opensandbox/bin"
                )
            ],
            security_context=security_context,
            liveness_probe=liveness_probe,
            readiness_probe=readiness_probe,
        )

    @staticmethod
    def _probe_to_dict(probe: V1Probe) -> Dict[str, Any]:
        """Serialize a V1Probe to a Kubernetes-compatible dict."""
        result: Dict[str, Any] = {}
        if probe.http_get:
            result["httpGet"] = {"path": probe.http_get.path, "port": probe.http_get.port}
        if probe._exec:
            result["exec"] = {"command": probe._exec.command}
        if probe.tcp_socket:
            result["tcpSocket"] = {"port": probe.tcp_socket.port}
            if probe.tcp_socket.host:
                result["tcpSocket"]["host"] = probe.tcp_socket.host
        if probe.grpc:
            result["grpc"] = {"port": probe.grpc.port}
            if probe.grpc.service:
                result["grpc"]["service"] = probe.grpc.service
        for attr, key in [
            ("initial_delay_seconds", "initialDelaySeconds"),
            ("period_seconds", "periodSeconds"),
            ("failure_threshold", "failureThreshold"),
            ("timeout_seconds", "timeoutSeconds"),
            ("success_threshold", "successThreshold"),
        ]:
            val = getattr(probe, attr, None)
            if val is not None:
                result[key] = val
        return result

    def _container_to_dict(self, container: V1Container) -> Dict[str, Any]:
        """
        Convert V1Container to dict for CRD.

        Args:
            container: V1Container object

        Returns:
            Dict representation of container
        """
        result = {
            "name": container.name,
            "image": container.image,
        }

        if container.command:
            result["command"] = container.command

        if container.args:
            result["args"] = container.args

        if container.env:
            result["env"] = [
                {"name": e.name, "value": e.value}
                for e in container.env
            ]

        if container.resources:
            result["resources"] = {}
            if container.resources.limits:
                result["resources"]["limits"] = container.resources.limits
            if container.resources.requests:
                result["resources"]["requests"] = container.resources.requests

        if container.volume_mounts:
            vm_list = []
            for vm in container.volume_mounts:
                entry: Dict[str, Any] = {"name": vm.name, "mountPath": vm.mount_path}
                if vm.sub_path:
                    entry["subPath"] = vm.sub_path
                vm_list.append(entry)
            result["volumeMounts"] = vm_list

        if container.security_context:
            security_context_dict = serialize_security_context_to_dict(container.security_context)
            if security_context_dict:
                result["securityContext"] = security_context_dict

        if container.liveness_probe:
            result["livenessProbe"] = self._probe_to_dict(container.liveness_probe)

        if container.readiness_probe:
            result["readinessProbe"] = self._probe_to_dict(container.readiness_probe)

        return result

    def _get_informer(self, namespace: str) -> Optional[WorkloadInformer]:
        if not self._enable_informer:
            return None

        with self._informers_lock:
            informer = self._informers.get(namespace)
            if informer is None:
                informer = self._informer_factory(namespace)
                self._informers[namespace] = informer
                try:
                    informer.start()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Failed to start informer for namespace %s: %s", namespace, exc
                    )
                    self._informers.pop(namespace, None)
                    return None
        return informer

    def get_execd_token(self, sandbox_id: str, namespace: str) -> Optional[str]:
        """Read the execd access token from the dedicated K8s Secret."""
        secret_name = f"{sandbox_id}-execd-token"
        core_v1 = self.k8s_client.get_core_v1_api()
        try:
            secret = core_v1.read_namespaced_secret(
                name=secret_name, namespace=namespace
            )
            return base64.b64decode(secret.data["token"]).decode()
        except ApiException as e:
            if e.status == 404:
                return None
            logger.error("Failed to read execd token secret %s: %s", secret_name, e)
            raise

    def get_egress_token(self, sandbox_id: str, namespace: str) -> Optional[str]:
        """Read the egress auth token from the dedicated K8s Secret."""
        secret_name = f"{sandbox_id}-egress-token"
        core_v1 = self.k8s_client.get_core_v1_api()
        try:
            secret = core_v1.read_namespaced_secret(
                name=secret_name, namespace=namespace
            )
            return base64.b64decode(secret.data["token"]).decode()
        except ApiException as e:
            if e.status == 404:
                return None
            logger.error("Failed to read egress token secret %s: %s", secret_name, e)
            raise

    def get_workload(self, sandbox_id: str, namespace: str) -> Optional[Dict[str, Any]]:
        """Get BatchSandbox by sandbox ID."""
        informer = self._get_informer(namespace)
        cache_ready = informer.has_synced if informer else False

        if informer and cache_ready:
            cached = informer.get(sandbox_id)
            if cached:
                return cached

            legacy_name = self.legacy_resource_name(sandbox_id)
            if legacy_name != sandbox_id:
                legacy_cached = informer.get(legacy_name)
                if legacy_cached:
                    return legacy_cached

        if informer and not cache_ready:
            logger.warning(
                f"Informer cache not synced for namespace {namespace}; falling back to direct API get."
            )

        try:
            workload = self.custom_api.get_namespaced_custom_object(
                group=self.group,
                version=self.version,
                namespace=namespace,
                plural=self.plural,
                name=sandbox_id,
            )
            if informer and workload:
                informer.update_cache(workload)
            return workload
        except ApiException as e:
            if e.status != 404:
                logger.error(f"Unexpected error getting BatchSandbox for {sandbox_id}: {e}")
                raise

        # Fallback for pre-upgrade sandboxes that used "sandbox-<id>" naming
        legacy_name = self.legacy_resource_name(sandbox_id)
        if legacy_name != sandbox_id:
            try:
                workload = self.custom_api.get_namespaced_custom_object(
                    group=self.group,
                    version=self.version,
                    namespace=namespace,
                    plural=self.plural,
                    name=legacy_name,
                )
                if informer and workload:
                    informer.update_cache(workload)
                return workload
            except ApiException as e:
                if e.status == 404:
                    return None
                raise
            except Exception as e:
                logger.error(f"Unexpected error getting BatchSandbox for {sandbox_id}: {e}")
                raise

        return None

    def delete_workload(self, sandbox_id: str, namespace: str) -> None:
        """Delete BatchSandbox workload and associated PVC."""
        batchsandbox = self.get_workload(sandbox_id, namespace)
        if not batchsandbox:
            raise Exception(f"BatchSandbox for sandbox {sandbox_id} not found")

        self.custom_api.delete_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            name=batchsandbox["metadata"]["name"],
            grace_period_seconds=10,
        )

        # Evict from informer cache so subsequent reads don't return stale data
        informer = self._get_informer(namespace)
        if informer:
            try:
                informer.remove_from_cache(sandbox_id)
            except Exception:
                pass  # best-effort

        # Best-effort cleanup: try each independently so one failure
        # doesn't prevent the others from running.
        errors = []

        try:
            self._delete_sandbox_data_pvc(sandbox_id, namespace)
        except Exception as e:
            logger.warning(f"Failed to delete PVC for sandbox {sandbox_id}: {e}")
            errors.append(e)

        try:
            self._delete_execd_token_secret(sandbox_id, namespace)
        except Exception as e:
            logger.warning(f"Failed to delete execd token secret for sandbox {sandbox_id}: {e}")
            errors.append(e)

        try:
            self._delete_egress_token_secret(sandbox_id, namespace)
        except Exception as e:
            logger.warning(f"Failed to delete egress token secret for sandbox {sandbox_id}: {e}")
            errors.append(e)

        if self.snapshot_manager:
            try:
                self.snapshot_manager.delete_snapshots(sandbox_id, namespace)
            except Exception as e:
                logger.warning(f"Failed to delete snapshots for sandbox {sandbox_id}: {e}")
                errors.append(e)

        if errors:
            logger.warning(
                f"Sandbox {sandbox_id} CR deleted but {len(errors)} cleanup "
                f"error(s) occurred: {errors}"
            )

    def pause_workload(self, sandbox_id: str, namespace: str) -> None:
        """Pause by scaling replicas to 0 and setting paused annotation.

        Pause is instant: the pod is scaled down immediately. If snapshot
        support is enabled, a background thread creates a VolumeSnapshot of
        the PVC (non-blocking). The PVC is kept alive for fast resume; it
        will be deleted later by the archive sweep once the snapshot is ready
        and ``archive_after_seconds`` has elapsed.
        """
        batchsandbox = self.get_workload(sandbox_id, namespace)
        if not batchsandbox:
            raise Exception(f"BatchSandbox for sandbox {sandbox_id} not found")

        pvc_name = f"{sandbox_id}-sandbox-data"

        # 1. Scale down immediately — instant pause
        body = {
            "metadata": {
                "annotations": {
                    ANNOTATION_PAUSED: "true",
                }
            },
            "spec": {
                "replicas": 0,
            },
        }

        updated = self.custom_api.patch_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            name=batchsandbox["metadata"]["name"],
            body=body,
        )

        # Update informer cache so subsequent calls within the same lock
        # see the new state immediately (avoids stale-cache TOCTOU issues).
        informer = self._get_informer(namespace)
        if informer and updated:
            informer.update_cache(updated)

        logger.info(f"Paused sandbox {sandbox_id} (replicas set to 0)")

        # 2. Background snapshot (non-blocking) — PVC stays until archive phase
        if self.snapshot_manager:
            pvc_exists = self._pvc_exists(pvc_name, namespace)
            if pvc_exists:
                labels = batchsandbox.get("metadata", {}).get("labels", {})
                thread = threading.Thread(
                    target=self._background_snapshot,
                    args=(sandbox_id, pvc_name, namespace, labels),
                    daemon=True,
                )
                thread.start()
            else:
                logger.warning(
                    f"PVC {pvc_name} not found for sandbox {sandbox_id}, "
                    "skipping snapshot on pause"
                )

            # Lazily start archive sweep for this namespace
            if self.archive_after_seconds > 0:
                self._ensure_archive_sweep(namespace)

    def _background_snapshot(
        self,
        sandbox_id: str,
        pvc_name: str,
        namespace: str,
        labels: Dict[str, str],
    ) -> None:
        """Create snapshot in background. PVC stays until archive phase."""
        with self._snapshot_lock:
            if sandbox_id in self._snapshot_in_progress:
                logger.debug(
                    f"Snapshot already in progress for {sandbox_id}, skipping"
                )
                return
            self._snapshot_in_progress.add(sandbox_id)
        try:
            # Re-check that the sandbox is still paused under the per-sandbox
            # lock to avoid snapshotting a PVC that is being resumed.
            if self._get_sandbox_lock_fn:
                with self._get_sandbox_lock_fn(sandbox_id):
                    workload = self.get_workload(sandbox_id, namespace)
                    if not workload:
                        return
                    annotations = workload.get("metadata", {}).get("annotations", {})
                    if annotations.get(ANNOTATION_PAUSED) != "true":
                        logger.debug(
                            f"Sandbox {sandbox_id} no longer paused, skipping snapshot"
                        )
                        return

            snap_name = self.snapshot_manager.create_snapshot(
                sandbox_id, pvc_name, namespace, labels
            )
            self.snapshot_manager.wait_for_snapshot_ready(snap_name, namespace)
            self.snapshot_manager.enforce_retention(sandbox_id, namespace)
            # Annotate sandbox with snapshot-ready timestamp for archive phase
            self._annotate_snapshot_ready(sandbox_id, namespace)
            logger.info(f"Background snapshot ready for {sandbox_id}")

            # If archive_after_seconds == 0, delegate to _archive_single_pvc
            # which handles locking and TOCTOU re-checking.
            if self.archive_after_seconds == 0:
                self._archive_single_pvc(sandbox_id, namespace, age=0)
        except Exception:
            logger.warning(
                f"Background snapshot failed for {sandbox_id}, PVC preserved",
                exc_info=True,
            )
        finally:
            with self._snapshot_lock:
                self._snapshot_in_progress.discard(sandbox_id)

    def _annotate_snapshot_ready(self, sandbox_id: str, namespace: str) -> None:
        """Mark sandbox with snapshot-ready timestamp for archive GC."""
        body = {
            "metadata": {
                "annotations": {
                    ANNOTATION_SNAPSHOT_READY_AT: (
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
                    ),
                }
            }
        }
        batchsandbox = self.get_workload(sandbox_id, namespace)
        if batchsandbox:
            try:
                self.custom_api.patch_namespaced_custom_object(
                    group=self.group,
                    version=self.version,
                    namespace=namespace,
                    plural=self.plural,
                    name=batchsandbox["metadata"]["name"],
                    body=body,
                )
            except Exception:
                logger.warning(
                    f"Failed to annotate snapshot-ready for {sandbox_id}",
                    exc_info=True,
                )

    def _list_paused_sandboxes_with_snapshot_ready(
        self, namespace: str
    ) -> List[Tuple[str, datetime]]:
        """List paused sandboxes that have a snapshot-ready-at annotation.

        Returns:
            List of (sandbox_id, snapshot_ready_at) tuples.
        """
        results: List[Tuple[str, datetime]] = []
        try:
            items = self.custom_api.list_namespaced_custom_object(
                group=self.group,
                version=self.version,
                namespace=namespace,
                plural=self.plural,
            ).get("items", [])
        except Exception:
            logger.warning("Failed to list sandboxes for archive sweep", exc_info=True)
            return results

        for item in items:
            annotations = item.get("metadata", {}).get("annotations", {}) or {}
            paused = annotations.get(ANNOTATION_PAUSED)
            snapshot_ready_str = annotations.get(
                ANNOTATION_SNAPSHOT_READY_AT
            )
            if paused != "true" or not snapshot_ready_str:
                continue
            labels = item.get("metadata", {}).get("labels", {})
            sandbox_id = labels.get(LABEL_SANDBOX_ID) or item.get("metadata", {}).get("name")
            if not sandbox_id:
                continue
            try:
                snapshot_ready_at = datetime.fromisoformat(
                    snapshot_ready_str.rstrip("Z")
                )
            except (ValueError, TypeError):
                continue
            results.append((sandbox_id, snapshot_ready_at))
        return results

    def archive_stale_pvcs(self, namespace: str) -> None:
        """Delete PVCs for paused sandboxes whose snapshots are ready and old enough."""
        paused = self._list_paused_sandboxes_with_snapshot_ready(namespace)
        # Parsed timestamps are naive (no tz); make `now` naive too for subtraction.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for sandbox_id, snapshot_ready_at in paused:
            age = (now - snapshot_ready_at).total_seconds()
            if age >= self.archive_after_seconds:
                self._archive_single_pvc(sandbox_id, namespace, age)

    def _archive_single_pvc(
        self, sandbox_id: str, namespace: str, age: float
    ) -> None:
        """Delete a single sandbox PVC under the per-sandbox lock.

        Re-reads the CR under the lock to prevent TOCTOU races with
        concurrent resume_workload calls.
        """
        ctx = self._get_sandbox_lock_fn(sandbox_id) if self._get_sandbox_lock_fn else nullcontext()
        with ctx:
            # Re-check that the sandbox is still paused (prevents race with resume)
            workload = self.get_workload(sandbox_id, namespace)
            if not workload:
                return
            annotations = workload.get("metadata", {}).get("annotations", {})
            if annotations.get(ANNOTATION_PAUSED) != "true":
                logger.debug(
                    f"Sandbox {sandbox_id} no longer paused, skipping archive"
                )
                return

            pvc_name = f"{sandbox_id}-sandbox-data"
            if self._pvc_exists(pvc_name, namespace):
                self._delete_sandbox_data_pvc(sandbox_id, namespace)
                logger.info(
                    f"Archived sandbox {sandbox_id}: PVC deleted "
                    f"(snapshot ready {age:.0f}s ago)"
                )

    def _reconcile_missing_snapshots(self, namespace: str) -> None:
        """Detect paused sandboxes without a snapshot and trigger one.

        The K8s controller pauses expired sandboxes by setting paused=true +
        replicas=0 but never creates a VolumeSnapshot.  This reconciliation
        loop catches those sandboxes and creates the missing snapshot so that
        the PVC can later be safely archived.
        """
        if not self.snapshot_manager:
            return

        try:
            items = self.custom_api.list_namespaced_custom_object(
                group=self.group,
                version=self.version,
                namespace=namespace,
                plural=self.plural,
            ).get("items", [])
        except Exception:
            logger.warning(
                "Failed to list sandboxes for snapshot reconciliation",
                exc_info=True,
            )
            return

        for item in items:
            annotations = item.get("metadata", {}).get("annotations", {}) or {}
            if annotations.get(ANNOTATION_PAUSED) != "true":
                continue
            if annotations.get(ANNOTATION_SNAPSHOT_READY_AT):
                continue

            labels = item.get("metadata", {}).get("labels", {})
            sandbox_id = labels.get(LABEL_SANDBOX_ID) or item.get("metadata", {}).get("name")
            if not sandbox_id:
                continue

            pvc_name = f"{sandbox_id}-sandbox-data"
            if not self._pvc_exists(pvc_name, namespace):
                continue

            # Skip if snapshot is already in progress (avoids noisy repeat logs)
            with self._snapshot_lock:
                if sandbox_id in self._snapshot_in_progress:
                    continue

            logger.info(
                f"Reconciling missing snapshot for controller-paused sandbox {sandbox_id}"
            )
            thread = threading.Thread(
                target=self._background_snapshot,
                args=(sandbox_id, pvc_name, namespace, labels),
                daemon=True,
            )
            thread.start()

    def _ensure_archive_sweep(self, namespace: str) -> None:
        """Lazily start the archive sweep thread for a namespace (once)."""
        with self._archive_sweep_lock:
            if namespace in self._archive_sweep_namespaces:
                return
            self._archive_sweep_namespaces.add(namespace)
        self._start_archive_sweep(namespace)

    def _start_archive_sweep(self, namespace: str) -> None:
        """Start a background thread that periodically archives stale PVCs."""
        def sweep_loop() -> None:
            while True:
                time.sleep(300)  # every 5 minutes
                try:
                    self._reconcile_missing_snapshots(namespace)
                except Exception:
                    logger.warning("Snapshot reconciliation failed", exc_info=True)
                try:
                    self.archive_stale_pvcs(namespace)
                except Exception:
                    logger.warning("Archive sweep failed", exc_info=True)

        t = threading.Thread(target=sweep_loop, daemon=True)
        t.start()
        logger.info(
            f"Archive sweep started (interval=300s, "
            f"archive_after_seconds={self.archive_after_seconds})"
        )

    @property
    def image_config_hash(self) -> Optional[str]:
        """Return the cached image config hash."""
        return self._image_config_hash

    @staticmethod
    def _egress_liveness_probe_dict() -> Dict[str, Any]:
        """Return the egress healthcheck liveness probe as a dict for CRD patching."""
        return {
            "exec": {
                "command": ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:18080/healthz"],
            },
            "initialDelaySeconds": 5,
            "periodSeconds": 5,
            "failureThreshold": 3,
            "timeoutSeconds": 2,
        }

    def _get_template_sidecar_images(self) -> Dict[str, str]:
        """Return {container_name: image} for non-sandbox containers from template."""
        _, _, extra_containers, _, _ = self._extract_template_pod_extras()
        return {c["name"]: c["image"] for c in extra_containers if c.get("name") and c.get("image")}

    def _get_template_sandbox_resources(self) -> Dict[str, Any]:
        """Return resource requirements from the sandbox container in the template."""
        _, _, _, _, extra_resources = self._extract_template_pod_extras()
        return extra_resources

    def _get_template_sandbox_image(self) -> Optional[str]:
        """Return the image from the sandbox container in the template, if any."""
        template = self.template_manager.get_base_template()
        spec = template.get("spec", {}) if isinstance(template, dict) else {}
        containers = spec.get("template", {}).get("spec", {}).get("containers", []) or []
        for c in containers:
            if c.get("name") == "sandbox":
                return c.get("image")
        return None

    # Pod-level spec fields that should be re-applied on resume so that
    # sandboxes created before a template change pick up the new values.
    _POD_SPEC_OVERRIDE_KEYS = ("dnsPolicy", "dnsConfig")

    def _get_template_pod_spec_overrides(self) -> Dict[str, Any]:
        """Return pod-level spec fields from the template that should be patched on resume."""
        template = self.template_manager.get_base_template()
        spec = template.get("spec", {}) if isinstance(template, dict) else {}
        template_pod_spec = spec.get("template", {}).get("spec", {})
        overrides: Dict[str, Any] = {}
        for key in self._POD_SPEC_OVERRIDE_KEYS:
            if key in template_pod_spec:
                overrides[key] = template_pod_spec[key]
        return overrides

    def _compute_image_config_hash(self) -> str:
        """Compute a short SHA256 hash of all operator-controlled images + egress enablement.

        The hash tracks image/structure changes only; policy content changes are
        handled by the PolicyPusher hot-reload mechanism.
        """
        parts: List[str] = []
        if self.execd_image:
            parts.append(f"execd={self.execd_image}")
        if self.egress_image:
            parts.append(f"egress={self.egress_image}")
        else:
            parts.append("egress=disabled")
        sidecar_images = self._get_template_sidecar_images()
        for name, image in sorted(sidecar_images.items()):
            parts.append(f"sidecar:{name}={image}")
        sandbox_resources = self._template_sandbox_resources
        if sandbox_resources:
            parts.append(f"sandbox-resources={json.dumps(sandbox_resources, sort_keys=True)}")
        if self._template_sandbox_image:
            parts.append(f"sandbox={self._template_sandbox_image}")
        digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
        return digest

    def resume_workload(
        self,
        sandbox_id: str,
        namespace: str,
        network_policy: Optional[NetworkPolicy] = None,
        egress_image: Optional[str] = None,
        upstream_dns: Optional[str] = None,
    ) -> None:
        """Resume by scaling replicas back to 1 and clearing paused annotation.

        Updates all operator-controlled images (execd, egress, template sidecars)
        and injects/removes the egress sidecar based on current network config.

        If snapshot support is enabled, restores the PVC from the latest
        VolumeSnapshot before scaling the pod back up.
        """
        batchsandbox = self.get_workload(sandbox_id, namespace)
        if not batchsandbox:
            raise Exception(f"BatchSandbox for sandbox {sandbox_id} not found")

        # Fall back to instance-level egress_image if not passed explicitly
        egress_image = egress_image or self.egress_image

        # If snapshots enabled: restore PVC from latest snapshot (or reuse existing PVC)
        if self.snapshot_manager:
            pvc_name = f"{sandbox_id}-sandbox-data"
            pvc_already_exists = self._pvc_exists(pvc_name, namespace)

            if pvc_already_exists:
                # TTL-pause path: PVC was kept alive, no snapshot restoration needed
                # Verify PVC is in a healthy state before reusing it
                core_v1 = self.k8s_client.get_core_v1_api()
                pvc = core_v1.read_namespaced_persistent_volume_claim(
                    name=pvc_name, namespace=namespace
                )
                phase = pvc.status.phase if pvc.status else None
                if phase != "Bound":
                    raise Exception(
                        f"PVC {pvc_name} exists but is in {phase} state. "
                        "Cannot safely resume sandbox."
                    )
                logger.info(
                    f"PVC {pvc_name} already exists for sandbox {sandbox_id}, "
                    "skipping snapshot restoration"
                )
            else:
                # Explicit pause path: PVC was deleted, restore from snapshot
                latest_snap = self.snapshot_manager.get_latest_snapshot(
                    sandbox_id, namespace
                )
                if not latest_snap:
                    raise Exception(
                        f"No VolumeSnapshot or existing PVC found for sandbox "
                        f"{sandbox_id}. Cannot restore storage."
                    )
                labels = batchsandbox.get("metadata", {}).get("labels", {})
                pvc_name = self.snapshot_manager.create_pvc_from_snapshot(
                    sandbox_id=sandbox_id,
                    snapshot_name=latest_snap,
                    namespace=namespace,
                    labels=labels,
                    storage_class=self.storage_class,
                    storage_size=self.storage_size,
                )
                # PVC binding is handled natively by K8s scheduler — the pod
                # will stay Pending until the PVC is bound, then start
                # automatically.  Blocking here caused HTTP timeouts on the
                # caller side and thread-pool exhaustion on the server.

        # Update execd-installer init container image to current version
        init_containers = batchsandbox.get("spec", {}).get("template", {}).get("spec", {}).get("initContainers", [])
        updated_init_containers = []
        for ic in init_containers:
            if ic.get("name") == "execd-installer" and self.execd_image:
                ic = {**ic, "image": self.execd_image}
            updated_init_containers.append(ic)

        # --- Egress sidecar inject/remove/update + template sidecar image updates ---
        containers = batchsandbox.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []
        has_egress_container = any(c.get("name") == "egress" for c in containers)
        updated_containers = []
        labels_patch: Dict[str, Any] = {}
        needs_egress_secret = False
        needs_egress_secret_cleanup = False
        egress_token: Optional[str] = None

        # Build liveness probe dict for sandbox container (used in Branch A)
        _liveness_probe_dict = self._egress_liveness_probe_dict()

        # Get current template sidecar images for updating non-sandbox, non-egress containers
        template_sidecar_images = self._template_sidecar_images

        for c in containers:
            name = c.get("name")

            if name == "sandbox":
                # Apply current template resources on resume
                template_resources = self._template_sandbox_resources
                if template_resources:
                    c = {**c, "resources": template_resources}

                # Upgrade sandbox image if it uses the same repo as the template (i.e. not a custom image)
                if self._template_sandbox_image:
                    current_repo = c.get("image", "").split(":")[0].split("@")[0]
                    template_repo = self._template_sandbox_image.split(":")[0].split("@")[0]
                    if current_repo == template_repo:
                        old_image = c.get("image", "")
                        c = {**c, "image": self._template_sandbox_image}
                        if old_image != self._template_sandbox_image:
                            logger.info(
                                "Upgrading sandbox image for %s: %s -> %s",
                                sandbox_id, old_image, self._template_sandbox_image,
                            )

                if not has_egress_container and network_policy and egress_image:
                    # Branch A: Inject egress sidecar — add NET_ADMIN to drop list
                    existing_sc = dict(c.get("securityContext") or {})
                    caps = dict(existing_sc.get("capabilities") or {})
                    drop_list = list(caps.get("drop") or [])
                    if "NET_ADMIN" not in drop_list:
                        drop_list.append("NET_ADMIN")
                    caps["drop"] = drop_list
                    existing_sc["capabilities"] = caps
                    c = {**c, "securityContext": existing_sc, "livenessProbe": _liveness_probe_dict}
                elif has_egress_container and network_policy is None:
                    # Branch B: Remove egress sidecar — remove NET_ADMIN from drop list
                    existing_sc = dict(c.get("securityContext") or {})
                    caps = dict(existing_sc.get("capabilities") or {})
                    drop_list = list(caps.get("drop") or [])
                    if "NET_ADMIN" in drop_list:
                        drop_list.remove("NET_ADMIN")
                    caps["drop"] = drop_list
                    existing_sc["capabilities"] = caps
                    c = {**c, "securityContext": existing_sc, "livenessProbe": None}
                # Branch C and no-change: leave sandbox container as-is
                updated_containers.append(c)

            elif name == "egress":
                if network_policy is None:
                    # Branch B: Remove egress sidecar — skip adding to updated_containers
                    # Secret deletion is deferred until after CR patch succeeds
                    labels_patch[LABEL_EGRESS_SIDECAR] = None
                    needs_egress_secret_cleanup = True
                    continue
                else:
                    # Branch C: Update egress image, upstream DNS, and policy
                    if egress_image:
                        c = {**c, "image": egress_image}
                    env_list = list(c.get("env", []))
                    # Update upstream DNS env var on existing egress container
                    if upstream_dns:
                        env_list = [
                            e for e in env_list
                            if e.get("name") != "OPENSANDBOX_EGRESS_UPSTREAM"
                        ]
                        env_list.append({
                            "name": "OPENSANDBOX_EGRESS_UPSTREAM",
                            "value": upstream_dns,
                        })
                    # Update egress policy env var so resumed sidecar gets current policy
                    if network_policy:
                        policy_payload = json.dumps(
                            network_policy.model_dump(by_alias=True, exclude_none=True)
                        )
                        env_list = [
                            e for e in env_list
                            if e.get("name") != "OPENSANDBOX_EGRESS_RULES"
                        ]
                        env_list.append({
                            "name": "OPENSANDBOX_EGRESS_RULES",
                            "value": policy_payload,
                        })
                    c = {**c, "env": env_list}
                    updated_containers.append(c)

            else:
                # Template sidecar containers — update image if changed;
                # drop containers no longer present in the current template
                # (e.g. dind removed after a template update).
                if name in template_sidecar_images:
                    current_image = template_sidecar_images[name]
                    if c.get("image") != current_image:
                        c = {**c, "image": current_image}
                    updated_containers.append(c)
                else:
                    logger.info(
                        f"Dropping container '{name}' from sandbox {sandbox_id} "
                        "-- no longer in template"
                    )

        # Branch A: Inject egress sidecar (sandbox had no egress container)
        if not has_egress_container and network_policy and egress_image:
            sidecar_spec, egress_token = build_egress_sidecar_container(
                egress_image=egress_image,
                network_policy=network_policy,
                upstream_dns=upstream_dns,
            )
            updated_containers.append(sidecar_spec)
            labels_patch[LABEL_EGRESS_SIDECAR] = "true"
            needs_egress_secret = True

        # --- Build patch body ---
        pod_spec_patch: Dict[str, Any] = {
            "initContainers": updated_init_containers,
            "containers": updated_containers,
            **self._template_pod_spec_overrides,
        }
        body: Dict[str, Any] = {
            "metadata": {
                "annotations": {
                    ANNOTATION_PAUSED: None,
                    # Clear snapshot-ready timestamp to prevent re-archiving
                    ANNOTATION_SNAPSHOT_READY_AT: None,
                    ANNOTATION_IMAGE_CONFIG_HASH: self._image_config_hash,
                },
            },
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": pod_spec_patch,
                },
            },
        }

        if labels_patch:
            body["metadata"]["labels"] = labels_patch

        updated = self.custom_api.patch_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            name=batchsandbox["metadata"]["name"],
            body=body,
        )

        # Create egress token secret AFTER CR patch succeeds (Branch A only)
        if needs_egress_secret and egress_token:
            cr_labels = batchsandbox.get("metadata", {}).get("labels", {})
            self._create_egress_token_secret(sandbox_id, namespace, cr_labels, egress_token)

        # Delete egress token secret AFTER CR patch succeeds (Branch B only)
        if needs_egress_secret_cleanup:
            try:
                self._delete_egress_token_secret(sandbox_id, namespace)
            except Exception as e:
                logger.warning(
                    "Failed to delete egress token secret on egress removal for %s: %s",
                    sandbox_id, e,
                )

        # Update informer cache so subsequent calls within the same lock
        # see the new state immediately (avoids stale-cache TOCTOU issues).
        informer = self._get_informer(namespace)
        if informer and updated:
            informer.update_cache(updated)

        logger.info(f"Resumed sandbox {sandbox_id} (replicas set to 1)")

    def list_workloads(self, namespace: str, label_selector: str) -> List[Dict[str, Any]]:
        """List BatchSandboxes matching label selector."""
        try:
            batchsandbox_list = self.custom_api.list_namespaced_custom_object(
                group=self.group,
                version=self.version,
                namespace=namespace,
                plural=self.plural,
                label_selector=label_selector,
            )
            return batchsandbox_list.get("items", [])
        except ApiException as e:
            # Handle 404 when CRD doesn't exist
            if e.status == 404:
                return []
            # Re-raise other API exceptions
            raise
        except Exception as e:
            # Log and re-raise unexpected errors
            logger.error(f"Unexpected error listing BatchSandboxes: {e}")
            raise

    def update_expiration(self, sandbox_id: str, namespace: str, expires_at: datetime) -> None:
        """Update BatchSandbox expiration time.

        Args:
            sandbox_id: Sandbox ID
            namespace: Kubernetes namespace
            expires_at: New expiration time

        Raises:
            Exception: If BatchSandbox not found or update fails
        """
        batchsandbox = self.get_workload(sandbox_id, namespace)
        if not batchsandbox:
            raise Exception(f"BatchSandbox for sandbox {sandbox_id} not found")

        # Patch BatchSandbox spec.expireTime
        body = {
            "spec": {
                "expireTime": expires_at.isoformat()
            }
        }

        self.custom_api.patch_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            name=batchsandbox["metadata"]["name"],
            body=body,
        )

    def get_expiration(self, workload: Dict[str, Any]) -> Optional[datetime]:
        """Get expiration time from BatchSandbox.

        Args:
            workload: BatchSandbox dict

        Returns:
            Expiration datetime or None if not set or invalid
        """
        spec = workload.get("spec", {})
        expire_time_str = spec.get("expireTime")

        if not expire_time_str:
            return None

        try:
            # Parse ISO format datetime
            return datetime.fromisoformat(expire_time_str.replace('Z', '+00:00'))
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid expireTime format: {expire_time_str}, error: {e}")
            return None

    def get_status(self, workload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get status from BatchSandbox.

        The status is derived from the BatchSandbox status fields:
        - replicas: total number of pods
        - allocated: number of scheduled pods
        - ready: number of ready pods
        """
        spec = workload.get("spec", {})
        annotations = workload.get("metadata", {}).get("annotations", {})
        creation_timestamp = workload.get("metadata", {}).get("creationTimestamp")

        # Check if sandbox is paused (annotation + replicas == 0)
        is_paused = annotations.get(ANNOTATION_PAUSED) == "true"
        desired_replicas = spec.get("replicas", 1)
        if is_paused and desired_replicas == 0:
            return {
                "state": "Paused",
                "reason": "SANDBOX_PAUSED",
                "message": "Sandbox is paused (storage preserved)",
                "last_transition_at": creation_timestamp,
            }

        status = workload.get("status", {})

        replicas = status.get("replicas", 0)
        ready = status.get("ready", 0)
        allocated = status.get("allocated", 0)
        endpoints_str = annotations.get(ANNOTATION_ENDPOINTS)

        # Determine state based on ready status and endpoint availability
        if ready == 1 and endpoints_str:
            # Pod is ready and has an IP address assigned
            state = "Running"
            reason = "READY_WITH_IP"
            message = f"Pod is ready with IP assigned ({ready}/{replicas} ready)"
        elif ready > 0:
            # Pod is ready but no IP yet - still pending
            state = "Pending"
            reason = "POD_READY_NO_IP"
            message = f"Pod is ready but waiting for IP assignment ({ready}/{replicas} ready)"
        elif allocated > 0:
            # Pod is allocated/scheduled but not ready yet
            state = "Pending"
            reason = "POD_SCHEDULED"
            message = f"Pod is scheduled but not ready ({allocated}/{replicas} allocated, {ready} ready)"
        else:
            # Pod is not allocated yet
            state = "Pending"
            reason = "BATCHSANDBOX_PENDING"
            message = "BatchSandbox is pending allocation"

        return {
            "state": state,
            "reason": reason,
            "message": message,
            "last_transition_at": creation_timestamp,
        }

    def _create_execd_token_secret(
        self,
        sandbox_id: str,
        namespace: str,
        labels: Dict[str, str],
        token: str,
    ) -> str:
        """Create a K8s Secret to store the execd access token."""
        secret_name = f"{sandbox_id}-execd-token"
        core_v1 = self.k8s_client.get_core_v1_api()

        secret_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "labels": {
                    **labels,
                    LABEL_PURPOSE: "execd-token",
                },
            },
            "stringData": {
                "token": token,
            },
        }

        core_v1.create_namespaced_secret(
            namespace=namespace,
            body=secret_body,
        )
        logger.info("Created execd token secret %s for sandbox %s", secret_name, sandbox_id)
        return secret_name

    def _delete_execd_token_secret(self, sandbox_id: str, namespace: str) -> None:
        """Delete the execd token Secret if it exists (best-effort)."""
        secret_name = f"{sandbox_id}-execd-token"
        core_v1 = self.k8s_client.get_core_v1_api()
        try:
            core_v1.delete_namespaced_secret(
                name=secret_name,
                namespace=namespace,
            )
            logger.info("Deleted execd token secret %s for sandbox %s", secret_name, sandbox_id)
        except ApiException as e:
            if e.status == 404:
                logger.debug("Execd token secret %s not found, skipping cleanup", secret_name)
            else:
                logger.warning("Failed to delete execd token secret %s: %s", secret_name, e)

    def _create_egress_token_secret(
        self,
        sandbox_id: str,
        namespace: str,
        labels: Dict[str, str],
        token: str,
    ) -> str:
        """Create a K8s Secret to store the egress auth token."""
        secret_name = f"{sandbox_id}-egress-token"
        core_v1 = self.k8s_client.get_core_v1_api()

        secret_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "labels": {
                    **labels,
                    LABEL_PURPOSE: "egress-token",
                },
            },
            "stringData": {
                "token": token,
            },
        }

        core_v1.create_namespaced_secret(
            namespace=namespace,
            body=secret_body,
        )
        logger.info("Created egress token secret %s for sandbox %s", secret_name, sandbox_id)
        return secret_name

    def _delete_egress_token_secret(self, sandbox_id: str, namespace: str) -> None:
        """Delete the egress token Secret if it exists (best-effort)."""
        secret_name = f"{sandbox_id}-egress-token"
        core_v1 = self.k8s_client.get_core_v1_api()
        try:
            core_v1.delete_namespaced_secret(
                name=secret_name,
                namespace=namespace,
            )
            logger.info("Deleted egress token secret %s for sandbox %s", secret_name, sandbox_id)
        except ApiException as e:
            if e.status == 404:
                logger.debug("Egress token secret %s not found, skipping cleanup", secret_name)
            else:
                logger.warning("Failed to delete egress token secret %s: %s", secret_name, e)

    def _create_sandbox_data_pvc(
        self,
        sandbox_id: str,
        namespace: str,
        labels: Dict[str, str],
        storage_class: Optional[str] = None,
        storage_size: str = "20Gi",
    ) -> str:
        """Create a PVC for sandbox data that persists across pause/resume."""
        pvc_name = f"{sandbox_id}-sandbox-data"
        core_v1 = self.k8s_client.get_core_v1_api()

        pvc_body = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc_name,
                "namespace": namespace,
                "labels": {
                    **labels,
                    LABEL_PURPOSE: "sandbox-data",
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {
                        "storage": storage_size,
                    }
                },
            },
        }
        if storage_class:
            pvc_body["spec"]["storageClassName"] = storage_class

        core_v1.create_namespaced_persistent_volume_claim(
            namespace=namespace,
            body=pvc_body,
        )
        logger.info(f"Created PVC {pvc_name} for sandbox {sandbox_id}")
        return pvc_name

    def _replace_sandbox_volumes_with_pvc(
        self, batchsandbox: Dict[str, Any], pvc_name: str
    ) -> None:
        """Replace docker-data emptyDir volume with PVC reference in the pod spec.

        The ``docker-data`` volume name is kept for backward compatibility with
        existing PVCs.  When ``filesystem_persistence`` is enabled, volume mounts
        are updated to use a ``subPath`` so that the PVC is partitioned into
        subdirectories (``overlay-upper/``, ``overlay-work/``, etc.).
        """
        try:
            spec = batchsandbox["spec"]["template"]["spec"]
            volumes = spec["volumes"]
        except KeyError:
            return

        for vol in volumes:
            if vol.get("name") == "docker-data" and "emptyDir" in vol:
                del vol["emptyDir"]
                vol["persistentVolumeClaim"] = {"claimName": pvc_name}
                break

        if not self.filesystem_persistence:
            return

        # When filesystem_persistence is enabled, update existing docker-data
        # mounts to use a subPath so overlay dirs get their own space on the PVC.
        for container in spec.get("containers", []):
            for mount in container.get("volumeMounts", []):
                if mount.get("name") == "docker-data" and "subPath" not in mount:
                    mount["subPath"] = "docker-data"

    def _apply_overlay_config(self, batchsandbox: Dict[str, Any]) -> None:
        """Configure the sandbox container for OverlayFS filesystem persistence.

        Adds CAP_SYS_ADMIN capability, OVERLAY_PERSIST=1 env var, and a
        volumeMount for the full PVC at /mnt/sandbox-data so bootstrap.sh
        can set up the overlay upper/work directories.
        """
        try:
            spec = batchsandbox["spec"]["template"]["spec"]
            containers = spec["containers"]
        except KeyError:
            return

        # Find the sandbox container (first container or named "sandbox")
        sandbox_container = None
        for c in containers:
            if c.get("name") == "sandbox":
                sandbox_container = c
                break
        if sandbox_container is None and containers:
            sandbox_container = containers[0]
        if sandbox_container is None:
            return

        # Configure capabilities for overlay setup.
        # Drop ALL default capabilities, then add back only the minimal set:
        #   SYS_ADMIN  – overlay mount, pivot_root
        #   CHOWN      – chown hardening in bootstrap.sh
        #   DAC_OVERRIDE – file access across ownership boundaries during overlay
        #   FOWNER     – chmod on files owned by other users
        #   SETUID/SETGID – setpriv privilege drop to SANDBOX_USER
        #   SETPCAP    – capsh to drop SYS_ADMIN from bounding set
        #   KILL       – signal processes (needed for normal process management)
        # bootstrap.sh drops SYS_ADMIN from the bounding set after overlay setup.
        sec_ctx = sandbox_container.setdefault("securityContext", {})
        caps = sec_ctx.setdefault("capabilities", {})
        drop_list = caps.setdefault("drop", [])
        if "ALL" not in drop_list:
            drop_list.append("ALL")
        add_list = caps.setdefault("add", [])
        for cap in ["SYS_ADMIN", "CHOWN", "DAC_OVERRIDE", "FOWNER",
                     "SETUID", "SETGID", "SETPCAP", "KILL"]:
            if cap not in add_list:
                add_list.append(cap)
        # OverlayFS mount + pivot_root require root; bootstrap.sh will
        # drop back to SANDBOX_USER after overlay setup if set.
        sec_ctx["runAsUser"] = 0

        # Add OVERLAY_PERSIST env var
        env = sandbox_container.setdefault("env", [])
        if not any(e.get("name") == "OVERLAY_PERSIST" for e in env):
            env.append({"name": "OVERLAY_PERSIST", "value": "1"})

        # Set SANDBOX_USER so bootstrap.sh drops privileges after overlay setup
        if self.sandbox_user and not any(e.get("name") == "SANDBOX_USER" for e in env):
            env.append({"name": "SANDBOX_USER", "value": self.sandbox_user})
            if self.sandbox_user != "root":
                logger.info(
                    "sandbox_user='%s' will be used for privilege drop. "
                    "Ensure this user exists in the sandbox image or bootstrap.sh "
                    "will auto-create it at startup.",
                    self.sandbox_user,
                )

        # Mount full PVC at /mnt/sandbox-data (for overlay-upper and overlay-work)
        mounts = sandbox_container.setdefault("volumeMounts", [])
        if not any(m.get("mountPath") == "/mnt/sandbox-data" for m in mounts):
            mounts.append({
                "name": "docker-data",
                "mountPath": "/mnt/sandbox-data",
            })

    def _pvc_exists(self, pvc_name: str, namespace: str) -> bool:
        """Check whether a PVC exists in the given namespace."""
        core_v1 = self.k8s_client.get_core_v1_api()
        try:
            core_v1.read_namespaced_persistent_volume_claim(
                name=pvc_name, namespace=namespace
            )
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def _delete_sandbox_data_pvc(self, sandbox_id: str, namespace: str) -> None:
        """Delete the sandbox-data PVC if it exists.

        Non-404 errors are intentionally logged and swallowed (best-effort).
        Callers (pause_workload, delete_workload) handle this gracefully:
        an orphaned PVC is acceptable and will be cleaned up on sandbox deletion.
        """
        pvc_name = f"{sandbox_id}-sandbox-data"
        core_v1 = self.k8s_client.get_core_v1_api()
        try:
            core_v1.delete_namespaced_persistent_volume_claim(
                name=pvc_name,
                namespace=namespace,
            )
            logger.info(f"Deleted PVC {pvc_name} for sandbox {sandbox_id}")
        except ApiException as e:
            if e.status == 404:
                logger.debug(f"PVC {pvc_name} not found, skipping cleanup")
            else:
                logger.warning(f"Failed to delete PVC {pvc_name}: {e}")

    def get_endpoint_info(self, workload: Dict[str, Any], port: int, sandbox_id: str) -> Optional[Endpoint]:
        """
        Get endpoint information from BatchSandbox.
        - gateway mode: use ingress config to format endpoint
        - direct/default: resolve Pod IP from annotation
        """
        import json

        if self.ingress_config and self.ingress_config.mode == INGRESS_MODE_GATEWAY:
            return format_ingress_endpoint(self.ingress_config, sandbox_id, port)

        annotations = workload.get("metadata", {}).get("annotations", {})

        # Get endpoints from annotation
        endpoints_str = annotations.get(ANNOTATION_ENDPOINTS)
        if not endpoints_str:
            return None

        try:
            # Parse JSON array of IPs
            endpoints = json.loads(endpoints_str)
            if endpoints and len(endpoints) > 0:
                # Use the first IP
                pod_ip = endpoints[0]
                return Endpoint(endpoint=f"{pod_ip}:{port}")
        except (json.JSONDecodeError, IndexError, TypeError):
            return None

        return None
