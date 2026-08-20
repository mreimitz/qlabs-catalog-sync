"""The package root's public surface: everything in ``__all__`` really imports from
there, and importing the root does not depend on parts of the SDK that have not landed
in this worktree yet (T1.3's ``manifest.py`` is still a docstring-only stub).
"""

from __future__ import annotations

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


def test_the_capability_manifest_is_exported_from_the_package_root() -> None:
    """``CapabilityManifest`` is what ``Connector.capabilities()`` returns and what the
    engine plans every write from, so it must be reachable from the package root."""
    import qlabs_catalog_sync_sdk as sdk
    from qlabs_catalog_sync_sdk.contract import CapabilityManifestBase
    from qlabs_catalog_sync_sdk.manifest import CapabilityManifest

    assert sdk.CapabilityManifest is CapabilityManifest
    assert issubclass(CapabilityManifest, CapabilityManifestBase)
    for name in ("ConcurrencyMode", "EntityCapability", "FieldCapability", "FieldCapabilityMode"):
        assert name in sdk.__all__
        assert getattr(sdk, name) is getattr(
            __import__("qlabs_catalog_sync_sdk.manifest", fromlist=[name]), name
        )
