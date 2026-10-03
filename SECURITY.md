# Security policy

Agent Bench is experimental software that intentionally executes generated code in Docker. It is not a security product and must not be treated as a complete sandbox for hostile agents or candidate code.

## Reporting a vulnerability

Please do not disclose exploitable details, credentials, or private transcripts in a public issue.

If GitHub private vulnerability reporting is enabled, use [Report a vulnerability](https://github.com/jonnie/agent-bench/security/advisories/new). If that option is unavailable, open a minimal public issue asking the maintainer for a private reporting channel, without technical exploit details or sensitive data. No private reporting address or response-time guarantee is currently advertised.

Include the affected framework/harness versions, host/Docker versions and architecture, impact, and a minimal safe reproduction. Avoid real secrets and model-generated malicious payloads in attachments unless a private channel has been agreed.

Security fixes are best-effort for the latest release and current `main`. Older versions have no guaranteed backport support.

## Boundaries and known limitations

- Agent containers can access the configured model endpoint and other services allowed by their Docker network. There is no endpoint-only outbound firewall.
- The host Docker daemon, Dockerfiles, harness packages, task definitions, and grading fixtures are trusted. Image builds download upstream dependencies; pinned direct versions do not freeze every transitive dependency or base image.
- Agent containers run nonroot with dropped capabilities, resource limits, and cleanup. Kernel/runtime vulnerabilities can still defeat container isolation.
- Grading uses a separate, network-disabled container. Submitted Python executes inside the grader process; a malicious submission could inspect or tamper with its tests. Hidden tests are not a cryptographic anti-cheating mechanism.
- CPU/RAM limits and finite grading/build/doctor timeouts constrain local execution, not the remote model server's GPU, memory, or total token consumption. Agent wall time and captured stdout/stderr limits default to 600 seconds and 2,000,000 bytes per stream, but `run --timeout unlimited` and `run --max-log-bytes unlimited` (case-insensitive), or JSON `timeout: null` and `max_log_bytes: null`, independently disable those two limits. CPU/RAM/PID and submitted-source limits remain finite; `--max-tokens` remains a positive finite per-response setting.
- Disabling both agent limits can run indefinitely, fill host temporary disk, generate huge JSON/HTML reports, and exhaust host RAM while rendering. Docker memory caps do **not** bound host logs or report rendering. Prefer a finite timeout such as `--timeout 1800 --max-log-bytes unlimited`; fully unlimited runs are opt-in exploration, not a fair bounded-benchmark replacement. Ctrl+C uses normal interruption cleanup to remove the active container; hard kills cannot guarantee cleanup. Native tool/server/harness errors and context-window capacity still constrain execution, so unlimited does not guarantee completion.
- The endpoint should be on a trusted LAN or authenticated tunnel. Binding llama.cpp to `0.0.0.0` does not provide authentication or access control.
- Credentials are passed only through the selected environment variable, and literal key copies are redacted from reports. Encoding, partial disclosure, tool output, and other sensitive data can evade that redaction.
- JSON and HTML reports contain prompts, tool/model transcripts, diffs, endpoint information, and environment details. Treat private run outputs as sensitive and manually review publication copies.
- `agent-bench report` treats input as report data and escapes HTML content, but does not fully validate the JSON Schema at runtime. Validate untrusted inputs separately, enforce size limits, and do not execute report contents.

Use dedicated machines or disposable VMs for experiments with untrusted code, restrict LAN access, do not mount personal directories or the Docker socket into agent containers, and keep Docker/OS/upstream harnesses patched. Do not run privileged CI workflows against untrusted pull-request code.
