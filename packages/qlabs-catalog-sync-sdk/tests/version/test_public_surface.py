"""The package root's public surface: everything in ``__all__`` really imports from
there, and importing the root does not depend on parts of the SDK that have not landed
in this worktree yet (T1.3's ``manifest.py`` is still a docstring-only stub).
"""

from __future__ import annotations

import subprocess
import sys

import qlabs_catalog_sync_sdk as sdk


def test_every_name_in_all_is_importable_from_the_package_root() -> None:
    assert sdk.__all__, "the public surface must not be empty"

    missing = [name for name in sdk.__all__ if not hasattr(sdk, name)]
    assert missing == []


def test_all_has_no_duplicate_names() -> None:
    assert len(sdk.__all__) == len(set(sdk.__all__))


def test_contract_version_and_entry_point_group_are_exported() -> None:
    assert sdk.SDK_CONTRACT_VERSION == 1
    assert isinstance(sdk.CONTRACT_VERSION, str)
    assert sdk.CONNECTOR_ENTRY_POINT_GROUP == "qlabs_catalog_sync.connectors"


def test_sdk_contract_version_is_re_exported_not_redefined() -> None:
    """``contract.py`` is T1.9's one source of truth for ``SDK_CONTRACT_VERSION`` — it
    is what stamps ``Connector.sdk_contract_version`` — so every path to this constant
    must resolve to the exact same object, never a second copy."""
    from qlabs_catalog_sync_sdk.contract import SDK_CONTRACT_VERSION as from_contract
    from qlabs_catalog_sync_sdk.version import SDK_CONTRACT_VERSION as from_version

    assert sdk.SDK_CONTRACT_VERSION is from_contract
    assert sdk.SDK_CONTRACT_VERSION is from_version


def test_connector_is_stamped_with_the_exported_contract_version() -> None:
    assert sdk.Connector.sdk_contract_version == sdk.SDK_CONTRACT_VERSION


def test_the_public_clock_and_system_clock_are_configs_not_auths() -> None:
    """``auth.py`` and ``config.py`` each define their own ``Clock``/``SystemClock``.
    The root package resolves the name collision in favor of ``config``'s versions —
    see the ``__init__.py`` module docstring for why."""
    from qlabs_catalog_sync_sdk import config

    assert sdk.Clock is config.Clock
    assert sdk.SystemClock is config.SystemClock


def test_root_import_does_not_pull_in_the_unfinished_manifest_module() -> None:
    """``manifest.py`` (T1.3) has not landed in this worktree — it defines no
    ``CapabilityManifest`` yet. Importing the package root must not depend on it or
    even touch it as a side effect; this proves that in a fresh interpreter, not just
    "it happens not to be imported yet" in a process where some other test already
    imported it directly.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import qlabs_catalog_sync_sdk\n"
            "print('qlabs_catalog_sync_sdk.manifest' in sys.modules)\n",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", result.stderr


def test_the_manifest_module_really_is_still_a_stub() -> None:
    """Guards the premise of the previous test: if T1.3 lands and this still passes,
    the premise is stale and the orchestrator's follow-up (wiring ``CapabilityManifest``
    into ``__init__.py``, per this package's module docstring) is due.
    """
    import qlabs_catalog_sync_sdk.manifest as manifest_stub

    assert not hasattr(manifest_stub, "CapabilityManifest")
