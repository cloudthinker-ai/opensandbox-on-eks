"""Tests for NetworkAccessConfig from src/services/k8s/network_access.py."""

import logging
import time

import pytest
import yaml

from src.services.k8s.network_access import NetworkAccessConfig


LOGGER_NAME = "src.services.k8s.network_access"


@pytest.fixture(autouse=True)
def _enable_log_propagation():
    """Ensure the logger propagates so caplog can capture messages.

    The app's logging config sets propagate=False on the 'src' logger,
    which prevents caplog from seeing messages from child loggers.
    """
    src_log = logging.getLogger("src")
    orig_src = src_log.propagate
    src_log.propagate = True
    yield
    src_log.propagate = orig_src


SAMPLE_CONFIG = {
    "metadataKeys": {"workspace": "workspace_id", "org": "org_id"},
    "policies": [
        {
            "scope": {"type": "workspace", "id": "ws-1"},
            "defaultAction": "deny",
            "egress": [{"action": "allow", "target": "pypi.org"}],
        },
        {
            "scope": {"type": "org", "id": "org-1"},
            "defaultAction": "deny",
            "egress": [{"action": "allow", "target": "*.github.com"}],
        },
        {
            "scope": {"type": "default"},
            "defaultAction": "allow",
            "egress": [],
        },
    ],
}


@pytest.fixture
def config_file(tmp_path):
    """Write a YAML config and return path."""

    def _write(data):
        p = tmp_path / "network-access.yaml"
        p.write_text(yaml.dump(data))
        return str(p)

    return _write


# ============================================================================
# Load Tests
# ============================================================================


class TestNetworkAccessConfigLoad:
    """Tests for loading config files."""

    def test_load_valid_config(self, config_file):
        cfg = NetworkAccessConfig(config_file(SAMPLE_CONFIG))
        assert len(cfg._policies) == 3
        assert cfg._metadata_keys == {"workspace": "workspace_id", "org": "org_id"}

    def test_load_missing_file(self, tmp_path):
        cfg = NetworkAccessConfig(str(tmp_path / "nonexistent.yaml"))
        assert cfg._policies == []

    def test_load_non_mapping_yaml(self, tmp_path):
        """YAML that parses to a non-dict type (string) is handled gracefully."""
        p = tmp_path / "network-access.yaml"
        p.write_text("just a string")
        cfg = NetworkAccessConfig(str(p))
        assert cfg._policies == []

    def test_load_detects_duplicate_scopes(self, config_file, caplog):
        data = {
            "policies": [
                {"scope": {"type": "default"}, "defaultAction": "deny", "egress": []},
                {"scope": {"type": "default"}, "defaultAction": "allow", "egress": []},
            ],
        }
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            NetworkAccessConfig(config_file(data))
        assert "Duplicate scope" in caplog.text


# ============================================================================
# Resolve Tests
# ============================================================================


