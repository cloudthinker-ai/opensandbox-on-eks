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
Kubernetes-based implementation of SandboxService.

This module provides a Kubernetes implementation of the sandbox service interface,
using Kubernetes resources for sandbox lifecycle management.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from fastapi import HTTPException, status

from src.api.schema import (
    CreateSandboxRequest,
    CreateSandboxResponse,
    Endpoint,
    ListSandboxesRequest,
    ListSandboxesResponse,
    PaginationInfo,
    RenewSandboxExpirationRequest,
    RenewSandboxExpirationResponse,
    Sandbox,
    SandboxStatus,
)
from src.config import AppConfig, get_config
from src.services.constants import (
    ANNOTATION_IMAGE_CONFIG_HASH,
    ANNOTATION_PAUSED,
    LABEL_PREFIX,
    SANDBOX_ID_LABEL,
    SandboxErrorCodes,
)
from src.services.helpers import matches_filter
from src.services.sandbox_service import SandboxService
from src.services.validators import (
    ensure_entrypoint,
    ensure_egress_configured,
    ensure_future_expiration,
    ensure_metadata_labels,
)
from src.services.k8s.client import K8sClient
from src.services.k8s.pod_failure import detect_pod_failure
from src.services.k8s.provider_factory import create_workload_provider

logger = logging.getLogger(__name__)


