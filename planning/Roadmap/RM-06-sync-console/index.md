# Catalog sync console: endpoint management and sync selection

## Concepts

* [Decision: configuration lives in the state store, and selection is an ordered rule set](decision-console-config-and-selection.md) - Moves endpoints, sync pairs and sync scope out of environment variables into the state store so a console can edit them, keeps credentials outside it as named references, and replaces the MVP's flat glob selector with an ordered include/exclude rule set evaluated by a single shared evaluator.
* [Console and selection implementation plan — Work Packages WP10-WP14](implementation-plan.md) - Executable build plan for the catalog sync console: the database-backed configuration store, the selection rule engine, the REST API and generated client, the SPA built on the @elabs-ai component packages, and the packaging that ships all of it inside the engine container.
* [Catalog sync console: endpoint management and sync selection](item.md) - Ship the operator console for the MVP: configure and manage connector endpoints and sync pairs from a browser, decide exactly which source objects sync through an ordered include/exclude rule set, preview the planned writes before applying them, and watch runs. Configuration moves from environment variables into the state database; credentials stay external and are only referenced.
