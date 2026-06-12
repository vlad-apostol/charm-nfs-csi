# Copyright 2026 vlad.apostol@canonical.com
# See LICENSE file for licensing details.

"""NFS CSI workload manager for deploying and managing the NFS CSI driver."""

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from charmlibs import snap
from lightkube import Client, KubeConfig
from lightkube.core.client import LabelValue
from lightkube.core.exceptions import ApiError
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace, Node, Pod
from lightkube.types import PatchType

logger = logging.getLogger(__name__)

# Helm chart source
NFS_CSI_CHART = "csi-driver-nfs"
NFS_CSI_REPO = "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts"
NFS_CSI_VERSION = "4.13.2"

# Fixed Helm release name — not user-configurable to keep deployments simple.
NFS_CSI_RELEASE = "nfs-csi"


class NfsCsiManager:
    """Manager for NFS CSI driver deployment."""

    def __init__(self, charm_dir: Path, app_name: str, model_name: str, kubeconfig_path: Path):
        """Initialize the NFS CSI manager.

        Args:
            charm_dir: Path to the charm directory
            app_name: Name of the charm application
            model_name: Name of the Juju model
            kubeconfig_path: Path to the kubeconfig file (written by the kube-control interface)
        """
        self.charm_dir = charm_dir
        self.app_name = app_name
        self.model_name = model_name
        self.manifests_dir = charm_dir / "manifests"
        self.kubeconfig_path = kubeconfig_path
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        """Get or create the Kubernetes client from the kube-control kubeconfig."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def reset_client(self) -> None:
        """Discard the cached client so it is rebuilt from the latest kubeconfig on next use."""
        self._client = None

    def _create_client(self) -> Client:
        """Create a Kubernetes client from the kubeconfig supplied by kube-control."""
        return Client(KubeConfig.from_file(self.kubeconfig_path).get())

    def _create_or_replace(self, resource: Any) -> None:
        """Create a resource, or merge-patch it if one already exists.

        Uses MERGE patch on conflict so the caller does not need to supply
        the current resourceVersion — avoiding the 409-then-replace pitfall
        where a stale resourceVersion would cause a second 409.
        """
        try:
            self.client.create(resource)
        except ApiError as e:
            if e.status.code == 409:
                self.client.patch(
                    type(resource),
                    resource.metadata.name,
                    resource,
                    namespace=resource.metadata.namespace,
                    patch_type=PatchType.MERGE,
                )
            else:
                raise

    def install(self) -> None:
        """Install prerequisite tools (helm snap)."""
        logger.info("Installing prerequisites")
        self._install_helm()
        logger.info("Prerequisites installed")

    def _install_helm(self) -> None:
        """Ensure helm is installed via the snap charmlib."""
        cache = snap.SnapCache()
        helm_snap = cache["helm"]
        if not helm_snap.present:
            logger.info("Installing helm snap")
            snap.add("helm", classic=True)
            logger.info("helm snap installed")
        else:
            logger.info("helm snap already installed (channel: %s)", helm_snap.channel)

    def _helm_upgrade_install(
        self,
        release_name: str,
        chart: str,
        repo: str,
        version: str,
        namespace: str,
        values_file: Path | None = None,
        extra_sets: dict[str, str] | None = None,
    ) -> None:
        """Deploy or upgrade a Helm release from a remote repository.

        Args:
            release_name: Helm release name
            chart: Chart name within the repository
            repo: Helm repository URL
            version: Chart version to install
            namespace: Kubernetes namespace to deploy into
            values_file: Optional path to a values YAML override file
            extra_sets: Optional dict of --set key=value overrides applied after values_file
        """
        cmd = [
            "helm",
            "--kubeconfig",
            str(self.kubeconfig_path),
            "upgrade",
            "--install",
            release_name,
            chart,
            "--repo",
            repo,
            "--version",
            version,
            "--namespace",
            namespace,
            "--create-namespace",
        ]
        if values_file and values_file.exists():
            cmd.extend(["--values", str(values_file)])
        for key, value in (extra_sets or {}).items():
            cmd.extend(["--set", f"{key}={value}"])

        logger.info("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"helm upgrade --install failed for '{release_name}': {result.stderr}"
            )
        logger.info("helm release '%s' deployed successfully", release_name)

    def _helm_uninstall_release(self, release_name: str, namespace: str) -> None:
        """Uninstall a Helm release.

        Args:
            release_name: Helm release name to uninstall
            namespace: Kubernetes namespace the release lives in
        """
        logger.info("Uninstalling helm release '%s' from namespace '%s'", release_name, namespace)
        result = subprocess.run(
            [
                "helm",
                "--kubeconfig",
                str(self.kubeconfig_path),
                "uninstall",
                release_name,
                "--namespace",
                namespace,
                "--ignore-not-found",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "helm uninstall '%s' failed: %s",
                release_name,
                result.stderr,
            )
        else:
            logger.info("Helm release '%s' removed", release_name)

    def configure(self, config: dict[str, Any]) -> None:
        """Configure and deploy the NFS CSI driver.

        Args:
            config: Configuration dictionary with deployment settings
        """
        namespace = config["namespace"]
        deploy_external_snapshotter = config.get("deploy_external_snapshotter", True)

        logger.info("Configuring NFS CSI in namespace %s", namespace)

        self._wait_for_k8s_ready()
        self._ensure_namespace(namespace)
        self._deploy_nfs_csi(namespace, deploy_external_snapshotter=deploy_external_snapshotter)

        logger.info("NFS CSI configuration complete")

    def remove(self, config: dict[str, Any]) -> None:
        """Remove NFS CSI components.

        Args:
            config: Configuration dictionary with deployment settings
        """
        logger.info("Removing NFS CSI components")
        self._helm_uninstall_release(NFS_CSI_RELEASE, config["namespace"])
        logger.info("NFS CSI components removed")

    def is_ready(self, namespace: str) -> bool:
        """Check if the NFS CSI driver is ready.

        Args:
            namespace: Kubernetes namespace where NFS CSI is deployed

        Returns:
            True if NFS CSI is deployed and all pods are Running
        """
        try:
            pods = list(
                self.client.list(
                    Pod,
                    namespace=namespace,
                    labels={"app.kubernetes.io/instance": NFS_CSI_RELEASE},
                )
            )
            if not pods:
                return False
            return all(p.status is not None and p.status.phase == "Running" for p in pods)
        except ApiError as e:
            logger.warning("Failed to check NFS CSI status: %s", e)
            return False

    def _wait_for_k8s_ready(self, timeout: int = 60) -> None:
        """Wait until all nodes are Ready and kube-system pods are Running.

        Args:
            timeout: Maximum seconds to wait before raising RuntimeError.

        Raises:
            RuntimeError: If the cluster is not ready within the timeout.
        """
        logger.info("Checking Kubernetes cluster readiness")
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                nodes = list(self.client.list(Node))
                ready_nodes = [
                    n
                    for n in nodes
                    if n.status
                    and any(
                        c.type == "Ready" and c.status == "True"
                        for c in (n.status.conditions or [])
                    )
                ]
                if nodes and len(ready_nodes) == len(nodes):
                    kube_pods = list(self.client.list(Pod, namespace="kube-system"))
                    unhealthy = [
                        p
                        for p in kube_pods
                        if p.status is None or p.status.phase not in ("Running", "Succeeded")
                    ]
                    if not unhealthy:
                        logger.info(
                            "Kubernetes cluster ready (%d/%d nodes Ready, "
                            "%d kube-system pods healthy)",
                            len(ready_nodes),
                            len(nodes),
                            len(kube_pods),
                        )
                        return
                    logger.debug("%d kube-system pod(s) not yet healthy", len(unhealthy))
                else:
                    logger.debug("%d/%d nodes Ready", len(ready_nodes), len(nodes))
            except ApiError as e:
                logger.debug("Waiting for k8s: %s", e)

            logger.debug("Kubernetes not ready yet, retrying in 10s...")
            time.sleep(10)

        raise RuntimeError(
            f"Kubernetes cluster not ready after {timeout}s. "
            "Ensure the cluster is running and kubeconfig is accessible."
        )

    def _ensure_namespace(self, namespace: str) -> None:
        """Ensure the namespace exists, creating it if needed."""
        ns = Namespace(metadata=ObjectMeta(name=namespace))
        try:
            self.client.create(ns)
            logger.info("Namespace '%s' created", namespace)
        except ApiError as e:
            if e.status.code == 409:
                logger.debug("Namespace '%s' already exists", namespace)
            else:
                raise

    def _deploy_nfs_csi(self, namespace: str, deploy_external_snapshotter: bool = True) -> None:
        """Deploy the NFS CSI driver via Helm from the upstream repository.

        Args:
            namespace: Kubernetes namespace to deploy into
            deploy_external_snapshotter: Whether to deploy the cluster-wide snapshot-controller.
                Set False when the cluster already has one to avoid conflicts.
        """
        values_file = self.manifests_dir / "nfs-csi-values.yaml"

        logger.info("Deploying NFS CSI release '%s'", NFS_CSI_RELEASE)
        self._helm_upgrade_install(
            NFS_CSI_RELEASE,
            NFS_CSI_CHART,
            NFS_CSI_REPO,
            NFS_CSI_VERSION,
            namespace,
            values_file=values_file,
            extra_sets={
                "externalSnapshotter.enabled": "true" if deploy_external_snapshotter else "false"
            },
        )
        self._wait_for_deployment(namespace, {"app.kubernetes.io/instance": NFS_CSI_RELEASE})

    def _wait_for_deployment(
        self, namespace: str, label_selector: dict[str, LabelValue], timeout: int = 300
    ) -> None:
        """Wait for all pods matching a label selector to reach Running phase.

        Args:
            namespace: Kubernetes namespace
            label_selector: Label selector dict (e.g. {'release': 'foo'})
            timeout: Timeout in seconds

        Raises:
            RuntimeError: If pods do not become Running within the timeout, including
                pod phase and container reason (e.g. ImagePullBackOff) to aid debugging.
        """
        logger.info("Waiting for deployment with label '%s' to be ready", label_selector)
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                pods = list(self.client.list(Pod, namespace=namespace, labels=label_selector))
                if pods and all(
                    p.status is not None and p.status.phase == "Running" for p in pods
                ):
                    logger.info("Deployment is ready")
                    return
                if pods:
                    for p in pods:
                        phase = p.status.phase if p.status else "Unknown"
                        reasons = []
                        if p.status and p.status.containerStatuses:
                            for cs in p.status.containerStatuses:
                                if cs.state and cs.state.waiting:
                                    reasons.append(
                                        f"{cs.name}: {cs.state.waiting.reason}"
                                    )
                        logger.debug(
                            "Pod '%s' phase=%s%s",
                            p.metadata.name if p.metadata else "?",
                            phase,
                            f" ({', '.join(reasons)})" if reasons else "",
                        )
            except ApiError as e:
                logger.debug("Waiting for deployment: %s", e)

            time.sleep(10)

        # Collect final pod state for the error message to aid debugging.
        pod_states = []
        try:
            pods = list(self.client.list(Pod, namespace=namespace, labels=label_selector))
            for p in pods:
                phase = p.status.phase if p.status else "Unknown"
                reasons = []
                if p.status and p.status.containerStatuses:
                    for cs in p.status.containerStatuses:
                        if cs.state and cs.state.waiting:
                            reasons.append(f"{cs.name}: {cs.state.waiting.reason}")
                name = p.metadata.name if p.metadata else "?"
                detail = f" ({', '.join(reasons)})" if reasons else ""
                pod_states.append(f"{name}={phase}{detail}")
        except ApiError:
            pass

        state_summary = "; ".join(pod_states) if pod_states else "no pods found"
        raise RuntimeError(
            f"Deployment with labels {label_selector} in namespace '{namespace}' "
            f"did not become ready within {timeout}s. Pod states: {state_summary}"
        )
