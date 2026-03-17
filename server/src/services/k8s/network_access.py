"""
Network access policy resolver.

Reads a YAML config file mapping scopes (workspace, org, default) to egress rules.
Resolves the most specific matching policy from sandbox request metadata.
"""

import logging
import os
import threading
import yaml
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.api.schema import NetworkPolicy, NetworkRule

logger = logging.getLogger(__name__)


class NetworkAccessConfig:
    """Loads and resolves scoped network access policies."""

    def __init__(self, config_path: str):
        self._config_path = Path(config_path)
        self._policies: List[dict] = []
        self._metadata_keys: Dict[str, str] = {}  # scope_type -> metadata key
        self._denied_cidrs: List[str] = []
        self._lock = threading.RLock()
        self._last_mtime: float = 0.0
        self._reload_callbacks: List[Callable[[], None]] = []
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_stop = threading.Event()
        self._load()

    def _load(self):
        with self._lock:
            if not self._config_path.exists():
                logger.warning("Network access config not found: %s", self._config_path)
                return
            try:
                self._last_mtime = os.path.getmtime(self._config_path)
            except OSError:
                pass
            with open(self._config_path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.warning("Network access config is not a mapping: %s", self._config_path)
                return
            self._metadata_keys = data.get("metadataKeys", {})
            self._denied_cidrs = data.get("deniedCIDRs", [])
            self._policies = data.get("policies", [])

            # Detect duplicate scope entries
            seen: set = set()
            for p in self._policies:
                scope = p.get("scope", {})
                key = (scope.get("type"), scope.get("id"))
                if key in seen:
                    logger.warning("Duplicate scope in network access config: %s", key)
                seen.add(key)

            logger.info(
                "Loaded %d network access policies (keys: %s)",
                len(self._policies), self._metadata_keys,
            )

    def resolve(self, metadata: Optional[Dict[str, str]]) -> Optional[NetworkPolicy]:
        """
        Resolve network policy from metadata.
        Uses configurable metadataKeys to extract scope IDs from request metadata.
        Priority: workspace > org > default.
        Returns None if no policy matches OR if resolved policy is allow-all (no sidecar needed).
        """
        with self._lock:
            return self._resolve_unlocked(metadata)

    def _resolve_unlocked(self, metadata: Optional[Dict[str, str]]) -> Optional[NetworkPolicy]:
        if not self._policies:
            return None

        metadata = metadata or {}

        # Look up scope IDs using configurable metadata keys
        workspace_key = self._metadata_keys.get("workspace", "workspace_id")
        org_key = self._metadata_keys.get("org", "org_id")
        workspace_id = metadata.get(workspace_key)
        org_id = metadata.get(org_key)

        # Most specific wins
        for scope_type, scope_id in [
            ("workspace", workspace_id),
            ("org", org_id),
            ("default", None),
        ]:
            if scope_type != "default" and not scope_id:
                continue
            policy = self._find(scope_type, scope_id)
            if policy:
                np = self._to_network_policy(policy)
                # allow-all with no USER rules = no sidecar needed (base NP handles CIDRs)
                if np.default_action == "allow" and not np.egress:
                    logger.debug(
                        "Resolved allow-all policy for scope %s/%s, skipping sidecar",
                        scope_type, scope_id,
                    )
                    return None
                # User has rules → append deniedCIDRs for sidecar enforcement
                for cidr in self._denied_cidrs:
                    np.egress.append(NetworkRule(action="deny", target=cidr))
                logger.debug(
                    "Resolved network policy for scope %s/%s: default_action=%s, %d rules (incl. %d denied CIDRs)",
                    scope_type, scope_id, np.default_action, len(np.egress), len(self._denied_cidrs),
                )
                return np

        return None

    def _find(self, scope_type: str, scope_id: Optional[str]) -> Optional[dict]:
        for p in self._policies:
            scope = p.get("scope", {})
            if scope.get("type") == scope_type:
                if scope_type == "default" or scope.get("id") == scope_id:
                    return p
        return None

    _VALID_ACTIONS = {"allow", "deny"}

    def _to_network_policy(self, policy: dict) -> NetworkPolicy:
        default_action = policy.get("defaultAction", "deny")
        if default_action not in self._VALID_ACTIONS:
            logger.warning(
                "Invalid defaultAction '%s', falling back to 'deny'", default_action,
            )
            default_action = "deny"

        rules = []
        for r in policy.get("egress", []):
            if "action" not in r or "target" not in r:
                logger.warning(
                    "Skipping malformed egress rule (missing action/target): %s", r,
                )
                continue
            if r["action"] not in self._VALID_ACTIONS:
                logger.warning(
                    "Skipping egress rule with invalid action '%s': %s",
                    r["action"], r,
                )
                continue
            rules.append(NetworkRule(action=r["action"], target=r["target"]))
        return NetworkPolicy(
            default_action=default_action,
            egress=rules,
        )

    def reload_if_changed(self) -> bool:
        """Check file mtime and reload if changed. Returns True if reloaded."""
        try:
            if not self._config_path.exists():
                return False
            current_mtime = os.path.getmtime(self._config_path)
        except OSError:
            return False

        if current_mtime <= self._last_mtime:
            return False

        logger.info("Network access config file changed, reloading: %s", self._config_path)
        self._load()

        # Fire callbacks outside the lock
        for cb in self._reload_callbacks:
            try:
                cb()
            except Exception:
                logger.exception("Error in reload callback")

        return True

    def on_reload(self, callback: Callable[[], None]) -> None:
        """Register a callback to be called after successful config reload."""
        self._reload_callbacks.append(callback)

    def start_watcher(self, poll_interval: float = 5.0) -> None:
        """Start a daemon thread that polls for config file changes."""
        if self._watcher_thread is not None:
            return

        self._watcher_stop.clear()

        def _poll_loop():
            while not self._watcher_stop.is_set():
                try:
                    self.reload_if_changed()
                except Exception:
                    logger.exception("Error in config watcher poll loop")
                self._watcher_stop.wait(poll_interval)

        self._watcher_thread = threading.Thread(
            target=_poll_loop,
            name="network-access-watcher",
            daemon=True,
        )
        self._watcher_thread.start()
        logger.info("Started network access config watcher (poll_interval=%.1fs)", poll_interval)

    def stop_watcher(self) -> None:
        """Stop the watcher thread."""
        self._watcher_stop.set()
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=10)
            self._watcher_thread = None
