# Initial publication and release checklist

Repository: <https://github.com/jonnie/agent-bench>. Package metadata and license attribution use **Agent Bench contributors**, MIT. Confirm that attribution matches the rights you intend to grant before publishing. The framework remains an experimental alpha; do not market its five synthetic tasks as a comprehensive coding leaderboard.

## 1. Validate the checkout

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pyright src tests scripts

.venv/bin/agent-bench build
AGENT_BENCH_DOCKER_TESTS=1 .venv/bin/python -m unittest discover \
  -s tests -p test_docker_integration.py -v
```

Docker checks use a local mock API, not the maintainer's LAN model. Installed development tools enable schema tests; ordinary runs deliberately skip the three opt-in Docker tests. Review any other skips.

## 2. Validate the artifacts

Build in a clean checkout/output directory so old artifacts cannot be mistaken for this release. Do not blindly delete a directory containing results or artifacts you need to retain.

```sh
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/check_distribution.py dist/*.whl dist/*.tar.gz
```

Inspect the sdist and wheel: package code, license, Dockerfiles, schema, and intended docs/examples must be present; local results, environments, private keys, and `.env` files must not be present. The checker verifies payload parity and isolated installations. Build dependencies and upstream container packages require network access; benchmark inference is not part of packaging checks.

## 3. Review public content

- Review `.gitignore` and every staged file. Ignoring a file does not remove it from existing Git history.
- Confirm no API keys, private LAN addresses, personal paths, raw transcripts, or local `results/` are staged.
- Review `examples/qwen3.8-27b/results.json` **and** `results.html`. The example is a sanitized historical measurement with incomplete model/server provenance, not an exact deployment recipe. Its source-specific scrubber cannot safely publish arbitrary runs.
- Verify schema examples/tests and README links. Keep missing metrics as null/unknown and preserve failures.
- Confirm third-party attribution and licenses; the MIT license covers this project's code, not every upstream tool or redistributed image.

## 4. Initialize Git and push (maintainer action)

These commands are for the current directory **only if it is still not a Git repository**. Repository initialization, committing, and pushing are not performed by the release-preparation scripts.

```sh
git init -b main
git add .
git --no-pager diff --cached --stat
git --no-pager diff --cached
# After reviewing the staged content:
git commit -m "Prepare Agent Bench for initial open-source release"
git remote add origin https://github.com/jonnie/agent-bench.git
git push -u origin main
```

If Git/remotes already exist, inspect them instead of overwriting them. After pushing, check the actual GitHub Actions results; local validation is not proof that hosted CI succeeded.

## 5. Configure GitHub

- Enable private vulnerability reporting so the route in `SECURITY.md` is available.
- Enable branch protection/rulesets for `main`, requiring the unit, quality, and packaging checks before merging; avoid requiring the optional Docker job on every PR.
- Run the CI workflow manually with Docker integration enabled before the first release. The scheduled job checks upstream integration weekly.
- Add a concise repository description/topics and review the rendered README/examples. GitHub does not display the standalone HTML as a live page; download/open it locally or publish reviewed artifacts through your website.
- Do not grant this CI publishing credentials or expose model-server keys. Workflows use `contents: read`; no `pull_request_target` workflow is needed.

## 6. Cut the initial release

Confirm `pyproject.toml` and `src/agent_bench/__init__.py` agree on version, date the changelog, and rerun the checks. Then create the `v0.1.0` tag and GitHub release with verified wheel/sdist attachments and experiment limitations. These are separate explicit maintainer actions, not automated by CI.

PyPI publishing is optional and is not configured. Availability/ownership of the name `agent-bench` on PyPI has not been verified. Do not advertise `pip install agent-bench` until you control and publish the intended distribution. Repository installation works independently:

```sh
python3 -m pip install 'git+https://github.com/jonnie/agent-bench.git'
```

That command becomes usable after the source has been pushed. For comparisons, record the source commit, task/test hashes, image identities, and complete remote deployment configuration; preserve original measured JSON privately and publish reviewed derivatives.
