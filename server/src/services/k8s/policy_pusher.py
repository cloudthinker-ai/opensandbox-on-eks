"""
Hot-reload policy pusher for running egress sidecars.

Discovers pods with egress sidecars and pushes updated network policies
via K8s exec into the egress container's policy HTTP endpoint.
"""

import json
import logging
import threading
from typing import Callable, Dict, List, Optional

from kubernetes.stream import stream

from src.api.schema import NetworkPolicy
from src.services.constants import LABEL_EGRESS_SIDECAR, LABEL_PREFIX, SANDBOX_ID_LABEL
from src.services.k8s.network_access import NetworkAccessConfig

logger = logging.getLogger(__name__)

# Label selector used to identify pods with egress sidecar
EGRESS_SIDECAR_LABEL_SELECTOR = f"{LABEL_EGRESS_SIDECAR}=true"


class PolicyPusher:
    """Discovers running pods with egress sidecars and pushes updated policies."""

    def __init__(
        self,
        core_v1_api,
        namespace: str,
        network_access_config: NetworkAccessConfig,
        get_egress_token_fn: Callable[[str, str], Optional[str]],
    ):
        self._core_v1 = core_v1_api
        self._namespace = namespace
        self._config = network_access_config
        self._get_egress_token = get_egress_token_fn

    def push_all(self) -> None:
        """Push updated policies to all running pods with egress sidecars.

        Runs in a background thread to avoid blocking the watcher.
        """
        thread = threading.Thread(
            target=self._push_all_sync,
            name="policy-push",
            daemon=True,
        )
        thread.start()

    def _push_all_sync(self) -> None:
        """Synchronous implementation of push_all."""
        try:
            pods = self._core_v1.list_namespaced_pod(
                namespace=self._namespace,
                label_selector=EGRESS_SIDECAR_LABEL_SELECTOR,
            )
        except Exception:
            logger.exception("Failed to list pods for policy push")
            return

        running_pods = [
            pod for pod in pods.items
            if pod.status and pod.status.phase == "Running"
        ]

        if not running_pods:
            logger.info("Policy push: no running pods with egress sidecar found")
            return

        succeeded = 0
        failed = 0

        for pod in running_pods:
            try:
                self._push_to_pod(pod)
                succeeded += 1
            except Exception:
                failed += 1
                logger.exception(
                    "Failed to push policy to pod %s", pod.metadata.name
                )

        logger.info(
            "Policy push complete: %d succeeded, %d failed", succeeded, failed
        )

    def _push_to_pod(self, pod) -> None:
        """Push updated policy to a single pod via K8s exec."""
        labels = pod.metadata.labels or {}
        sandbox_id = labels.get(SANDBOX_ID_LABEL)
        if not sandbox_id:
            logger.warning(
                "Pod %s has egress sidecar label but no sandbox-id label, skipping",
                pod.metadata.name,
            )
            return

        # Extract user metadata (non-system labels)
        metadata = {
            k: v for k, v in labels.items()
            if not k.startswith(LABEL_PREFIX)
        }

        # Resolve new policy
        new_policy = self._config.resolve(metadata)

        # Build payload — if resolve returns None (allow-all, no sidecar needed),
        # push an allow-all policy so the sidecar opens up
        if new_policy is None:
            payload = json.dumps({"defaultAction": "allow", "egress": []})
        else:
            payload = json.dumps(
                new_policy.model_dump(by_alias=True, exclude_none=True)
            )

        # Get egress auth token
        token = self._get_egress_token(sandbox_id, self._namespace)
        if not token:
            logger.warning(
                "No egress token found for sandbox %s, skipping policy push",
                sandbox_id,
            )
            return

        # Execute wget POST inside the egress container
        self._exec_policy_update(pod, token, payload)

    def _exec_policy_update(self, pod, token: str, payload: str) -> None:
        """Execute wget POST /policy inside the egress container."""
        # Use sh -c so that arguments with spaces/special chars are handled
        # correctly through the K8s exec API.
        # The egress sidecar listens on 127.0.0.1:18080
        command = [
            "sh", "-c",
            f"wget -q -O - "
            f"--header 'OPENSANDBOX-EGRESS-AUTH: {token}' "
            f"--header 'Content-Type: application/json' "
            f"--post-data '{payload}' "
            f"http://127.0.0.1:18080/policy",
        ]

        resp = stream(
            self._core_v1.connect_get_namespaced_pod_exec,
            name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            container="egress",
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

        logger.info(
            "Policy push to pod %s response: %s", pod.metadata.name, resp
        )