class TestNetworkAccessConfigResolve:
    """Tests for policy resolution logic."""

    def test_resolve_workspace_scope(self, config_file):
        cfg = NetworkAccessConfig(config_file(SAMPLE_CONFIG))
        policy = cfg.resolve({"workspace_id": "ws-1", "org_id": "org-1"})
        assert policy is not None
        assert policy.default_action == "deny"
        assert len(policy.egress) == 1
        assert policy.egress[0].target == "pypi.org"

    def test_resolve_org_scope(self, config_file):
        cfg = NetworkAccessConfig(config_file(SAMPLE_CONFIG))
        policy = cfg.resolve({"org_id": "org-1"})
        assert policy is not None
        assert policy.default_action == "deny"
        assert policy.egress[0].target == "*.github.com"

    def test_resolve_default_scope(self, config_file):
        """Default allow-all with no rules returns None (skip sidecar)."""
        cfg = NetworkAccessConfig(config_file(SAMPLE_CONFIG))
        policy = cfg.resolve({"workspace_id": "ws-unknown", "org_id": "org-unknown"})
        # default is allow with no rules -> None
        assert policy is None

    def test_resolve_workspace_over_org_priority(self, config_file):
        cfg = NetworkAccessConfig(config_file(SAMPLE_CONFIG))
        policy = cfg.resolve({"workspace_id": "ws-1", "org_id": "org-1"})
        # workspace should win
        assert policy is not None
        assert policy.egress[0].target == "pypi.org"

    def test_resolve_no_policies_returns_none(self, config_file):
        cfg = NetworkAccessConfig(config_file({"policies": []}))
        assert cfg.resolve({"workspace_id": "ws-1"}) is None

    def test_resolve_no_metadata_returns_default(self, config_file):
        """None/empty metadata falls through to default scope."""
        cfg = NetworkAccessConfig(config_file(SAMPLE_CONFIG))
        # Default is allow-all with empty egress -> None (skip sidecar)
        assert cfg.resolve(None) is None
        assert cfg.resolve({}) is None

    def test_resolve_allow_all_default_returns_none(self, config_file):
        data = {
            "policies": [
                {"scope": {"type": "default"}, "defaultAction": "allow", "egress": []},
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        assert cfg.resolve({}) is None

    def test_resolve_allow_all_workspace_returns_none(self, config_file):
        data = {
            "policies": [
                {"scope": {"type": "workspace", "id": "ws-1"}, "defaultAction": "allow", "egress": []},
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        assert cfg.resolve({"workspace_id": "ws-1"}) is None

    def test_resolve_allow_all_with_rules_returns_policy(self, config_file):
        data = {
            "policies": [
                {
                    "scope": {"type": "default"},
                    "defaultAction": "allow",
                    "egress": [{"action": "deny", "target": "*.evil.com"}],
                },
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        policy = cfg.resolve({})
        assert policy is not None
        assert policy.default_action == "allow"
        assert len(policy.egress) == 1

    def test_resolve_custom_metadata_keys(self, config_file):
        data = {
            "metadataKeys": {"workspace": "ws_key", "org": "org_key"},
            "policies": [
                {"scope": {"type": "workspace", "id": "ws-x"}, "defaultAction": "deny", "egress": []},
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        # Should match using custom key
        policy = cfg.resolve({"ws_key": "ws-x"})
        assert policy is not None
        assert policy.default_action == "deny"
        # Should NOT match using default key
        assert cfg.resolve({"workspace_id": "ws-x"}) is None

    def test_resolve_unmatched_scope_falls_through(self, config_file):
        data = {
            "policies": [
                {"scope": {"type": "workspace", "id": "ws-1"}, "defaultAction": "deny", "egress": []},
                {"scope": {"type": "default"}, "defaultAction": "deny", "egress": []},
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        # ws-2 doesn't match ws-1 workspace policy, falls to default
        policy = cfg.resolve({"workspace_id": "ws-2"})
        assert policy is not None
        assert policy.default_action == "deny"


# ============================================================================
# Validation Tests
# ============================================================================


class TestNetworkAccessConfigValidation:
    """Tests for action validation during policy conversion."""

    def test_invalid_default_action_falls_back_to_deny(self, config_file, caplog):
        data = {
            "policies": [
                {"scope": {"type": "default"}, "defaultAction": "alow", "egress": []},
            ],
        }
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            cfg = NetworkAccessConfig(config_file(data))
            policy = cfg.resolve({})
        assert policy is not None
        assert policy.default_action == "deny"
        assert "Invalid defaultAction" in caplog.text

    def test_invalid_rule_action_skipped(self, config_file, caplog):
        data = {
            "policies": [
                {
                    "scope": {"type": "default"},
                    "defaultAction": "deny",
                    "egress": [
                        {"action": "block", "target": "bad.com"},
                        {"action": "allow", "target": "good.com"},
                    ],
                },
            ],
        }
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            cfg = NetworkAccessConfig(config_file(data))
            policy = cfg.resolve({})
        assert policy is not None
        assert len(policy.egress) == 1
        assert policy.egress[0].target == "good.com"
        assert "invalid action" in caplog.text.lower()

    def test_malformed_rule_missing_action_skipped(self, config_file, caplog):
        data = {
            "policies": [
                {
                    "scope": {"type": "default"},
                    "defaultAction": "deny",
                    "egress": [{"target": "no-action.com"}],
                },
            ],
        }
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            cfg = NetworkAccessConfig(config_file(data))
            policy = cfg.resolve({})
        assert policy is not None
        assert len(policy.egress) == 0
        assert "malformed" in caplog.text.lower()

    def test_malformed_rule_missing_target_skipped(self, config_file, caplog):
        data = {
            "policies": [
                {
                    "scope": {"type": "default"},
                    "defaultAction": "deny",
                    "egress": [{"action": "allow"}],
                },
            ],
        }
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            cfg = NetworkAccessConfig(config_file(data))
            policy = cfg.resolve({})
        assert policy is not None
        assert len(policy.egress) == 0
        assert "malformed" in caplog.text.lower()


# ============================================================================
# DeniedCIDRs Injection Tests
# ============================================================================


class TestDeniedCIDRsInjection:
    """Tests for auto-injection of deniedCIDRs into resolved policies."""

    def test_denied_cidrs_appended_to_policy_with_rules(self, config_file):
        data = {
            "deniedCIDRs": ["10.0.0.0/8", "172.16.0.0/12", "169.254.0.0/16"],
            "policies": [
                {
                    "scope": {"type": "workspace", "id": "ws-1"},
                    "defaultAction": "allow",
                    "egress": [{"action": "allow", "target": "192.168.65.254"}],
                },
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        policy = cfg.resolve({"workspace_id": "ws-1"})
        assert policy is not None
        # 1 user rule + 3 denied CIDRs
        assert len(policy.egress) == 4
        assert policy.egress[0].target == "192.168.65.254"
        assert policy.egress[0].action == "allow"
        assert policy.egress[1].target == "10.0.0.0/8"
        assert policy.egress[1].action == "deny"
        assert policy.egress[2].target == "172.16.0.0/12"
        assert policy.egress[2].action == "deny"
        assert policy.egress[3].target == "169.254.0.0/16"
        assert policy.egress[3].action == "deny"

    def test_allow_all_skip_preserved_with_denied_cidrs(self, config_file):
        """Allow-all with no user rules should still skip sidecar (return None)."""
        data = {
            "deniedCIDRs": ["10.0.0.0/8"],
            "policies": [
                {
                    "scope": {"type": "default"},
                    "defaultAction": "allow",
                    "egress": [],
                },
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        assert cfg.resolve({}) is None

    def test_no_denied_cidrs_no_extra_rules(self, config_file):
        data = {
            "policies": [
                {
                    "scope": {"type": "default"},
                    "defaultAction": "deny",
                    "egress": [{"action": "allow", "target": "pypi.org"}],
                },
            ],
        }
        cfg = NetworkAccessConfig(config_file(data))
        policy = cfg.resolve({})
        assert policy is not None
        assert len(policy.egress) == 1


# ============================================================================
# Reload / Watcher Tests
# ============================================================================


class TestNetworkAccessConfigReload:
    """Tests for reload_if_changed() and watcher."""

    def test_reload_if_changed_detects_update(self, tmp_path):
        p = tmp_path / "network-access.yaml"
        data = {
            "policies": [
                {"scope": {"type": "default"}, "defaultAction": "deny", "egress": []},
            ],
        }
        p.write_text(yaml.dump(data))
        cfg = NetworkAccessConfig(str(p))
        assert cfg.resolve({}) is not None
        assert cfg.resolve({}).default_action == "deny"

        # Update config
        data["policies"][0]["defaultAction"] = "allow"
        data["policies"][0]["egress"] = [{"action": "deny", "target": "evil.com"}]
        time.sleep(0.05)  # ensure mtime changes
        p.write_text(yaml.dump(data))

        assert cfg.reload_if_changed() is True
        policy = cfg.resolve({})
        assert policy is not None
        assert policy.default_action == "allow"

    def test_reload_if_changed_no_change(self, tmp_path):
        p = tmp_path / "network-access.yaml"
        data = {"policies": []}
        p.write_text(yaml.dump(data))
        cfg = NetworkAccessConfig(str(p))
        assert cfg.reload_if_changed() is False

    def test_reload_if_changed_fires_callbacks(self, tmp_path):
        p = tmp_path / "network-access.yaml"
        data = {"policies": []}
        p.write_text(yaml.dump(data))
        cfg = NetworkAccessConfig(str(p))

        callback_count = [0]

        def on_change():
            callback_count[0] += 1

        cfg.on_reload(on_change)

        time.sleep(0.05)
        p.write_text(yaml.dump({"policies": []}))

        cfg.reload_if_changed()
        assert callback_count[0] == 1

    def test_reload_missing_file_returns_false(self, tmp_path):
        cfg = NetworkAccessConfig(str(tmp_path / "nonexistent.yaml"))
        assert cfg.reload_if_changed() is False

    def test_start_and_stop_watcher(self, tmp_path):
        p = tmp_path / "network-access.yaml"
        p.write_text(yaml.dump({"policies": []}))
        cfg = NetworkAccessConfig(str(p))
        cfg.start_watcher(poll_interval=0.1)
        assert cfg._watcher_thread is not None
        assert cfg._watcher_thread.is_alive()
        cfg.stop_watcher()
        assert cfg._watcher_thread is None

    def test_watcher_detects_change(self, tmp_path):
        p = tmp_path / "network-access.yaml"
        data = {"policies": [{"scope": {"type": "default"}, "defaultAction": "deny", "egress": []}]}
        p.write_text(yaml.dump(data))
        cfg = NetworkAccessConfig(str(p))

        detected = [False]

        def on_change():
            detected[0] = True

        cfg.on_reload(on_change)
        cfg.start_watcher(poll_interval=0.1)

        try:
            time.sleep(0.15)
            data["policies"][0]["egress"] = [{"action": "allow", "target": "x.com"}]
            p.write_text(yaml.dump(data))
            time.sleep(0.5)  # wait for watcher to detect
            assert detected[0] is True
        finally:
            cfg.stop_watcher()

    def test_resolve_is_thread_safe(self, tmp_path):
        """Ensure resolve uses the lock (no crash under concurrent access)."""
        p = tmp_path / "network-access.yaml"
        data = {"policies": [{"scope": {"type": "default"}, "defaultAction": "deny", "egress": []}]}
        p.write_text(yaml.dump(data))
        cfg = NetworkAccessConfig(str(p))

        import concurrent.futures

        def resolve_many():
            for _ in range(100):
                cfg.resolve({})

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(resolve_many) for _ in range(4)]
            for f in futures:
                f.result()  # should not raise
