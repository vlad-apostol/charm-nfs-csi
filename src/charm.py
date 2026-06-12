#!/usr/bin/env python3
# Copyright 2026 vlad.apostol@canonical.com
# See LICENSE file for licensing details.

"""NFS CSI subordinate charm for Kubernetes integration."""

import logging
from pathlib import Path

import ops
from ops.interface_kube_control import KubeControlRequirer
from pydantic import BaseModel, ConfigDict, ValidationError

from nfs_csi import NfsCsiManager

logger = logging.getLogger(__name__)

# Kubernetes RBAC group granting cluster-admin privileges — required so the charm
# can create and manage cluster-wide resources.
_KUBE_CONTROL_GROUP = "system:masters"


class CharmConfig(BaseModel):
    """Validated charm configuration."""

    model_config = ConfigDict(populate_by_name=True)

    namespace: str
    deploy_external_snapshotter: bool


class NfsCsiCharm(ops.CharmBase):
    """NFS CSI subordinate charm.

    Attaches to a Kubernetes principal (e.g. kubernetes-control-plane) via the juju-info
    relation and obtains cluster credentials via the kube-control interface.

    Only the elected leader unit performs Helm deployments; all other units remain on
    standby and take over automatically if the leader is lost.
    """

    _state = ops.StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        # Persist the namespace actually deployed so we can detect and block changes.
        self._state.set_default(deployed_namespace=None)

        self._kubeconfig_path = Path(self.charm_dir) / ".kube" / "config"
        self._ca_path = Path(self.charm_dir) / ".kube" / "ca.crt"

        self.kube_control = KubeControlRequirer(self, "kube-control", schemas="0,1")

        self.manager = NfsCsiManager(
            charm_dir=Path(self.charm_dir),
            app_name=self.app.name,
            model_name=self.model.name,
            kubeconfig_path=self._kubeconfig_path,
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.leader_elected, self._on_leader_elected)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self.on.remove, self._on_remove)
        framework.observe(self.on.kube_control_relation_joined, self._on_kube_control_joined)
        framework.observe(self.on.kube_control_relation_changed, self._on_kube_control_changed)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _k8s_user(self) -> str:
        """Return a stable Kubernetes username for this unit's auth token request."""
        return f"nfs-csi-{self.unit.name.replace('/', '-')}"

    def _setup_kubeconfig(self) -> bool:
        """Write kubeconfig from kube-control relation data.

        Returns:
            True if the kubeconfig was written successfully; False if credentials
            are not yet available.
        """
        try:
            self._kubeconfig_path.parent.mkdir(parents=True, exist_ok=True)
            self.kube_control.create_kubeconfig(
                ca=self._ca_path,
                kubeconfig=self._kubeconfig_path,
                user="admin",
                k8s_user=self._k8s_user(),
            )
            return True
        except Exception as e:
            logger.debug("Kubeconfig not yet available: %s", e)
            return False

    def _parse_config(self) -> CharmConfig | None:
        """Parse and validate charm config, setting BlockedStatus on error.

        Returns:
            Validated CharmConfig, or None if validation failed.
        """
        raw = {
            "namespace": str(self.config["nfs-csi-namespace"]),
            "deploy_external_snapshotter": bool(self.config["deploy-external-snapshotter"]),
        }
        try:
            return CharmConfig(**raw)
        except ValidationError as e:
            messages = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            self.unit.status = ops.BlockedStatus(f"Invalid config: {messages}")
            return None

    def _configure(self) -> None:
        """Run a full configure cycle.

        Preconditions (callers must verify):
          - ``self.unit.is_leader()`` is True
          - ``self._kubeconfig_path.exists()`` is True
        """
        cfg = self._parse_config()
        if cfg is None:
            return

        # Enforce namespace immutability after the first successful deploy.
        deployed_ns = self._state.deployed_namespace
        if deployed_ns and deployed_ns != cfg.namespace:
            self.unit.status = ops.BlockedStatus(
                f"nfs-csi-namespace cannot be changed from '{deployed_ns}' to "
                f"'{cfg.namespace}' after deploy — remove and re-deploy to change."
            )
            return

        self.unit.status = ops.MaintenanceStatus("Configuring NFS CSI")
        try:
            self.manager.reset_client()
            self.manager.configure(cfg.model_dump())
            self._state.deployed_namespace = cfg.namespace
            self.unit.status = ops.ActiveStatus("NFS CSI configured")
        except Exception as e:
            logger.error("Configuration failed: %s", e)
            self.unit.status = ops.BlockedStatus(f"Configuration failed: {e}")

    # -------------------------------------------------------------------------
    # Core event handlers
    # -------------------------------------------------------------------------

    def _on_install(self, event: ops.InstallEvent) -> None:
        """Install prerequisites on every unit — any unit may become leader."""
        self.unit.status = ops.MaintenanceStatus("Installing prerequisites")
        try:
            self.manager.install()
            self.unit.status = ops.ActiveStatus("Prerequisites installed")
        except RuntimeError as e:
            logger.error("Installation failed: %s", e)
            self.unit.status = ops.BlockedStatus(f"Installation failed: {e}")

    def _on_leader_elected(self, event: ops.LeaderElectedEvent) -> None:
        """Take over deployments after winning leader election."""
        if not self._setup_kubeconfig():
            self.unit.status = ops.WaitingStatus("Waiting for kube-control credentials")
            return
        self._configure()

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Apply updated configuration — leader only."""
        if not self.unit.is_leader():
            self.unit.status = ops.ActiveStatus("Standby — leader handles deployments")
            return
        if not self._kubeconfig_path.exists():
            self.unit.status = ops.WaitingStatus("Waiting for kube-control credentials")
            return
        self._configure()

    def _on_update_status(self, event: ops.UpdateStatusEvent) -> None:
        """Refresh the unit status."""
        if not self.unit.is_leader():
            self.unit.status = ops.ActiveStatus("Standby — leader handles deployments")
            return
        if not self._kubeconfig_path.exists():
            self.unit.status = ops.WaitingStatus("Waiting for kube-control credentials")
            return
        try:
            namespace = self._state.deployed_namespace or str(self.config["nfs-csi-namespace"])
            if self.manager.is_ready(namespace):
                self.unit.status = ops.ActiveStatus("NFS CSI ready")
            else:
                self.unit.status = ops.WaitingStatus("NFS CSI not ready")
        except Exception as e:
            logger.warning("Status check failed: %s", e)
            self.unit.status = ops.UnknownStatus()

    def _on_remove(self, event: ops.RemoveEvent) -> None:
        """Remove NFS CSI components — leader only."""
        if not self.unit.is_leader():
            return
        if not self._kubeconfig_path.exists():
            logger.info("No kubeconfig present; skipping Kubernetes resource cleanup")
            return
        # Use the stored namespace so we clean up the right release even if config changed.
        namespace = self._state.deployed_namespace or str(self.config["nfs-csi-namespace"])
        self.unit.status = ops.MaintenanceStatus("Removing NFS CSI components")
        try:
            self.manager.remove({"namespace": namespace})
        except Exception as e:
            logger.error("Removal failed: %s", e)

    # -------------------------------------------------------------------------
    # kube-control relation handlers
    # -------------------------------------------------------------------------

    def _on_kube_control_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Request a Kubernetes auth token from the control-plane."""
        self.kube_control.set_auth_request(
            user=self._k8s_user(),
            group=_KUBE_CONTROL_GROUP,
        )
        self.unit.status = ops.WaitingStatus("Waiting for kube-control credentials")

    def _on_kube_control_changed(self, event: ops.RelationChangedEvent) -> None:
        """Process updated credentials from the control-plane."""
        if not self.unit.is_leader():
            self.unit.status = ops.ActiveStatus("Standby — leader handles deployments")
            return
        if not self._setup_kubeconfig():
            self.unit.status = ops.WaitingStatus("Waiting for kube-control credentials")
            return
        self._configure()


if __name__ == "__main__":  # pragma: nocover
    ops.main(NfsCsiCharm)
