"""Tests for PolicyPusher from src/services/k8s/policy_pusher.py."""

import json
import re
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

from src.api.schema import NetworkPolicy, NetworkRule
from src.services.k8s.network_access import NetworkAccessConfig
from src.services.k8s.policy_pusher import PolicyPusher


def _make_pod(name, sandbox_id, labels=None, phase="Running"):
    """Create a mock pod object."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = "default"
    all_labels = {
        "opensandbox.io/id": sandbox_id,
        **(labels or {}),
    }
    pod.metadata.labels = all_labels
    pod.status.phase = phase
    pod.status.pod_ip = "10.0.0.1"
    return pod


def _extract_payload_from_command(command):
    """Extract JSON payload from sh -c command."""
    # command is ["sh", "-c", "wget ... --post-data '{json}' ..."]
    shell_cmd = command[2]
    match = re.search(r"--post-data '(\{.*?\})'", shell_cmd)
    assert match, f"Could not find --post-data in: {shell_cmd}"
    return json.loads(match.group(1))


@pytest.fixture
def network_config(tmp_path):
    """Create a NetworkAccessConfig with sample data."""
    p = tmp_path / "network-access.yaml"
    data = {
        "metadataKeys": {"workspace": "workspace_id"},
        "policies": [
            {
                "scope": {"type": "workspace", "id": "ws-1"},
                "defaultAction": "deny",
                "egress": [{"action": "allow", "target": "pypi.org"}],
            },
            {
                "scope": {"type": "default"},
                "defaultAction": "deny",
                "egress": [{"action": "allow", "target": "example.com"}],
            },
        ],
    }
    p.write_text(yaml.dump(data))
    return NetworkAccessConfig(str(p))


class TestPolicyPusher:
    """Tests for PolicyPusher."""

    def test_push_all_no_pods(self, network_config):
        """push_all does nothing when no pods exist."""
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = []

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: "token123",
        )
        pusher._push_all_sync()

        core_v1.list_namespaced_pod.assert_called_once_with(
            namespace="default",
            label_selector="opensandbox.io/egress-sidecar=true",
        )

    def test_push_all_skips_non_running_pods(self, network_config):
        """push_all skips pods that are not Running."""
        pod = _make_pod("pod-1", "sb-1", phase="Pending")
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = [pod]

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: "token123",
        )
        pusher._push_all_sync()

        # No exec should happen for non-running pods
        core_v1.connect_get_namespaced_pod_exec.assert_not_called()

    @patch("src.services.k8s.policy_pusher.stream")
    def test_push_to_pod_resolves_policy(self, mock_stream, network_config):
        """push_to_pod resolves correct policy based on pod labels."""
        pod = _make_pod("pod-1", "sb-1", labels={"workspace_id": "ws-1"})
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = [pod]

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: "token123",
        )
        pusher._push_all_sync()

        mock_stream.assert_called_once()
        call_args = mock_stream.call_args
        command = call_args.kwargs.get("command") or call_args[1].get("command")
        payload = _extract_payload_from_command(command)
        assert payload["defaultAction"] == "deny"
        assert any(r["target"] == "pypi.org" for r in payload["egress"])

    @patch("src.services.k8s.policy_pusher.stream")
    def test_push_to_pod_allow_all_fallback(self, mock_stream, network_config):
        """When resolve returns None, push allow-all policy."""
        # Pod with no matching workspace
        pod = _make_pod("pod-1", "sb-1", labels={})
        # Use config where default is allow-all
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = [pod]

        from src.services.k8s.network_access import NetworkAccessConfig
        import tempfile, os

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({
                "policies": [
                    {"scope": {"type": "default"}, "defaultAction": "allow", "egress": []},
                ],
            }, f)
            config_path = f.name

        try:
            cfg = NetworkAccessConfig(config_path)
            pusher = PolicyPusher(
                core_v1_api=core_v1,
                namespace="default",
                network_access_config=cfg,
                get_egress_token_fn=lambda sid, ns: "token123",
            )
            pusher._push_all_sync()

            mock_stream.assert_called_once()
            call_args = mock_stream.call_args
            command = call_args.kwargs.get("command") or call_args[1].get("command")
            payload = _extract_payload_from_command(command)
            assert payload["defaultAction"] == "allow"
            assert payload["egress"] == []
        finally:
            os.unlink(config_path)

    def test_push_skips_pod_without_token(self, network_config):
        """push skips pod when no egress token found."""
        pod = _make_pod("pod-1", "sb-1")
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = [pod]

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: None,
        )
        pusher._push_all_sync()

        # No exec should happen
        core_v1.connect_get_namespaced_pod_exec.assert_not_called()

    def test_push_skips_pod_without_sandbox_id(self, network_config):
        """push skips pod when sandbox-id label is missing."""
        pod = _make_pod("pod-1", "sb-1")
        pod.metadata.labels = {}  # no sandbox-id label
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = [pod]

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: "token123",
        )
        pusher._push_all_sync()

    @patch("src.services.k8s.policy_pusher.stream")
    def test_push_continues_on_individual_failure(self, mock_stream, network_config):
        """push_all continues when one pod fails."""
        pod1 = _make_pod("pod-1", "sb-1")
        pod2 = _make_pod("pod-2", "sb-2")
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = [pod1, pod2]

        mock_stream.side_effect = [Exception("exec failed"), "ok"]

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: "token123",
        )
        pusher._push_all_sync()

        assert mock_stream.call_count == 2

    def test_push_all_handles_list_failure(self, network_config):
        """push_all handles failure to list pods gracefully."""
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.side_effect = Exception("API error")

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: "token123",
        )
        # Should not raise
        pusher._push_all_sync()

    @patch("src.services.k8s.policy_pusher.stream")
    def test_push_sends_correct_auth_header(self, mock_stream, network_config):
        """push includes OPENSANDBOX-EGRESS-AUTH header."""
        pod = _make_pod("pod-1", "sb-1")
        core_v1 = MagicMock()
        core_v1.list_namespaced_pod.return_value.items = [pod]

        pusher = PolicyPusher(
            core_v1_api=core_v1,
            namespace="default",
            network_access_config=network_config,
            get_egress_token_fn=lambda sid, ns: "secret-token-42",
        )
        pusher._push_all_sync()

        call_args = mock_stream.call_args
        command = call_args.kwargs.get("command") or call_args[1].get("command")
        shell_cmd = command[2]
        assert "OPENSANDBOX-EGRESS-AUTH: secret-token-42" in shell_cmd
