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
Agent-sandbox workload provider implementation.
"""

import base64
import logging
import secrets
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable
from threading import Lock

from kubernetes.client import (
    V1Container,
    V1EnvVar,
    V1ResourceRequirements,
    V1VolumeMount,
    ApiException,
)

from src.config import IngressConfig
from src.services.helpers import format_ingress_endpoint
from src.api.schema import Endpoint, ImageSpec, NetworkPolicy
from src.services.constants import LABEL_EGRESS_SIDECAR, LABEL_PURPOSE
from src.services.k8s.agent_sandbox_template import AgentSandboxTemplateManager
from src.services.k8s.client import K8sClient
from src.services.k8s.egress_helper import (
    apply_egress_to_spec,
    build_security_context_for_sandbox_container,
    build_security_context_from_dict,
    serialize_security_context_to_dict,
)
from src.services.k8s.informer import WorkloadInformer
from src.services.k8s.pod_failure import detect_pod_failure
from src.services.k8s.workload_provider import WorkloadProvider

logger = logging.getLogger(__name__)


class AgentSandboxProvider(WorkloadProvider):
    """
    Workload provider using kubernetes-sigs/agent-sandbox Sandbox CRD.
    """

    def __init__(
        self,
        k8s_client: K8sClient,
        template_file_path: Optional[str] = None,
        shutdown_policy: str = "Delete",
        service_account: Optional[str] = None,
        ingress_config: Optional[IngressConfig] = None,
        enable_informer: bool = True,
        informer_factory: Optional[Callable[[str], WorkloadInformer]] = None,
        informer_resync_seconds: int = 300,
        informer_watch_timeout_seconds: int = 60,
    ):
        self.k8s_client = k8s_client
        self.custom_api = k8s_client.get_custom_objects_api()
        self.core_api = k8s_client.get_core_v1_api()

        self.group = "agents.x-k8s.io"
        self.version = "v1alpha1"
        self.plural = "sandboxes"

        self.shutdown_policy = shutdown_policy
        self.service_account = service_account
        self.template_manager = AgentSandboxTemplateManager(template_file_path)
        self.ingress_config = ingress_config
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
        # Generate per-sandbox execd access token
        execd_token = secrets.token_hex(32)

        pod_spec, egress_token = self._build_pod_spec(
            image_spec=image_spec,
            entrypoint=entrypoint,
            env=env,
            resource_limits=resource_limits,
            execd_image=execd_image,
            network_policy=network_policy,
            egress_image=egress_image,
            execd_token=execd_token,
            upstream_dns=upstream_dns,
        )

        if network_policy and egress_image:
            labels[LABEL_EGRESS_SIDECAR] = "true"

        if self.service_account:
            pod_spec["serviceAccountName"] = self.service_account

        # Store execd token in a dedicated K8s Secret (not in CR annotation)
        self._create_execd_token_secret(sandbox_id, namespace, labels, execd_token)

        # Store egress token in a dedicated K8s Secret for hot-reload policy push
        if egress_token:
            self._create_egress_token_secret(sandbox_id, namespace, labels, egress_token)

        runtime_manifest = {
            "apiVersion": f"{self.group}/{self.version}",
            "kind": "Sandbox",
            "metadata": {
                "name": sandbox_id,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "shutdownTime": expires_at.isoformat(),
                "shutdownPolicy": self.shutdown_policy,
                "podTemplate": {
                    "metadata": {
                        "labels": labels,
                    },
                    "spec": pod_spec,
                },
            },
        }

        sandbox = self.template_manager.merge_with_runtime_values(runtime_manifest)

        created = self.custom_api.create_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            body=sandbox,
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

    def _build_pod_spec(
        self,
        image_spec: ImageSpec,
        entrypoint: List[str],
        env: Dict[str, str],
        resource_limits: Dict[str, str],
        execd_image: str,
        network_policy: Optional[NetworkPolicy] = None,
        egress_image: Optional[str] = None,
        execd_token: Optional[str] = None,
        upstream_dns: Optional[str] = None,
    ) -> Dict[str, Any]:
        init_container = self._build_execd_init_container(execd_image)
        main_container = self._build_main_container(
            image_spec=image_spec,
            entrypoint=entrypoint,
            env=env,
            resource_limits=resource_limits,
            include_execd_volume=True,
            has_network_policy=network_policy is not None,
            execd_token=execd_token,
        )

        containers = [self._container_to_dict(main_container)]

        # Build base pod spec
        pod_spec: Dict[str, Any] = {
            "initContainers": [self._container_to_dict(init_container)],
            "containers": containers,
            "volumes": [
                {
                    "name": "opensandbox-bin",
                    "emptyDir": {},
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

        return pod_spec, egress_token

    def _build_execd_init_container(self, execd_image: str) -> V1Container:
        # Set 755 permissions so only root can modify (not world-writable)
        script = (
            "cp ./execd /opt/opensandbox/bin/execd && "
            "cp ./bootstrap.sh /opt/opensandbox/bin/bootstrap.sh && "
            "chmod 755 /opt/opensandbox/bin/execd && "
            "chmod 755 /opt/opensandbox/bin/bootstrap.sh && "
            "chmod 755 /opt/opensandbox/bin"
        )

        return V1Container(
            name="execd-installer",
            image=execd_image,
            command=["/bin/sh", "-c"],
            args=[script],
            volume_mounts=[
                V1VolumeMount(
                    name="opensandbox-bin",
                    mount_path="/opt/opensandbox/bin",
                )
            ],
        )

    def _build_main_container(
        self,
        image_spec: ImageSpec,
        entrypoint: List[str],
        env: Dict[str, str],
        resource_limits: Dict[str, str],
        include_execd_volume: bool,
        has_network_policy: bool = False,
        execd_token: Optional[str] = None,
    ) -> V1Container:
        env_vars = [V1EnvVar(name=k, value=v) for k, v in env.items()]
        env_vars.append(V1EnvVar(name="EXECD", value="/opt/opensandbox/bin/execd"))

        if execd_token:
            env_vars.append(V1EnvVar(name="EXECD_ACCESS_TOKEN", value=execd_token))

        resources = None
        if resource_limits:
            resources = V1ResourceRequirements(
                limits=resource_limits,
                requests=resource_limits,
            )

        wrapped_command = ["/opt/opensandbox/bin/bootstrap.sh"] + entrypoint

        volume_mounts = None
        if include_execd_volume:
            volume_mounts = [
                V1VolumeMount(
                    name="opensandbox-bin",
                    mount_path="/opt/opensandbox/bin",
                )
            ]

        # Always apply security context
        security_context_dict = build_security_context_for_sandbox_container(
            has_network_policy=has_network_policy,
            is_overlay_mode=False,
        )
        security_context = build_security_context_from_dict(security_context_dict)

        return V1Container(
            name="sandbox",
            image=image_spec.uri,
            command=wrapped_command,
            env=env_vars if env_vars else None,
            resources=resources,
            volume_mounts=volume_mounts,
            security_context=security_context,
        )

    def _container_to_dict(self, container: V1Container) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "name": container.name,
            "image": container.image,
        }

        if container.command:
            result["command"] = container.command
        if container.args:
            result["args"] = container.args
        if container.env:
            result["env"] = [{"name": e.name, "value": e.value} for e in container.env]
        if container.resources:
            result["resources"] = {}
            if container.resources.limits:
                result["resources"]["limits"] = container.resources.limits
            if container.resources.requests:
                result["resources"]["requests"] = container.resources.requests
        if container.volume_mounts:
            result["volumeMounts"] = [
                {"name": vm.name, "mountPath": vm.mount_path}
                for vm in container.volume_mounts
            ]
        if container.security_context:
            security_context_dict = serialize_security_context_to_dict(container.security_context)
            if security_context_dict:
                result["securityContext"] = security_context_dict

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
                logger.error(f"Unexpected error getting Sandbox for {sandbox_id}: {e}")
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
                logger.error(f"Unexpected error getting Sandbox for {sandbox_id}: {e}")
                raise

        return None

    def delete_workload(self, sandbox_id: str, namespace: str) -> None:
        sandbox = self.get_workload(sandbox_id, namespace)
        if not sandbox:
            raise Exception(f"Sandbox for sandbox {sandbox_id} not found")

        self.custom_api.delete_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            name=sandbox["metadata"]["name"],
            grace_period_seconds=0,
        )

        # Evict from informer cache so subsequent reads don't return stale data
        informer = self._get_informer(namespace)
        if informer:
            try:
                informer.remove_from_cache(sandbox_id)
            except Exception:
                pass  # best-effort

        # Best-effort cleanup of execd token secret
        try:
            self._delete_execd_token_secret(sandbox_id, namespace)
        except Exception as e:
            logger.warning("Failed to delete execd token secret for sandbox %s: %s", sandbox_id, e)

        # Best-effort cleanup of egress token secret
        try:
            self._delete_egress_token_secret(sandbox_id, namespace)
        except Exception as e:
            logger.warning("Failed to delete egress token secret for sandbox %s: %s", sandbox_id, e)

    def list_workloads(self, namespace: str, label_selector: str) -> List[Dict[str, Any]]:
        try:
            sandbox_list = self.custom_api.list_namespaced_custom_object(
                group=self.group,
                version=self.version,
                namespace=namespace,
                plural=self.plural,
                label_selector=label_selector,
            )
            return sandbox_list.get("items", [])
        except ApiException as e:
            if e.status == 404:
                return []
            raise
        except Exception as e:
            logger.error(f"Unexpected error listing Sandboxes: {e}")
            raise

    def update_expiration(self, sandbox_id: str, namespace: str, expires_at: datetime) -> None:
        sandbox = self.get_workload(sandbox_id, namespace)
        if not sandbox:
            raise Exception(f"Sandbox for sandbox {sandbox_id} not found")

        body = {
            "spec": {
                "shutdownTime": expires_at.isoformat(),
            }
        }

        self.custom_api.patch_namespaced_custom_object(
            group=self.group,
            version=self.version,
            namespace=namespace,
            plural=self.plural,
            name=sandbox["metadata"]["name"],
            body=body,
        )

    def get_expiration(self, workload: Dict[str, Any]) -> Optional[datetime]:
        spec = workload.get("spec", {})
        shutdown_time_str = spec.get("shutdownTime")

        if not shutdown_time_str:
            return None

        try:
            return datetime.fromisoformat(shutdown_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid shutdownTime format: {shutdown_time_str}, error: {e}")
            return None

    def pause_workload(self, sandbox_id: str, namespace: str) -> None:
        """Pause is not supported for agent-sandbox workloads."""
        raise NotImplementedError("Pause operation is not supported for agent-sandbox workloads")

    def resume_workload(
        self,
        sandbox_id: str,
        namespace: str,
        network_policy: Optional[NetworkPolicy] = None,
        egress_image: Optional[str] = None,
        upstream_dns: Optional[str] = None,
    ) -> None:
        """Resume is not supported for agent-sandbox workloads."""
        raise NotImplementedError("Resume operation is not supported for agent-sandbox workloads")

    def get_status(self, workload: Dict[str, Any]) -> Dict[str, Any]:
        status = workload.get("status", {})
        conditions = status.get("conditions", [])

        ready_condition = None
        for condition in conditions:
            if condition.get("type") == "Ready":
                ready_condition = condition
                break

        creation_timestamp = workload.get("metadata", {}).get("creationTimestamp")

        if not ready_condition:
            pod_state = self._pod_state_from_selector(workload)
            if pod_state:
                state, reason, message = pod_state
                return {
                    "state": state,
                    "reason": reason,
                    "message": message,
                    "last_transition_at": creation_timestamp,
                }
            return {
                "state": "Pending",
                "reason": "SANDBOX_PENDING",
                "message": "Sandbox is pending scheduling",
                "last_transition_at": creation_timestamp,
            }

        cond_status = ready_condition.get("status")
        reason = ready_condition.get("reason")
        message = ready_condition.get("message")
        last_transition_at = ready_condition.get("lastTransitionTime") or creation_timestamp

        if cond_status == "True":
            state = "Running"
        elif reason == "SandboxExpired":
            state = "Terminated"
        elif cond_status == "False":
            state = "Pending"
        else:
            state = "Pending"

        return {
            "state": state,
            "reason": reason,
            "message": message,
            "last_transition_at": last_transition_at,
        }

    def _pod_state_from_selector(self, workload: Dict[str, Any]) -> Optional[tuple[str, str, str]]:
        status = workload.get("status", {})
        selector = status.get("selector")
        namespace = workload.get("metadata", {}).get("namespace")
        if not selector or not namespace:
            return None

        try:
            pods = self.core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=selector,
            ).items
        except Exception:
            return None

        for pod in pods:
            if pod.status and pod.status.phase == "Running":
                if pod.status.pod_ip:
                    return (
                        "Running",
                        "POD_READY",
                        "Pod is running with IP assigned",
                    )
                return (
                    "Pending",
                    "POD_READY_NO_IP",
                    "Pod is running but waiting for IP assignment",
                )

            failure = detect_pod_failure(pod)
            if failure:
                return failure

        if pods:
            return ("Pending", "POD_PENDING", "Pod is pending")

        return None

    def get_endpoint_info(self, workload: Dict[str, Any], port: int, sandbox_id: str) -> Optional[Endpoint]:
        # ingress-based endpoint if configured (gateway)
        ingress_endpoint = format_ingress_endpoint(self.ingress_config, sandbox_id, port)
        if ingress_endpoint:
            return ingress_endpoint

        status = workload.get("status", {})
        selector = status.get("selector")
        namespace = workload.get("metadata", {}).get("namespace")
        if selector and namespace:
            try:
                pods = self.core_api.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=selector,
                ).items
                for pod in pods:
                    if pod.status and pod.status.pod_ip and pod.status.phase == "Running":
                        return Endpoint(endpoint=f"{pod.status.pod_ip}:{port}")
            except Exception as e:
                logger.warning(f"Failed to resolve pod endpoint: {e}")

        service_fqdn = status.get("serviceFQDN")
        if service_fqdn:
            return Endpoint(endpoint=f"{service_fqdn}:{port}")

        return None
