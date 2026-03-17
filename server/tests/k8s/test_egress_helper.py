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
Unit tests for egress helper functions.
"""

import json

from src.api.schema import NetworkPolicy, NetworkRule
from src.services.k8s.egress_helper import (
    EGRESS_RULES_ENV,
    apply_egress_to_spec,
    build_egress_sidecar_container,
    build_security_context_for_sandbox_container,
)


class TestBuildEgressSidecarContainer:
    """Tests for build_egress_sidecar_container function."""

    def test_builds_container_with_basic_config(self):
        """Test that container is built with correct basic configuration."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[
                NetworkRule(action="allow", target="pypi.org"),
            ],
        )

        container, token = build_egress_sidecar_container(egress_image, network_policy)

        assert container["name"] == "egress"
        assert container["image"] == egress_image
        assert "env" in container
        assert "securityContext" in container
        assert isinstance(token, str)
        assert len(token) == 64

    def test_contains_egress_rules_environment_variable(self):
        """Test that container includes OPENSANDBOX_EGRESS_RULES environment variable."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        env_vars = container["env"]
        assert len(env_vars) == 4
        assert env_vars[0]["name"] == EGRESS_RULES_ENV
        assert env_vars[0]["value"] is not None

    def test_serializes_network_policy_correctly(self):
        """Test that network policy is correctly serialized to JSON."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[
                NetworkRule(action="allow", target="pypi.org"),
                NetworkRule(action="deny", target="*.malicious.com"),
            ],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        env_value = container["env"][0]["value"]
        # Should be valid JSON
        policy_dict = json.loads(env_value)

        # Verify structure
        assert "defaultAction" in policy_dict  # by_alias=True converts default_action
        assert policy_dict["defaultAction"] == "deny"
        assert "egress" in policy_dict
        assert len(policy_dict["egress"]) == 2
        assert policy_dict["egress"][0]["action"] == "allow"
        assert policy_dict["egress"][0]["target"] == "pypi.org"
        assert policy_dict["egress"][1]["action"] == "deny"
        assert policy_dict["egress"][1]["target"] == "*.malicious.com"

    def test_handles_empty_egress_rules(self):
        """Test that empty egress rules are handled correctly."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="allow",
            egress=[],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        env_value = container["env"][0]["value"]
        policy_dict = json.loads(env_value)

        assert policy_dict["defaultAction"] == "allow"
        assert policy_dict["egress"] == []

    def test_handles_missing_default_action(self):
        """Test that missing default_action is handled (exclude_none=True)."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        env_value = container["env"][0]["value"]
        policy_dict = json.loads(env_value)

        # defaultAction should be excluded if None (exclude_none=True)
        assert "defaultAction" not in policy_dict or policy_dict.get("defaultAction") is None
        assert "egress" in policy_dict

    def test_security_context_has_net_admin_capability(self):
        """Test that security context includes NET_ADMIN capability."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        security_context = container["securityContext"]
        assert "capabilities" in security_context
        assert "add" in security_context["capabilities"]
        assert "NET_ADMIN" in security_context["capabilities"]["add"]

    def test_container_spec_is_valid_kubernetes_format(self):
        """Test that returned container spec is in valid Kubernetes format."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        # Verify all required fields are present
        assert "name" in container
        assert "image" in container
        assert "env" in container
        assert "securityContext" in container

        # Verify env is a list of dicts with name/value
        assert isinstance(container["env"], list)
        assert len(container["env"]) > 0
        assert "name" in container["env"][0]
        assert "value" in container["env"][0]

    def test_handles_wildcard_domains(self):
        """Test that wildcard domains in egress rules are handled correctly."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[
                NetworkRule(action="allow", target="*.python.org"),
                NetworkRule(action="allow", target="pypi.org"),
            ],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        env_value = container["env"][0]["value"]
        policy_dict = json.loads(env_value)

        assert len(policy_dict["egress"]) == 2
        assert policy_dict["egress"][0]["target"] == "*.python.org"
        assert policy_dict["egress"][1]["target"] == "pypi.org"


    def test_includes_auth_token_and_localhost_binding(self):
        """Test that container includes auth token, localhost binding, and dns+nft mode."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        container, token = build_egress_sidecar_container(egress_image, network_policy)

        env_vars = {e["name"]: e["value"] for e in container["env"]}
        assert "OPENSANDBOX_EGRESS_TOKEN" in env_vars
        assert token == env_vars["OPENSANDBOX_EGRESS_TOKEN"]
        assert len(env_vars["OPENSANDBOX_EGRESS_TOKEN"]) == 64  # hex(32) = 64 chars
        assert env_vars["OPENSANDBOX_EGRESS_HTTP_ADDR"] == "127.0.0.1:18080"
        assert env_vars["OPENSANDBOX_EGRESS_MODE"] == "dns+nft"

    def test_includes_readiness_probe(self):
        """Test that container includes a readiness probe with correct config."""
        egress_image = "opensandbox/egress:v1.0.1"
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        container, _token = build_egress_sidecar_container(egress_image, network_policy)

        assert "readinessProbe" in container
        probe = container["readinessProbe"]
        assert probe["exec"]["command"] == [
            "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:18080/healthz",
        ]
        assert probe["initialDelaySeconds"] == 1
        assert probe["periodSeconds"] == 2
        assert probe["failureThreshold"] == 5
        assert probe["timeoutSeconds"] == 1

    def test_auth_token_is_unique_per_call(self):
        """Test that each call generates a unique auth token."""
        network_policy = NetworkPolicy(default_action="deny", egress=[])
        _c1, t1 = build_egress_sidecar_container("img:v1", network_policy)
        _c2, t2 = build_egress_sidecar_container("img:v1", network_policy)
        assert t1 != t2


