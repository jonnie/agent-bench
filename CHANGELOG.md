# Changelog

User-visible changes are recorded here. Agent Bench is currently an experimental alpha; benchmark results describe the selected suite and deployment, not universal coding ability.

## Unreleased

### Added

- GitHub Actions checks for unit tests on Linux/macOS, linting and type checking, package builds, and isolated wheel/source-distribution installations.
- Scheduled and manually enabled Docker integration checks against a local mock API, with no model credentials or inference.
- Development extras, contributor/security guidance, and an initial-release checklist.
- Bundled draft-2020-12 JSON Schema and documentation for schema-version-1 reports, including task sets and partial runs.
- Reviewed, sanitized JSON/HTML example from a measured 45-attempt run; published results retain failures and warning observations.

### Initial framework capabilities

- Host-orchestrated, Docker-isolated comparisons of one OpenAI-compatible/LAN-hosted model across pi, oh-my-pi, and OpenCode.
- Five synthetic Python benchmarks covering bug fixing, small features, system refactoring, security hardening, and performance.
- Individual task modules and explicit `default`, `core`, and `robustness` task sets, with deduplicated union selection.
- Independent hidden grading, repeat scheduling, resource/time/output limits, usage and patch diagnostics, and JSON/offline HTML reports.
- Explicit llama.cpp chat-template thinking controls and final token-limit warnings.

The package version is `0.1.0`; no GitHub release or release tag has been published by this preparation step. Move these entries into a dated `0.1.0` section when cutting the initial release.
