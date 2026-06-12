# Copyright 2026 vlad.apostol@canonical.com
# See LICENSE file for licensing details.
#
# The integration tests use the Jubilant library. See https://documentation.ubuntu.com/jubilant/
# To learn more about testing, see https://documentation.ubuntu.com/ops/latest/explanation/testing/

import logging
import os
import pathlib
import platform
import sys
import time

import jubilant
import pytest

logger = logging.getLogger(__name__)


def _host_ubuntu_series() -> str:
    """Return the Ubuntu series of the current host (e.g. '22.04' or '24.04')."""
    try:
        info = platform.freedesktop_os_release()
        return info.get("VERSION_ID", "")
    except (AttributeError, OSError):
        return ""


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    """Create a temporary Juju model for running tests."""
    with jubilant.temp_model() as juju:
        yield juju

        if request.session.testsfailed:
            logger.info("Collecting Juju logs...")
            time.sleep(0.5)  # Wait for Juju to process logs.
            log = juju.debug_log(limit=1000)
            print(log, end="", file=sys.stderr)


@pytest.fixture(scope="session")
def charm():
    """Return the path of the charm under test.

    When CHARM_PATH is set, that path is used directly. Otherwise the project
    directory is searched for built .charm files. If multiple builds are present
    (one per platform), the one matching the host Ubuntu version is preferred.
    """
    if "CHARM_PATH" in os.environ:
        charm_path = pathlib.Path(os.environ["CHARM_PATH"])
        if not charm_path.exists():
            raise FileNotFoundError(f"Charm does not exist: {charm_path}")
        return charm_path

    charm_paths = list(pathlib.Path(".").glob("*.charm"))
    if not charm_paths:
        raise FileNotFoundError("No .charm file in current directory")
    if len(charm_paths) == 1:
        return charm_paths[0]

    # Multiple builds found — pick the one matching the host Ubuntu version.
    series = _host_ubuntu_series()
    if series:
        matching = [p for p in charm_paths if series in p.name]
        if len(matching) == 1:
            logger.info(
                "Multiple .charm files found; selected %s (host Ubuntu %s)", matching[0], series
            )
            return matching[0]

    path_list = ", ".join(str(p) for p in charm_paths)
    raise ValueError(
        f"Multiple .charm files found and could not auto-select for host Ubuntu '{series}': "
        f"{path_list}. Set the CHARM_PATH environment variable to specify which to use."
    )