class TestBuildSecurityContextForMainContainer:
    """Tests for build_security_context_for_sandbox_container function."""

    def test_non_overlay_sets_allow_privilege_escalation_false(self):
        """Test that non-overlay mode sets allowPrivilegeEscalation to False."""
        result = build_security_context_for_sandbox_container(
            has_network_policy=False, is_overlay_mode=False,
        )
        assert result["allowPrivilegeEscalation"] is False

    def test_overlay_mode_does_not_set_allow_privilege_escalation(self):
        """Test that overlay mode does NOT set allowPrivilegeEscalation."""
        result = build_security_context_for_sandbox_container(
            has_network_policy=False, is_overlay_mode=True,
        )
        assert "allowPrivilegeEscalation" not in result

    def test_drops_net_admin_when_network_policy_enabled(self):
        """Test that NET_ADMIN is dropped when network policy is enabled."""
        result = build_security_context_for_sandbox_container(has_network_policy=True)

        assert "capabilities" in result
        assert "drop" in result["capabilities"]
        assert "NET_ADMIN" in result["capabilities"]["drop"]

    def test_non_overlay_with_network_policy_has_both(self):
        """Test that non-overlay + network policy combines both settings."""
        result = build_security_context_for_sandbox_container(
            has_network_policy=True, is_overlay_mode=False,
        )
        assert result["allowPrivilegeEscalation"] is False
        assert "NET_ADMIN" in result["capabilities"]["drop"]


class TestApplyEgressToSpec:
    """Tests for apply_egress_to_spec function."""

    def test_adds_egress_sidecar_container(self):
        """Test that egress sidecar container is added to containers list."""
        pod_spec: dict = {}
        containers: list = []
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )
        egress_image = "opensandbox/egress:v1.0.1"

        token = apply_egress_to_spec(
            pod_spec=pod_spec,
            containers=containers,
            network_policy=network_policy,
            egress_image=egress_image,
        )

        assert len(containers) == 1
        assert containers[0]["name"] == "egress"
        assert containers[0]["image"] == egress_image
        assert isinstance(token, str)
        assert len(token) == 64

    def test_no_op_when_no_network_policy(self):
        """Test that function does nothing when network_policy is None."""
        pod_spec: dict = {}
        containers: list = []

        token = apply_egress_to_spec(
            pod_spec=pod_spec,
            containers=containers,
            network_policy=None,
            egress_image="opensandbox/egress:v1.0.1",
        )

        assert len(containers) == 0
        assert "securityContext" not in pod_spec
        assert token is None

    def test_no_op_when_no_egress_image(self):
        """Test that function does nothing when egress_image is None."""
        pod_spec: dict = {}
        containers: list = []
        network_policy = NetworkPolicy(
            default_action="deny",
            egress=[NetworkRule(action="allow", target="example.com")],
        )

        token = apply_egress_to_spec(
            pod_spec=pod_spec,
            containers=containers,
            network_policy=network_policy,
            egress_image=None,
        )

        assert len(containers) == 0
        assert "securityContext" not in pod_spec
        assert token is None
