"""A module that always fails to import — stands in for an installed connector
distribution whose package is broken (an import error, a missing vendor dependency).

``EntryPoint.load()`` for an entry point pointing here raises exactly the way it would
for a real broken connector package, letting the collision/load-failure tests in
``test_discover_connectors.py`` exercise ``discover_connectors``'s handling of
``ep.load()`` raising without needing a genuinely broken package installed.
"""

raise ModuleNotFoundError("simulated broken connector: no module named 'not_a_real_vendor_sdk'")
