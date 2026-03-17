# Copyright 2026 Alibaba Group Holding Ltd.
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
Egress sidecar helper functions for Kubernetes workloads.

This module provides shared utilities for building egress sidecar containers
and related configurations that can be reused across different workload providers.
"""

import json
import secrets
from typing import Dict, Any, List, Optional, Tuple

from src.api.schema import NetworkPolicy

# Environment variable name for passing network policy to egress sidecar
EGRESS_RULES_ENV = "OPENSANDBOX_EGRESS_RULES"


def build_egress_sidecar_container(
    egress_image: str,
    network_policy: NetworkPolicy,
    upstream_dns: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """
    Build egress sidecar container specification for Kubernetes Pod.

    This function creates a container spec that can be added to a Pod's containers
    list. The sidecar container will:
    - Run the egress image
    - Receive network policy via OPENSANDBOX_EGRESS_RULES environment variable
    - Have NET_ADMIN capability to manage iptables

    Note: In Kubernetes, containers in the same Pod share the network namespace,
    so the main container can access the sidecar's ports (44772 for execd, 8080 for HTTP)
    via localhost without explicit port declarations.

    Args:
        egress_image: Container image for the egress sidecar
        network_policy: Network policy configuration to enforce

    Returns:
        Tuple of (container_spec, token). The container spec dict can be directly
        added to the Pod's containers list. The token is the auth token used by the
        egress sidecar's policy HTTP endpoint.

    Example:
        ```python
        sidecar, token = build_egress_sidecar_container(
            egress_image="opensandbox/egress:v1.0.1",
            network_policy=NetworkPolicy(
                default_action="deny",
                egress=[NetworkRule(action="allow", target="pypi.org")]
            )
        )
        pod_spec["containers"].append(sidecar)
        ```
    """
    # Serialize network policy to JSON for environment variable
    policy_payload = json.dumps(
        network_policy.model_dump(by_alias=True, exclude_none=True)
    )

    # Generate auth token to prevent sandbox from modifying its own policy
    token = secrets.token_hex(32)

    # Build container specification
    container_spec: Dict[str, Any] = {
        "name": "egress",
        "image": egress_image,
        "env": [
            {
                "name": EGRESS_RULES_ENV,
                "value": policy_payload,
            },
            {
                "name": "OPENSANDBOX_EGRESS_TOKEN",
                "value": token,
            },
            {
                "name": "OPENSANDBOX_EGRESS_HTTP_ADDR",
                "value": "127.0.0.1:18080",
            },
            {
                "name": "OPENSANDBOX_EGRESS_MODE",
                "value": "dns+nft",
            },
        ],
        "securityContext": _build_security_context_for_egress(),
        "readinessProbe": {
            "exec": {
                "command": ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:18080/healthz"],
            },
            "initialDelaySeconds": 1,
            "periodSeconds": 2,
            "failureThreshold": 5,
            "timeoutSeconds": 1,
        },
    }

    if upstream_dns:
        container_spec["env"].append({
            "name": "OPENSANDBOX_EGRESS_UPSTREAM",
            "value": upstream_dns,
        })

    return container_spec, token


def _build_security_context_for_egress() -> Dict[str, Any]:
    """
    Build security context for egress sidecar container.

    The egress sidecar needs NET_ADMIN capability to manage iptables rules
    for network policy enforcement.

    This is an internal helper function used by build_egress_sidecar_container().

    Returns:
        Dict containing security context configuration with NET_ADMIN capability.
    """
    return {
        "capabilities": {
            "add": ["NET_ADMIN"],
        },
    }


def build_security_context_for_sandbox_container(
    has_network_policy: bool,
    is_overlay_mode: bool = False,
) -> Dict[str, Any]:
    """
    Build security context for main sandbox container.

    Always sets allowPrivilegeEscalation=False unless overlay mode requires
    privilege escalation for capsh during setup. When network policy is enabled,
    the main container drops NET_ADMIN capability.

    Args:
        has_network_policy: Whether network policy is enabled for this sandbox
        is_overlay_mode: Whether overlay filesystem persistence is active

    Returns:
        Dict containing security context configuration.
    """
    result: Dict[str, Any] = {}

    if not is_overlay_mode:
        result["allowPrivilegeEscalation"] = False

    if has_network_policy:
        result.setdefault("capabilities", {})["drop"] = ["NET_ADMIN"]

    return result


def apply_egress_to_spec(
    pod_spec: Dict[str, Any],
    containers: List[Dict[str, Any]],
    network_policy: Optional[NetworkPolicy],
    egress_image: Optional[str],
    upstream_dns: Optional[str] = None,
) -> Optional[str]:
    """
    Apply egress sidecar configuration to Pod spec.

    This function adds the egress sidecar container to the containers list
    when network policy is provided.

    Args:
        pod_spec: Pod specification dict (will be modified in place)
        containers: List of container dicts (will be modified in place)
        network_policy: Optional network policy configuration
        egress_image: Optional egress sidecar image

    Example:
        ```python
        containers = [main_container_dict]
        pod_spec = {"containers": containers, ...}

        apply_egress_to_spec(
            pod_spec=pod_spec,
            containers=containers,
            network_policy=network_policy,
            egress_image=egress_image,
        )
        ```

    """
    if not network_policy or not egress_image:
        return None

    # Build and add egress sidecar container
    sidecar_container, token = build_egress_sidecar_container(
        egress_image=egress_image,
        network_policy=network_policy,
        upstream_dns=upstream_dns,
    )
    containers.append(sidecar_container)

    return token


def build_security_context_from_dict(
    security_context_dict: Dict[str, Any],
) -> Optional[Any]:
    """
    Convert security context dict to V1SecurityContext object.

    This is a helper function to convert the dict returned by
    build_security_context_for_sandbox_container() into a Kubernetes
    V1SecurityContext object that can be used in V1Container.

    Args:
        security_context_dict: Security context configuration dict

    Returns:
        V1SecurityContext object or None if dict is empty

    Example:
        ```python
        from kubernetes.client import V1Container

        security_context_dict = build_security_context_for_sandbox_container(True)
        security_context = build_security_context_from_dict(security_context_dict)

        container = V1Container(
            name="sandbox",
            security_context=security_context,
        )
        ```
    """
    if not security_context_dict:
        return None

    from kubernetes.client import V1SecurityContext, V1Capabilities

    capabilities = None
    if "capabilities" in security_context_dict:
        caps_dict = security_context_dict["capabilities"]
        add_caps = caps_dict.get("add", [])
        drop_caps = caps_dict.get("drop", [])
        capabilities = V1Capabilities(
            add=add_caps if add_caps else None,
            drop=drop_caps if drop_caps else None,
        )

    allow_priv_esc = security_context_dict.get("allowPrivilegeEscalation")

    return V1SecurityContext(
        capabilities=capabilities,
        allow_privilege_escalation=allow_priv_esc,
    )


def serialize_security_context_to_dict(
    security_context: Optional[Any],
) -> Optional[Dict[str, Any]]:
    """
    Serialize V1SecurityContext to dict format for CRD.

    This function converts a V1SecurityContext object (from V1Container)
    into a dict format that can be used in Kubernetes CRD specifications.

    Args:
        security_context: V1SecurityContext object or None

    Returns:
        Dict representation of security context or None

    Example:
        ```python
        container_dict = {
            "name": container.name,
            "image": container.image,
        }

        if container.security_context:
            container_dict["securityContext"] = serialize_security_context_to_dict(
                container.security_context
            )
        ```
    """
    if not security_context:
        return None

    result: Dict[str, Any] = {}

    if security_context.run_as_user is not None:
        result["runAsUser"] = security_context.run_as_user

    if security_context.allow_privilege_escalation is not None:
        result["allowPrivilegeEscalation"] = security_context.allow_privilege_escalation

    if security_context.capabilities:
        caps: Dict[str, Any] = {}
        if security_context.capabilities.add:
            caps["add"] = security_context.capabilities.add
        if security_context.capabilities.drop:
            caps["drop"] = security_context.capabilities.drop
        if caps:
            result["capabilities"] = caps

    return result if result else None


