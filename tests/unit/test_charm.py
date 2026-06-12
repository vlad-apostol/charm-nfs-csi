# Copyright 2026 vlad.apostol@canonical.com
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
from ops import testing

from charm import NfsCsiCharm


def test_install(monkeypatch: pytest.MonkeyPatch):
    """All units install prerequisites regardless of leadership."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State()

    install_called = []
    monkeypatch.setattr(
        "nfs_csi.NfsCsiManager.install", lambda self: install_called.append(True)
    )

    state_out = ctx.run(ctx.on.install(), state_in)

    assert len(install_called) == 1
    assert state_out.unit_status == testing.ActiveStatus("Prerequisites installed")


def test_config_changed_leader_with_kubeconfig(monkeypatch: pytest.MonkeyPatch):
    """Leader with kubeconfig runs configure."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=True)

    configure_called = []
    monkeypatch.setattr(
        "nfs_csi.NfsCsiManager.configure",
        lambda self, cfg: configure_called.append(cfg),
    )
    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)
    monkeypatch.setattr("nfs_csi.NfsCsiManager.reset_client", lambda self: None)

    _orig_exists = Path.exists

    def _mock_exists(self: Path) -> bool:
        if self.name == "config" and self.parent.name == ".kube":
            return True
        return _orig_exists(self)

    monkeypatch.setattr(Path, "exists", _mock_exists)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert len(configure_called) == 1
    assert state_out.unit_status == testing.ActiveStatus("NFS CSI configured")

    config = configure_called[0]
    assert config["namespace"] == "kube-system"
    assert config["deploy_external_snapshotter"] is True


def test_namespace_immutability_blocks_change(monkeypatch: pytest.MonkeyPatch):
    """Leader is blocked if nfs-csi-namespace is changed after first deploy."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(
        leader=True,
        stored_states=[
            testing.StoredState(
                owner_path="NfsCsiCharm",
                name="_state",
                content={"deployed_namespace": "kube-system"},
            )
        ],
    )

    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)
    monkeypatch.setattr("nfs_csi.NfsCsiManager.reset_client", lambda self: None)

    _orig_exists = Path.exists

    def _mock_exists(self: Path) -> bool:
        if self.name == "config" and self.parent.name == ".kube":
            return True
        return _orig_exists(self)

    monkeypatch.setattr(Path, "exists", _mock_exists)

    state_out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            leader=True,
            config={"nfs-csi-namespace": "different-ns"},
            stored_states=[
                testing.StoredState(
                    owner_path="NfsCsiCharm",
                    name="_state",
                    content={"deployed_namespace": "kube-system"},
                )
            ],
        ),
    )

    assert state_out.unit_status.name == "blocked"
    assert "cannot be changed" in state_out.unit_status.message


def test_config_changed_non_leader(monkeypatch: pytest.MonkeyPatch):
    """Non-leader units skip configure and report standby status."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=False)

    configure_called = []
    monkeypatch.setattr(
        "nfs_csi.NfsCsiManager.configure",
        lambda self, cfg: configure_called.append(cfg),
    )
    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert len(configure_called) == 0
    assert state_out.unit_status == testing.ActiveStatus(
        "Standby \u2014 leader handles deployments"
    )


def test_config_changed_leader_no_kubeconfig(monkeypatch: pytest.MonkeyPatch):
    """Leader waits when kube-control credentials are not yet available."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=True)

    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.unit_status == testing.WaitingStatus("Waiting for kube-control credentials")


def _mock_kubeconfig_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch Path.exists so the charm's .kube/config appears present."""
    _orig_exists = Path.exists

    def _mock(self: Path) -> bool:
        if self.name == "config" and self.parent.name == ".kube":
            return True
        return _orig_exists(self)

    monkeypatch.setattr(Path, "exists", _mock)


def test_update_status_ready(monkeypatch: pytest.MonkeyPatch):
    """Leader reports ready status when NFS CSI is ready."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=True)

    monkeypatch.setattr("nfs_csi.NfsCsiManager.is_ready", lambda self, ns: True)
    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)
    _mock_kubeconfig_exists(monkeypatch)

    state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.ActiveStatus("NFS CSI ready")


def test_update_status_not_ready(monkeypatch: pytest.MonkeyPatch):
    """Leader reports waiting status when NFS CSI is not ready."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=True)

    monkeypatch.setattr("nfs_csi.NfsCsiManager.is_ready", lambda self, ns: False)
    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)
    _mock_kubeconfig_exists(monkeypatch)

    state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.WaitingStatus("NFS CSI not ready")


def test_update_status_no_kubeconfig(monkeypatch: pytest.MonkeyPatch):
    """Leader waits when kubeconfig is absent instead of crashing."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=True)

    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)
    # No kubeconfig mock — file does not exist

    state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.WaitingStatus("Waiting for kube-control credentials")


def test_update_status_non_leader(monkeypatch: pytest.MonkeyPatch):
    """Non-leader reports standby on update-status."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=False)

    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)

    state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.ActiveStatus(
        "Standby \u2014 leader handles deployments"
    )


def test_remove_leader(monkeypatch: pytest.MonkeyPatch):
    """Leader passes the correct config to manager.remove."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=True)

    remove_called = []
    monkeypatch.setattr(
        "nfs_csi.NfsCsiManager.remove",
        lambda self, cfg: remove_called.append(cfg),
    )
    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)
    _mock_kubeconfig_exists(monkeypatch)

    ctx.run(ctx.on.remove(), state_in)

    assert len(remove_called) == 1
    assert remove_called[0]["namespace"] == "kube-system"


def test_remove_non_leader(monkeypatch: pytest.MonkeyPatch):
    """Non-leader units skip the remove operation entirely."""
    ctx = testing.Context(NfsCsiCharm)
    state_in = testing.State(leader=False)

    remove_called = []
    monkeypatch.setattr(
        "nfs_csi.NfsCsiManager.remove",
        lambda self, cfg: remove_called.append(cfg),
    )
    monkeypatch.setattr("nfs_csi.NfsCsiManager.install", lambda self: None)

    ctx.run(ctx.on.remove(), state_in)

    assert len(remove_called) == 0