class KubernetesSandboxService(SandboxService):
    """
    Kubernetes-based implementation of SandboxService.

    This class implements sandbox lifecycle operations using Kubernetes resources.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        """
        Initialize Kubernetes sandbox service.

        Args:
            config: Application configuration

        Raises:
            HTTPException: If initialization fails
        """
        self.app_config = config or get_config()
        runtime_config = self.app_config.runtime

        if runtime_config.type != "kubernetes":
            raise ValueError("KubernetesSandboxService requires runtime.type = 'kubernetes'")

        if not self.app_config.kubernetes:
            raise ValueError("Kubernetes configuration is required")

        # Ingress configuration (direct/gateway) if provided
        self.ingress_config = self.app_config.ingress

        self.namespace = self.app_config.kubernetes.namespace
        self.execd_image = runtime_config.execd_image
        self.egress_image = self.app_config.egress.image if self.app_config.egress else None
        self.egress_upstream_dns = self.app_config.egress.upstream_dns if self.app_config.egress else None
        self.service_account = self.app_config.kubernetes.service_account

        # Initialize Kubernetes client
        try:
            self.k8s_client = K8sClient(self.app_config.kubernetes)
            logger.info("Kubernetes client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": SandboxErrorCodes.K8S_INITIALIZATION_ERROR,
                    "message": f"Failed to initialize Kubernetes client: {str(e)}",
                },
            ) from e

        # Auto-discover cluster DNS if egress is enabled but upstream_dns not explicitly set
        if self.app_config.egress and self.app_config.egress.image and not self.egress_upstream_dns:
            try:
                svc = self.k8s_client.get_core_v1_api().read_namespaced_service("kube-dns", "kube-system")
                cluster_dns_ip = svc.spec.cluster_ip
                if cluster_dns_ip and cluster_dns_ip != "None":
                    self.egress_upstream_dns = f"{cluster_dns_ip}:53"
                    logger.info("Auto-discovered cluster DNS for egress upstream: %s", self.egress_upstream_dns)
                else:
                    logger.warning("kube-dns service has no ClusterIP (headless), egress will use resolv.conf fallback")
            except Exception as e:
                logger.warning("Failed to auto-discover cluster DNS, egress will use resolv.conf fallback: %s", e)

        # Initialize workload provider
        provider_type = self.app_config.kubernetes.workload_provider
        try:
            self.workload_provider = create_workload_provider(
                provider_type=provider_type,
                k8s_client=self.k8s_client,
                k8s_config=self.app_config.kubernetes,
                agent_sandbox_config=self.app_config.agent_sandbox,
                ingress_config=self.ingress_config,
                execd_image=self.execd_image,
                egress_image=self.egress_image,
            )
            # Inject per-sandbox lock function so the provider's archive sweep
            # can coordinate with pause/resume/delete operations.
            if hasattr(self.workload_provider, 'set_sandbox_lock_fn'):
                self.workload_provider.set_sandbox_lock_fn(self._get_sandbox_lock)
            logger.info(
                f"Initialized workload provider: {self.workload_provider.__class__.__name__}"
            )
        except ValueError as e:
            logger.error(f"Failed to create workload provider: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": SandboxErrorCodes.K8S_INITIALIZATION_ERROR,
                    "message": f"Invalid workload provider configuration: {str(e)}",
                },
            ) from e

        # Initialize network access config resolver with hot-reload watcher
        if self.app_config.egress and self.app_config.egress.network_access_config_file:
            from src.services.k8s.network_access import NetworkAccessConfig
            self.network_access_config = NetworkAccessConfig(
                self.app_config.egress.network_access_config_file
            )

            # Set up hot-reload policy pusher
            from src.services.k8s.policy_pusher import PolicyPusher
            self._policy_pusher = PolicyPusher(
                core_v1_api=self.k8s_client.get_core_v1_api(),
                namespace=self.namespace,
                network_access_config=self.network_access_config,
                get_egress_token_fn=self.workload_provider.get_egress_token,
            )
            self.network_access_config.on_reload(self._policy_pusher.push_all)
            self.network_access_config.start_watcher()

            # Push current policy to any already-running sandboxes (e.g. after
            # helm upgrade restarts the server with a new config).
            self._policy_pusher.push_all()
        else:
            self.network_access_config = None
            self._policy_pusher = None

        # Per-sandbox locks to prevent TOCTOU races in pause/resume/delete
        self._sandbox_locks: dict[str, threading.Lock] = {}
        self._sandbox_locks_lock = threading.Lock()
        self._sandbox_locks_prune_counter = 0
        self._SANDBOX_LOCKS_PRUNE_INTERVAL = 100  # prune every N lock acquisitions

        logger.info(
            "KubernetesSandboxService initialized: namespace=%s, execd_image=%s",
            self.namespace,
            self.execd_image,
        )

        self._sweep_lock = threading.Lock()
        self._sweep_completed: bool = False

        # Start background tasks (archive sweep / snapshot reconciliation)
        # on server startup so they run even if no pause_workload() is called.
        # This must be after set_sandbox_lock_fn() to ensure the lock function
        # is available when the sweep thread eventually runs.
        if hasattr(self.workload_provider, 'start_background_tasks'):
            self.workload_provider.start_background_tasks(self.namespace)

    def startup_stale_sandbox_sweep(self) -> None:
        """Public entry point for the deferred stale-sandbox sweep.

        Guarded so it only runs once, even if called multiple times.
        """
        with self._sweep_lock:
            if self._sweep_completed:
                return
            self._sweep_completed = True
        self._startup_stale_sandbox_sweep()

    def _startup_stale_sandbox_sweep(self) -> None:
        """Detect running sandboxes with stale operator-controlled images.

        Compares the ``image-config-hash`` annotation on each running
        BatchSandbox CR against the current hash computed by the provider.
        Logs warnings for every mismatch.  When ``runtime.eager_image_update``
        is enabled, force-pauses the stale sandboxes so they pick up new
        images on the next user-triggered resume.
        """
        current_hash = self.workload_provider.image_config_hash
        if not current_hash:
            logger.debug("Skipping stale-sandbox sweep: no image config hash available")
            return

        eager = self.app_config.runtime.eager_image_update

        try:
            workloads = self.workload_provider.list_workloads(
                self.namespace, label_selector=SANDBOX_ID_LABEL
            )
        except Exception as e:
            logger.warning("Stale-sandbox sweep: failed to list workloads: %s", e)
            return

        stale_count = 0
        for w in workloads:
            # Only consider running sandboxes (replicas > 0, not paused)
            replicas = w.get("spec", {}).get("replicas", 0)
            annotations = w.get("metadata", {}).get("annotations", {}) or {}
            is_paused = annotations.get(ANNOTATION_PAUSED) == "true"
            if replicas < 1 or is_paused:
                continue

            sandbox_hash = annotations.get(ANNOTATION_IMAGE_CONFIG_HASH)
            sandbox_id = w.get("metadata", {}).get("name", "unknown")

            # Skip sandboxes created before hash tracking was deployed
            if sandbox_hash is None:
                logger.debug(
                    "Sandbox %s has no image-config-hash annotation, skipping stale check",
                    sandbox_id,
                )
                continue

            if sandbox_hash != current_hash:
                stale_count += 1
                logger.warning(
                    "Stale sandbox detected: %s (hash=%s, current=%s)",
                    sandbox_id, sandbox_hash, current_hash,
                )
                if eager:
                    lock = self._get_sandbox_lock(sandbox_id)
                    with lock:
                        try:
                            self.workload_provider.pause_workload(sandbox_id, self.namespace)
                            logger.info(
                                "Force-paused stale sandbox %s for image update", sandbox_id
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to force-pause stale sandbox %s: %s", sandbox_id, e
                            )

        if stale_count:
            logger.info(
                "Stale-sandbox sweep complete: %d stale sandbox(es) found%s",
                stale_count,
                ", force-paused" if eager else " (eager_image_update=false, no action taken)",
            )

    def _get_sandbox_lock(self, sandbox_id: str) -> threading.Lock:
        """Get or create a per-sandbox lock."""
        should_prune = False
        with self._sandbox_locks_lock:
            if sandbox_id not in self._sandbox_locks:
                self._sandbox_locks[sandbox_id] = threading.Lock()
            lock = self._sandbox_locks[sandbox_id]
            # Periodically prune locks for sandboxes that no longer exist
            self._sandbox_locks_prune_counter += 1
            if self._sandbox_locks_prune_counter >= self._SANDBOX_LOCKS_PRUNE_INTERVAL:
                self._sandbox_locks_prune_counter = 0
                should_prune = True
        # Prune outside the global lock to avoid blocking all threads during K8s API calls
        if should_prune:
            self._prune_stale_locks()
        return lock

    def _prune_stale_locks(self) -> None:
        """Remove lock entries for sandboxes that no longer exist in Kubernetes.

        Runs outside ``_sandbox_locks_lock`` to avoid blocking all threads
        during Kubernetes API calls.
        """
        # Step 1: Collect candidates under the lock
        with self._sandbox_locks_lock:
            candidates = [
                (sid, lk) for sid, lk in self._sandbox_locks.items()
                if not lk.locked()
            ]

        # Step 2: Check liveness outside the lock (K8s API calls)
        stale_ids = []
        for sid, _ in candidates:
            try:
                workload = self.workload_provider.get_workload(
                    sandbox_id=sid, namespace=self.namespace,
                )
                if not workload:
                    stale_ids.append(sid)
            except Exception:
                # If we can't check, leave the lock in place
                pass

        # Step 3: Remove stale entries under the lock, but only if the lock
        # is still not held (prevents pruning a lock that was just acquired)
        if stale_ids:
            with self._sandbox_locks_lock:
                for sid in stale_ids:
                    lk = self._sandbox_locks.get(sid)
                    if lk and not lk.locked():
                        self._sandbox_locks.pop(sid, None)
            logger.debug("Pruned %d stale sandbox lock(s)", len(stale_ids))

    def _detect_pod_failure(self, sandbox_id: str) -> Optional[Dict[str, Any]]:
        """Check actual pod status for failure signals not yet reflected in the CRD.

        Called only from ``_wait_for_sandbox_ready()`` when the CRD status is
        still ``Pending``, so it does NOT add overhead to ``list_sandboxes``.

        Returns:
            A status dict with state ``"Failed"`` if a failure is detected,
            or ``None`` if no failure is found.
        """
        core_api = self.k8s_client.get_core_v1_api()
        try:
            pods = core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"{SANDBOX_ID_LABEL}={sandbox_id}",
            ).items
        except Exception as e:
            logger.debug("Failed to query pods for failure detection on %s: %s", sandbox_id, e)
            return None

        for pod in pods:
            failure = detect_pod_failure(pod)
            if failure:
                state, reason, message = failure
                return {"state": state, "reason": reason, "message": message}

        return None

    def _wait_for_sandbox_ready(
        self,
        sandbox_id: str,
        timeout_seconds: int = 60,
        fast_poll_interval_seconds: float = 0.3,
    ) -> Dict[str, Any]:
        """
        Wait for Pod to be Running and have an IP address.

        Uses adaptive polling: ``fast_poll_interval_seconds`` for the first 5 s,
        then backs off to 1.0 s to reduce API-server load on slow starts.

        Args:
            sandbox_id: Sandbox ID
            timeout_seconds: Maximum time to wait in seconds
            fast_poll_interval_seconds: Polling interval during the first 5 s;
                after that the interval increases to 1.0 s

        Returns:
            Workload dict when Pod is Running with IP

        Raises:
            HTTPException: If timeout or Pod fails
        """
        logger.info(
            f"Waiting for sandbox {sandbox_id} to be Running with IP (timeout: {timeout_seconds}s)"
        )

        _FAST_POLL_WINDOW = 5.0
        _SLOW_POLL_INTERVAL = 1.0

        start_time = time.time()
        last_state = None
        last_message = None

        while True:
            elapsed = time.time() - start_time
            if elapsed >= timeout_seconds:
                break

            poll_sleep = fast_poll_interval_seconds if elapsed < _FAST_POLL_WINDOW else _SLOW_POLL_INTERVAL

            try:
                # Get current workload status
                workload = self.workload_provider.get_workload(
                    sandbox_id=sandbox_id,
                    namespace=self.namespace,
                )

                if not workload:
                    logger.debug(f"Workload not found yet for sandbox {sandbox_id}")
                    time.sleep(poll_sleep)
                    continue

                # Get status
                status_info = self.workload_provider.get_status(workload)
                current_state = status_info["state"]
                current_message = status_info["message"]

                # When CRD still says Pending, check actual pod for failures
                if current_state == "Pending":
                    failure = self._detect_pod_failure(sandbox_id)
                    if failure:
                        current_state = failure["state"]
                        current_message = failure["message"]

                # Log state changes
                if current_state != last_state or current_message != last_message:
                    logger.info(
                        f"Sandbox {sandbox_id} state: {current_state} - {current_message}"
                    )
                    last_state = current_state
                    last_message = current_message

                # Check if Failed
                if current_state == "Failed":
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail={
                            "code": SandboxErrorCodes.K8S_POD_FAILED,
                            "message": f"Pod failed: {current_message}",
                        },
                    )

                # Check if Running
                if current_state == "Running":
                    return workload

            except HTTPException:
                raise
            except Exception as e:
                logger.warning(
                    f"Error checking sandbox {sandbox_id} status: {e}",
                    exc_info=True
                )

            # Wait before next poll — use fast interval for first 5s, then back off
            time.sleep(poll_sleep)

        # Timeout
        elapsed = time.time() - start_time
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "code": SandboxErrorCodes.K8S_POD_READY_TIMEOUT,
                "message": (
                    f"Timeout waiting for sandbox {sandbox_id} to be Running with IP. "
                    f"Elapsed: {elapsed:.1f}s, Last state: {last_state}"
                ),
            },
        )

    def create_sandbox(self, request: CreateSandboxRequest) -> CreateSandboxResponse:
        """
        Create a new sandbox using Kubernetes Pod.

        Wait for the Pod to be Running and have an IP address before returning.

        Args:
            request: Sandbox creation request.

        Returns:
            CreateSandboxResponse: Created sandbox information with Running state

        Raises:
            HTTPException: If creation fails, timeout, or invalid parameters
        """
        # Validate request
        ensure_entrypoint(request.entrypoint)
        ensure_metadata_labels(request.metadata)

        # Resolve network policy: explicit request > config-based resolution
        network_policy = request.network_policy
        if network_policy is None and self.network_access_config:
            network_policy = self.network_access_config.resolve(request.metadata)

        # Validate egress support with the RESOLVED policy (not request.network_policy)
        if network_policy:
            ensure_egress_configured(network_policy, self.app_config.egress)

        # Generate sandbox ID
        sandbox_id = self.generate_sandbox_id()

        # Calculate expiration time
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=request.timeout)

        # Build labels
        labels = {
            SANDBOX_ID_LABEL: sandbox_id,
        }

        # Add user metadata as labels
        if request.metadata:
            labels.update(request.metadata)

        # Extract resource limits
        resource_limits = {}
        if request.resource_limits and request.resource_limits.root:
            resource_limits = request.resource_limits.root

        try:
            # Get egress image based on RESOLVED policy
            egress_image = self.egress_image if network_policy else None

            # Create workload
            workload_info = self.workload_provider.create_workload(
                sandbox_id=sandbox_id,
                namespace=self.namespace,
                image_spec=request.image,
                entrypoint=request.entrypoint,
                env=request.env or {},
                resource_limits=resource_limits,
                labels=labels,
                expires_at=expires_at,
                execd_image=self.execd_image,
                extensions=request.extensions,
                network_policy=network_policy,
                egress_image=egress_image,
                upstream_dns=self.egress_upstream_dns,
            )

            logger.info(
                "Created sandbox: id=%s, workload=%s",
                sandbox_id,
                workload_info.get("name"),
            )

            # Wait for Pod to be Running with IP
            try:
                workload = self._wait_for_sandbox_ready(
                    sandbox_id=sandbox_id,
                    timeout_seconds=60,
                    fast_poll_interval_seconds=0.3,
                )

                # Get final status
                status_info = self.workload_provider.get_status(workload)

                # Build and return response with Running state
                return CreateSandboxResponse(
                    id=sandbox_id,
                    status=SandboxStatus(
                        state=status_info["state"],
                        reason=status_info["reason"],
                        message=status_info["message"],
                        last_transition_at=status_info["last_transition_at"],
                    ),
                    created_at=created_at,
                    expires_at=expires_at,
                    metadata=request.metadata,
                    image=request.image,
                    entrypoint=request.entrypoint,
                )

            except HTTPException:
                # Clean up on failure
                try:
                    logger.warning(f"Creation failed, cleaning up sandbox: {sandbox_id}")
                    self.workload_provider.delete_workload(sandbox_id, self.namespace)
                except Exception as cleanup_ex:
                    logger.error(f"Failed to cleanup sandbox {sandbox_id}", exc_info=cleanup_ex)
                raise

        except HTTPException:
            raise
        except ValueError as e:
            # Handle parameter validation errors from provider
            logger.error(f"Invalid parameters for sandbox creation: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": SandboxErrorCodes.INVALID_PARAMETER,
                    "message": str(e),
                },
            ) from e
        except Exception as e:
            logger.error(f"Error creating sandbox: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.K8S_API_ERROR,
                    "message": f"Failed to create sandbox: {str(e)}",
                },
            ) from e

    def get_sandbox(self, sandbox_id: str) -> Sandbox:
        """
        Get sandbox by ID.

        Args:
            sandbox_id: Unique sandbox identifier

        Returns:
            Sandbox: Sandbox information

        Raises:
            HTTPException: If sandbox not found
        """
        try:
            workload = self.workload_provider.get_workload(
                sandbox_id=sandbox_id,
                namespace=self.namespace,
            )

            if not workload:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": SandboxErrorCodes.K8S_SANDBOX_NOT_FOUND,
                        "message": f"Sandbox '{sandbox_id}' not found",
                    },
                )

            return self._build_sandbox_from_workload(workload)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting sandbox {sandbox_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.K8S_API_ERROR,
                    "message": f"Failed to get sandbox: {str(e)}",
                },
            ) from e

    def list_sandboxes(self, request: ListSandboxesRequest) -> ListSandboxesResponse:
        """
        List sandboxes with filtering and pagination.

        Args:
            request: List request with filters and pagination

        Returns:
            ListSandboxesResponse: Paginated list of sandboxes
        """
        try:
            # Build label selector
            label_selector = SANDBOX_ID_LABEL

            # List all workloads
            workloads = self.workload_provider.list_workloads(
                namespace=self.namespace,
                label_selector=label_selector,
            )

            # Convert to Sandbox objects
            sandboxes = [
                self._build_sandbox_from_workload(w) for w in workloads
            ]

            # Apply filters
            filtered = self._apply_filters(sandboxes, request.filter)

            # Sort by creation time (newest first)
            filtered.sort(key=lambda s: s.created_at or datetime.min, reverse=True)

            # Apply pagination
            total_items = len(filtered)
            page = request.pagination.page
            page_size = request.pagination.page_size

            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_items = filtered[start_idx:end_idx]

            total_pages = (total_items + page_size - 1) // page_size
            has_next = page < total_pages

            return ListSandboxesResponse(
                items=paginated_items,
                pagination=PaginationInfo(
                    page=page,
                    page_size=page_size,
                    total_items=total_items,
                    total_pages=total_pages,
                    has_next_page=has_next,
                ),
            )

        except Exception as e:
            logger.error(f"Error listing sandboxes: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.K8S_API_ERROR,
                    "message": f"Failed to list sandboxes: {str(e)}",
                },
            ) from e

    def delete_sandbox(self, sandbox_id: str) -> None:
        """
        Delete a sandbox.

        Args:
            sandbox_id: Unique sandbox identifier

        Raises:
            HTTPException: If deletion fails
        """
        lock = self._get_sandbox_lock(sandbox_id)
        deleted = False
        with lock:
            try:
                self.workload_provider.delete_workload(
                    sandbox_id=sandbox_id,
                    namespace=self.namespace,
                )

                logger.info(f"Deleted sandbox: {sandbox_id}")
                deleted = True

            except Exception as e:
                if "not found" in str(e).lower():
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "code": SandboxErrorCodes.K8S_SANDBOX_NOT_FOUND,
                            "message": f"Sandbox '{sandbox_id}' not found",
                        },
                    ) from e

                logger.error(f"Error deleting sandbox {sandbox_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "code": SandboxErrorCodes.K8S_API_ERROR,
                        "message": f"Failed to delete sandbox: {str(e)}",
                    },
                ) from e

        # Clean up lock entry after releasing the lock
        if deleted:
            with self._sandbox_locks_lock:
                self._sandbox_locks.pop(sandbox_id, None)

    def pause_sandbox(self, sandbox_id: str) -> None:
        """
        Pause a running sandbox by scaling replicas to 0.

        Args:
            sandbox_id: Unique sandbox identifier

        Raises:
            HTTPException: If sandbox not found, not in Running state, or pause fails
        """
        lock = self._get_sandbox_lock(sandbox_id)
        with lock:
            try:
                workload = self.workload_provider.get_workload(
                    sandbox_id=sandbox_id,
                    namespace=self.namespace,
                )
                if not workload:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "code": SandboxErrorCodes.K8S_SANDBOX_NOT_FOUND,
                            "message": f"Sandbox '{sandbox_id}' not found",
                        },
                    )

                # Reject pool-based sandboxes
                if workload.get("spec", {}).get("poolRef"):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": SandboxErrorCodes.K8S_API_ERROR,
                            "message": "Pause is not supported for pool-based sandboxes",
                        },
                    )

                # Verify sandbox is currently running
                status_info = self.workload_provider.get_status(workload)
                if status_info["state"] != "Running":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": SandboxErrorCodes.K8S_API_ERROR,
                            "message": (
                                f"Sandbox '{sandbox_id}' is in state "
                                f"'{status_info['state']}', must be Running to pause"
                            ),
                        },
                    )

                self.workload_provider.pause_workload(sandbox_id, self.namespace)
                logger.info(f"Paused sandbox: {sandbox_id}")

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error pausing sandbox {sandbox_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "code": SandboxErrorCodes.K8S_API_ERROR,
                        "message": f"Failed to pause sandbox: {str(e)}",
                    },
                ) from e

    def resume_sandbox(self, sandbox_id: str) -> None:
        """
        Resume a paused sandbox by scaling replicas back to 1.

        Args:
            sandbox_id: Unique sandbox identifier

        Raises:
            HTTPException: If sandbox not found, not in Paused state, or resume fails
        """
        lock = self._get_sandbox_lock(sandbox_id)
        with lock:
            try:
                workload = self.workload_provider.get_workload(
                    sandbox_id=sandbox_id,
                    namespace=self.namespace,
                )
                if not workload:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "code": SandboxErrorCodes.K8S_SANDBOX_NOT_FOUND,
                            "message": f"Sandbox '{sandbox_id}' not found",
                        },
                    )

                # Reject pool-based sandboxes
                if workload.get("spec", {}).get("poolRef"):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": SandboxErrorCodes.K8S_API_ERROR,
                            "message": "Resume is not supported for pool-based sandboxes",
                        },
                    )

                # Verify sandbox is currently paused
                status_info = self.workload_provider.get_status(workload)
                if status_info["state"] != "Paused":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": SandboxErrorCodes.K8S_API_ERROR,
                            "message": (
                                f"Sandbox '{sandbox_id}' is in state "
                                f"'{status_info['state']}', must be Paused to resume"
                            ),
                        },
                    )

                # Resolve current network policy for egress sidecar inject/remove/update
                network_policy = None
                if self.network_access_config:
                    metadata = workload.get("metadata", {}).get("labels", {})
                    network_policy = self.network_access_config.resolve(metadata)

                self.workload_provider.resume_workload(
                    sandbox_id, self.namespace,
                    network_policy=network_policy,
                    egress_image=self.egress_image,
                    upstream_dns=self.egress_upstream_dns,
                )
                logger.info(f"Resumed sandbox: {sandbox_id}")

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error resuming sandbox {sandbox_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "code": SandboxErrorCodes.K8S_API_ERROR,
                        "message": f"Failed to resume sandbox: {str(e)}",
                    },
                ) from e

    def renew_expiration(
        self,
        sandbox_id: str,
        request: RenewSandboxExpirationRequest,
    ) -> RenewSandboxExpirationResponse:
        """
        Renew sandbox expiration time.

        Updates both the BatchSandbox spec.expireTime and label for consistency.

        Args:
            sandbox_id: Unique sandbox identifier
            request: Renewal request with new expiration time

        Returns:
            RenewSandboxExpirationResponse: Updated expiration time

        Raises:
            HTTPException: If renewal fails
        """
        # Validate future expiration
        new_expiration = ensure_future_expiration(request.expires_at)

        lock = self._get_sandbox_lock(sandbox_id)
        with lock:
            try:
                # Verify sandbox exists
                workload = self.workload_provider.get_workload(
                    sandbox_id=sandbox_id,
                    namespace=self.namespace,
                )

                if not workload:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "code": SandboxErrorCodes.K8S_SANDBOX_NOT_FOUND,
                            "message": f"Sandbox '{sandbox_id}' not found",
                        },
                    )

                # Update BatchSandbox spec.expireTime field
                self.workload_provider.update_expiration(
                    sandbox_id=sandbox_id,
                    namespace=self.namespace,
                    expires_at=new_expiration,
                )

                logger.info(
                    f"Renewed sandbox {sandbox_id} expiration to {new_expiration}"
                )

                return RenewSandboxExpirationResponse(
                    expires_at=new_expiration
                )

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error renewing expiration for {sandbox_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "code": SandboxErrorCodes.K8S_API_ERROR,
                        "message": f"Failed to renew expiration: {str(e)}",
                    },
                ) from e

    def get_endpoint(
        self,
        sandbox_id: str,
        port: int,
        resolve_internal: bool = False,
    ) -> Endpoint:
        """
        Get sandbox access endpoint.

        Args:
            sandbox_id: Unique sandbox identifier
            port: Port number
            resolve_internal: Ignored for Kubernetes (always returns Pod IP)

        Returns:
            Endpoint: Endpoint information

        Raises:
            HTTPException: If endpoint not available
        """
        self.validate_port(port)

        try:
            workload = self.workload_provider.get_workload(
                sandbox_id=sandbox_id,
                namespace=self.namespace,
            )

            if not workload:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": SandboxErrorCodes.K8S_SANDBOX_NOT_FOUND,
                        "message": f"Sandbox '{sandbox_id}' not found",
                    },
                )

            endpoint = self.workload_provider.get_endpoint_info(workload, port, sandbox_id)
            if not endpoint:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": SandboxErrorCodes.K8S_POD_IP_NOT_AVAILABLE,
                        "message": "Pod IP is not yet available. The Pod may still be starting.",
                    },
                )
            return endpoint

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting endpoint for {sandbox_id}:{port}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.K8S_API_ERROR,
                    "message": f"Failed to get endpoint: {str(e)}",
                },
            ) from e

    def get_execd_token(self, sandbox_id: str) -> Optional[str]:
        """Get the execd access token for a sandbox from its K8s Secret."""
        return self.workload_provider.get_execd_token(sandbox_id, self.namespace)

    def get_egress_token(self, sandbox_id: str) -> Optional[str]:
        """Get the egress auth token for a sandbox from its K8s Secret."""
        return self.workload_provider.get_egress_token(sandbox_id, self.namespace)

    def _build_sandbox_from_workload(self, workload: Any) -> Sandbox:
        """
        Build Sandbox object from Kubernetes workload.

        Args:
            workload: Kubernetes workload object (V1Pod or dict for CRD)

        Returns:
            Sandbox: Sandbox object
        """
        # Handle both dict (CRD) and object (Pod) formats
        if isinstance(workload, dict):
            metadata = workload.get("metadata", {})
            spec = workload.get("spec", {})
            labels = metadata.get("labels", {})
            creation_timestamp = metadata.get("creationTimestamp")
        else:
            metadata = workload.metadata
            spec = workload.spec
            labels = metadata.labels or {}
            creation_timestamp = metadata.creation_timestamp

        sandbox_id = labels.get(SANDBOX_ID_LABEL, "")

        # Get expiration from provider.
        # Fall back to creation timestamp when the workload has no expiration
        # (e.g. paused sandboxes created before expireTime was introduced)
        # so that the API never returns expiresAt: null which breaks older SDK clients.
        expires_at = self.workload_provider.get_expiration(workload) or creation_timestamp

        # Get status
        status_info = self.workload_provider.get_status(workload)

        # Extract metadata (filter out system labels)
        user_metadata = {
            k: v for k, v in labels.items()
            if not k.startswith(LABEL_PREFIX)
        }

        # Get image and entrypoint from spec
        image_uri = ""
        entrypoint = []

        if isinstance(workload, dict):
            # For CRD, extract from template
            template = spec.get("template") or spec.get("podTemplate") or {}
            pod_spec = template.get("spec", {})
            containers = pod_spec.get("containers", [])
            if containers:
                container = containers[0]
                image_uri = container.get("image", "")
                entrypoint = container.get("command", [])
        else:
            # For Pod object
            if hasattr(spec, 'containers') and spec.containers:
                container = spec.containers[0]
                image_uri = container.image or ""
                entrypoint = container.command or []

        # Create ImageSpec object
        from src.api.schema import ImageSpec
        image_spec = ImageSpec(uri=image_uri) if image_uri else ImageSpec(uri="unknown")

        return Sandbox(
            id=sandbox_id,
            status=SandboxStatus(
                state=status_info["state"],
                reason=status_info["reason"],
                message=status_info["message"],
                last_transition_at=status_info["last_transition_at"],
            ),
            created_at=creation_timestamp,
            expires_at=expires_at,
            metadata=user_metadata if user_metadata else None,
            image=image_spec,
            entrypoint=entrypoint,
        )

    def _apply_filters(self, sandboxes: list[Sandbox], filter_spec: Any) -> list[Sandbox]:
        """
        Apply filters to sandbox list.

        Args:
            sandboxes: List of sandboxes
            filter_spec: Filter specification

        Returns:
            Filtered list of sandboxes
        """
        if not filter_spec:
            return sandboxes

        filtered = []
        for sandbox in sandboxes:
            if matches_filter(sandbox, filter_spec):
                filtered.append(sandbox)

        return filtered
