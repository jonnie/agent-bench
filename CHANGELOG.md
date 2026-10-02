# Changelog

User-visible changes are recorded here. Agent Bench is currently an experimental alpha; benchmark results describe the selected suite and deployment, not universal coding ability.

## Unreleased

### Added

- Weekly Dependabot version-update configuration for GitHub Actions and Python development dependencies; updates require review rather than automatic merging.
- An importable `main` branch ruleset requiring CI and pull requests with no mandatory external approval, plus owner-level GitHub setup instructions. Committed configuration does not itself enable branch protection or security-alert settings.

## 0.1.0 — 2026-10-02

Initial experimental alpha release.

### Fixed

- Linux agent containers now match a nonroot caller's UID/GID and use a private temporary writable home, preventing container-created bytecode and `0700` directories from breaking host cleanup. Root-host runs and graders retain the image's nonroot user.
- Docker regression coverage reproduces UID-mismatched cleanup on a Linux named volume, including bytecode, private directories, and read-only files. Integration summary failures now include diagnostics.

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

See [release notes](docs/releases/0.1.0.md) for installation instructions and experiment limitations.
