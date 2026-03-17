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
VolumeSnapshot manager for persistent sandbox storage.

Manages CSI VolumeSnapshot lifecycle for cross-zone durability:
- Create snapshots on pause
- Restore PVCs from snapshots on resume
- Clean up snapshots on sandbox deletion
- Retention policy (keep N most recent per sandbox)

Uses standard Kubernetes snapshot.storage.k8s.io/v1 API — cloud-agnostic.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from kubernetes.client import CoreV1Api, CustomObjectsApi, ApiException

from src.services.constants import LABEL_PURPOSE, SANDBOX_ID_LABEL

logger = logging.getLogger(__name__)

SNAPSHOT_GROUP = "snapshot.storage.k8s.io"
SNAPSHOT_VERSION = "v1"
SNAPSHOT_PLURAL = "volumesnapshots"


class SnapshotManager:
    """Manages CSI VolumeSnapshots for sandbox storage durability."""

    def __init__(
        self,
        custom_api: CustomObjectsApi,
        snapshot_class: str,
        max_retention: int = 1,
        core_v1_api: Optional[CoreV1Api] = None,
    ):
        """
        Initialize snapshot manager.

        Args:
            custom_api: Kubernetes CustomObjects API client
            snapshot_class: VolumeSnapshotClass name (e.g., "ebs-snapshot-class")
            max_retention: Maximum number of snapshots to retain per sandbox
            core_v1_api: Kubernetes CoreV1 API client for PVC operations
        """
        self.custom_api = custom_api
        self.snapshot_class = snapshot_class
        self.max_retention = max_retention
        self.core_v1_api = core_v1_api

    def create_snapshot(
        self,
        sandbox_id: str,
        pvc_name: str,
        namespace: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Create a VolumeSnapshot from a PVC.

        Args:
            sandbox_id: Sandbox identifier
            pvc_name: Source PVC name
            namespace: Kubernetes namespace
            labels: Additional labels to apply

        Returns:
            Name of the created VolumeSnapshot
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        snapshot_name = f"{sandbox_id}-snap-{timestamp}"

        snapshot_labels = {
            SANDBOX_ID_LABEL: sandbox_id,
            LABEL_PURPOSE: "sandbox-snapshot",
        }
        if labels:
            snapshot_labels.update(labels)

        body = {
            "apiVersion": f"{SNAPSHOT_GROUP}/{SNAPSHOT_VERSION}",
            "kind": "VolumeSnapshot",
            "metadata": {
                "name": snapshot_name,
                "namespace": namespace,
                "labels": snapshot_labels,
            },
            "spec": {
                "volumeSnapshotClassName": self.snapshot_class,
                "source": {
                    "persistentVolumeClaimName": pvc_name,
                },
            },
        }

        self.custom_api.create_namespaced_custom_object(
            group=SNAPSHOT_GROUP,
            version=SNAPSHOT_VERSION,
            namespace=namespace,
            plural=SNAPSHOT_PLURAL,
            body=body,
        )
        logger.info(
            f"Created VolumeSnapshot {snapshot_name} from PVC {pvc_name} "
            f"for sandbox {sandbox_id}"
        )
        return snapshot_name

    def wait_for_snapshot_ready(
        self,
        snapshot_name: str,
        namespace: str,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> None:
        """
        Poll until a VolumeSnapshot is ready to use.

        Args:
            snapshot_name: VolumeSnapshot name
            namespace: Kubernetes namespace
            timeout: Maximum wait time in seconds
            poll_interval: Seconds between polls

        Raises:
            TimeoutError: If snapshot is not ready within timeout
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            snapshot = self.custom_api.get_namespaced_custom_object(
                group=SNAPSHOT_GROUP,
                version=SNAPSHOT_VERSION,
                namespace=namespace,
                plural=SNAPSHOT_PLURAL,
                name=snapshot_name,
            )
            status = snapshot.get("status", {})
            if status.get("readyToUse") is True:
                logger.info(f"VolumeSnapshot {snapshot_name} is ready")
                return
            time.sleep(poll_interval)

        raise TimeoutError(
            f"VolumeSnapshot {snapshot_name} not ready after {timeout}s"
        )

    def create_pvc_from_snapshot(
        self,
        sandbox_id: str,
        snapshot_name: str,
        namespace: str,
        labels: Dict[str, str],
        storage_class: Optional[str] = None,
        storage_size: str = "20Gi",
    ) -> str:
        """
        Create a new PVC restored from a VolumeSnapshot.

        If the PVC already exists (e.g. from a previous failed resume attempt),
        it is reused rather than raising a conflict error.

        Args:
            sandbox_id: Sandbox identifier
            snapshot_name: Source VolumeSnapshot name
            namespace: Kubernetes namespace
            labels: Labels to apply to the PVC
            storage_class: StorageClass for the new PVC (None = cluster default)
            storage_size: Storage size (e.g., "20Gi")

        Returns:
            Name of the created or existing PVC
        """
        if not self.core_v1_api:
            raise RuntimeError(
                "core_v1_api is required for PVC operations. "
                "Pass it via the SnapshotManager constructor."
            )

        pvc_name = f"{sandbox_id}-sandbox-data"

        pvc_spec: Dict = {
            "accessModes": ["ReadWriteOnce"],
            "resources": {
                "requests": {
                    "storage": storage_size,
                }
            },
            "dataSource": {
                "name": snapshot_name,
                "kind": "VolumeSnapshot",
                "apiGroup": SNAPSHOT_GROUP,
            },
        }
        if storage_class:
            pvc_spec["storageClassName"] = storage_class

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
            "spec": pvc_spec,
        }

        try:
            self.core_v1_api.create_namespaced_persistent_volume_claim(
                namespace=namespace,
                body=pvc_body,
            )
            logger.info(
                f"Created PVC {pvc_name} from snapshot {snapshot_name} "
                f"for sandbox {sandbox_id}"
            )
        except ApiException as e:
            if e.status == 409:
                # PVC already exists (previous failed resume left it behind)
                logger.info(
                    f"PVC {pvc_name} already exists for sandbox {sandbox_id}, "
                    "reusing existing PVC"
                )
            else:
                raise
        return pvc_name

    def wait_for_pvc_bound(
        self,
        pvc_name: str,
        namespace: str,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> None:
        """
        Poll until a PVC is bound.

        Args:
            pvc_name: PVC name
            namespace: Kubernetes namespace
            timeout: Maximum wait time in seconds
            poll_interval: Seconds between polls

        Raises:
            TimeoutError: If PVC is not bound within timeout
        """
        if not self.core_v1_api:
            raise RuntimeError("core_v1_api is required for PVC operations")

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                pvc = self.core_v1_api.read_namespaced_persistent_volume_claim(
                    name=pvc_name, namespace=namespace
                )
            except ApiException as e:
                if e.status == 404:
                    raise RuntimeError(
                        f"PVC {pvc_name} was deleted during wait"
                    ) from e
                logger.warning(f"Transient error reading PVC {pvc_name}: {e}")
                time.sleep(poll_interval)
                continue

            phase = pvc.status.phase if pvc.status else None
            if phase == "Bound":
                logger.info(f"PVC {pvc_name} is bound")
                return
            if phase in ("Failed", "Lost"):
                raise RuntimeError(
                    f"PVC {pvc_name} in terminal state: {phase}"
                )
            time.sleep(poll_interval)

        raise TimeoutError(
            f"PVC {pvc_name} not bound after {timeout}s"
        )

    def get_latest_snapshot(
        self, sandbox_id: str, namespace: str
    ) -> Optional[str]:
        """
        Get the most recent VolumeSnapshot for a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            namespace: Kubernetes namespace

        Returns:
            Snapshot name or None if no snapshots exist
        """
        snapshots = self._list_snapshots(sandbox_id, namespace)
        if not snapshots:
            return None

        # Sort by creation timestamp (newest first)
        snapshots.sort(
            key=lambda s: s.get("metadata", {}).get("creationTimestamp", ""),
            reverse=True,
        )
        return snapshots[0]["metadata"]["name"]

    def delete_snapshot(self, snapshot_name: str, namespace: str) -> None:
        """Delete a single VolumeSnapshot by name."""
        self.custom_api.delete_namespaced_custom_object(
            group=SNAPSHOT_GROUP,
            version=SNAPSHOT_VERSION,
            namespace=namespace,
            plural=SNAPSHOT_PLURAL,
            name=snapshot_name,
        )
        logger.info(f"Deleted VolumeSnapshot {snapshot_name}")

    def delete_snapshots(self, sandbox_id: str, namespace: str) -> None:
        """
        Delete all VolumeSnapshots for a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            namespace: Kubernetes namespace
        """
        snapshots = self._list_snapshots(sandbox_id, namespace)
        for snapshot in snapshots:
            name = snapshot["metadata"]["name"]
            try:
                self.custom_api.delete_namespaced_custom_object(
                    group=SNAPSHOT_GROUP,
                    version=SNAPSHOT_VERSION,
                    namespace=namespace,
                    plural=SNAPSHOT_PLURAL,
                    name=name,
                )
                logger.info(f"Deleted VolumeSnapshot {name} for sandbox {sandbox_id}")
            except ApiException as e:
                if e.status == 404:
                    logger.debug(f"VolumeSnapshot {name} already deleted")
                else:
                    logger.warning(f"Failed to delete VolumeSnapshot {name}: {e}")

    def enforce_retention(self, sandbox_id: str, namespace: str) -> None:
        """
        Delete old snapshots exceeding the retention limit.

        Keeps the N most recent snapshots (N = max_retention).

        Args:
            sandbox_id: Sandbox identifier
            namespace: Kubernetes namespace
        """
        snapshots = self._list_snapshots(sandbox_id, namespace)
        if len(snapshots) <= self.max_retention:
            return

        # Sort by creation timestamp (newest first)
        snapshots.sort(
            key=lambda s: s.get("metadata", {}).get("creationTimestamp", ""),
            reverse=True,
        )

        # Delete snapshots beyond retention limit
        for snapshot in snapshots[self.max_retention:]:
            name = snapshot["metadata"]["name"]
            try:
                self.custom_api.delete_namespaced_custom_object(
                    group=SNAPSHOT_GROUP,
                    version=SNAPSHOT_VERSION,
                    namespace=namespace,
                    plural=SNAPSHOT_PLURAL,
                    name=name,
                )
                logger.info(
                    f"Deleted old VolumeSnapshot {name} (retention cleanup) "
                    f"for sandbox {sandbox_id}"
                )
            except ApiException as e:
                if e.status != 404:
                    logger.warning(
                        f"Failed to delete old VolumeSnapshot {name}: {e}"
                    )

    def _list_snapshots(
        self, sandbox_id: str, namespace: str
    ) -> List[Dict]:
        """List all VolumeSnapshots for a sandbox by label selector."""
        try:
            result = self.custom_api.list_namespaced_custom_object(
                group=SNAPSHOT_GROUP,
                version=SNAPSHOT_VERSION,
                namespace=namespace,
                plural=SNAPSHOT_PLURAL,
                label_selector=f"opensandbox.io/id={sandbox_id}",
            )
            return result.get("items", [])
        except ApiException as e:
            logger.warning(
                f"Failed to list VolumeSnapshots for sandbox {sandbox_id}: {e}"
            )
            return []
