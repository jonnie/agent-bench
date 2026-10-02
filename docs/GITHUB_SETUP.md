# GitHub repository setup

These are **owner/admin repository settings**, separate from files committed to Git. An SSH key that can push commits does not grant API access to change them. Never paste a personal access token into an issue, report, workflow, or this repository.

## Protect `main`

In [Settings → Rules → Rulesets](https://github.com/jonnie/agent-bench/settings/rules), create an **active branch ruleset** targeting `refs/heads/main` with:

- Restrict deletions and block force pushes.
- Require a pull request before merging, with **zero required approvals** so a solo maintainer can merge after checks pass.
- Require conversations to be resolved.
- Require status checks, and require the branch to be up to date before merging:
  - `Unit (ubuntu-latest, Python 3.11)`
  - `Unit (ubuntu-latest, Python 3.13)`
  - `Unit (macos-latest, Python 3.11)`
  - `Lint and typecheck`
  - `Build and isolated distribution smoke checks`
- Do not require `Docker integration (local mock, no inference)`: it runs manually/weekly, not on every PR.
- Leave bypass actors empty unless you deliberately choose a maintenance policy. Do not require an external reviewer for your own changes.

Only apply protection **after** the initial release preparation has been pushed. Future changes should use branches and PRs. An active ruleset applies to direct pushes too; an SSH key is not an automatic bypass.

## Security and dependencies

In [Settings → Code security](https://github.com/jonnie/agent-bench/settings/security_analysis), verify:

- Dependency graph is enabled (normally enabled automatically for public repositories).
- Dependabot alerts are enabled.
- Dependabot security updates are enabled. These create PRs; they do not authorize automatic merging.
- Private vulnerability reporting is enabled, matching `SECURITY.md`.

The repository can schedule Dependabot version updates for GitHub Actions and Python development dependencies using `.github/dependabot.yml`. Review each update through CI; direct harness pins in Dockerfiles require separate manual review and Docker integration because harness changes affect measurements. Do not mix automatic dependency updating with automatic merging or release publication.

With an authenticated GitHub CLI and appropriate repository administration permissions, alert/security-update settings can also be enabled explicitly:

```sh
gh auth login --hostname github.com --web
gh api --method PUT repos/jonnie/agent-bench/vulnerability-alerts
gh api --method PUT repos/jonnie/agent-bench/automated-security-fixes
```

Account/organization policies may restrict these options. Inspect the resulting settings rather than assuming a configuration file enabled every server-side feature.

## Repository presentation

In [repository settings](https://github.com/jonnie/agent-bench/settings), set:

- **Description:** `Docker-isolated benchmarks comparing one LAN-hosted LLM across pi, oh-my-pi, and OpenCode, with JSON and HTML reports.`
- **Topics:** `llm`, `benchmarking`, `coding-agents`, `docker`, `llama-cpp`, `python`, `opencode`, `pi`, `oh-my-pi`.
- **Website:** your intended website or benchmark landing page. The owner's public GitHub profile lists `https://jonnie.github.io`; confirm that this is the intended destination before applying it.

The description/topics are also editable from the repository's **About** panel. Review the rendered README, license, contributor/security links, release notes, and example documentation. GitHub does not render a checked-in HTML report as a live website. Publish only reviewed/sanitized reports to your website; do not enable Pages against raw private output.

For an authenticated CLI, description and topics can be applied without choosing a website:

```sh
gh repo edit jonnie/agent-bench \
  --description 'Docker-isolated benchmarks comparing one LAN-hosted LLM across pi, oh-my-pi, and OpenCode, with JSON and HTML reports.' \
  --add-topic llm,benchmarking,coding-agents,docker,llama-cpp,python,opencode,pi,oh-my-pi
```

## Release policy

The `Release` workflow publishes validated distribution artifacts for maintainer-pushed version tags; it does not publish to PyPI, deploy your website, or modify branch/security settings. Only its publication job receives `contents: write`. CI and dependency-update PRs receive no model credentials or personal publishing secrets.

Keep the experimental scope and security limitations visible in public announcements. Record the framework release/commit and full experiment provenance alongside blog comparisons, preserve failures, and distinguish task/grading changes from report schema changes.
