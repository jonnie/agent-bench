"""Stdlib-only, single-model adapters for isolated benchmark containers.

``prepare_harness`` returns an argv prefix (append the prompt as ONE argument,
without a shell, or supply it on stdin), container environment, individual config
file mounts, and a secret-free report configuration. The runner MUST mount every
``(source, destination)`` pair read-only; never mount host home or a Docker socket.
The only credential-bearing result is ``environment['BENCH_API_KEY']``. Do not
include that environment in reports. Prices are USD per million tokens.

Native tools retain harness-specific semantics. The common profile exposes only
read/write/edit/bash, not identical implementations or system prompts. Disabling
delegation is a benchmark policy, not a security sandbox: bash/eval still execute
arbitrary code. Container isolation and resource limits belong to the runner.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HARNESS_VERSIONS = {
    "pi": "0.99.2",
    "oh-my-pi": "18.4.8",
    "opencode": "1.18.34",
}

_COMMON_TOOLS = "read,write,edit,bash"
# OMP 18.4.8 gates ast_grep off by default and rejects it in an explicit
# --tools list. Keep the default-available coding tools without task/wait.
_OMP_TOOLS = _COMMON_TOOLS + ",grep,glob,lsp,eval,debug,ast_edit,todo"
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_THINKING = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}


def _harness(harness: str) -> str:
    # Accept the executable name as an alias; image/config identities are canonical.
    if harness == "omp":
        harness = "oh-my-pi"
    if harness not in HARNESS_VERSIONS:
        raise ValueError(f"Unknown harness: {harness!r}")
    return harness


def image_tag(harness: str, version: str) -> str:
    """Return a deterministic Docker tag, rejecting rather than lossy-sanitizing.

    Only Docker-safe tags of at most 128 characters are accepted. In particular,
    whitespace, shell syntax, slashes, colons and SemVer '+' metadata are rejected
    so distinct versions cannot accidentally collide after sanitization.
    """
    harness = _harness(harness)
    if not isinstance(version, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", version
    ):
        raise ValueError("version must be a Docker-safe tag (1–128 characters)")
    return f"agent-bench-{harness}:{version}"


def _price(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _configuration(config: dict) -> dict:
    """Whitelist reportable fields; never copy caller credentials or extra settings."""
    model = config.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*", model):
        raise ValueError(
            "model must be a nonempty literal model ID, without patterns or interpolation"
        )
    base_url = config.get("base_url")
    if not isinstance(base_url, str) or any(char.isspace() for char in base_url):
        raise ValueError("base_url must be an HTTP(S) URL without credentials or query parameters")
    url = urlsplit(base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(char in base_url for char in "${}")
    ):
        raise ValueError("base_url must be an HTTP(S) URL without credentials or query parameters")
    # Accessing port also validates malformed/non-numeric port declarations.
    _ = url.port
    result: dict[str, Any] = {"model": model, "base_url": base_url}
    for field in ("context_window", "max_tokens"):
        value = config.get(field)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
        result[field] = value
    if result["max_tokens"] > result["context_window"]:
        raise ValueError("max_tokens cannot exceed context_window")
    reasoning = config.get("reasoning", False)
    thinking = config.get("thinking", "off")
    if type(reasoning) is not bool:
        raise ValueError("reasoning must be a boolean")
    if not isinstance(thinking, str) or thinking not in _THINKING:
        raise ValueError(f"thinking must be one of {', '.join(sorted(_THINKING))}")
    if thinking != "off" and not reasoning:
        raise ValueError("thinking requires a reasoning-capable model")
    api_key_env = config.get("api_key_env", "AGENT_BENCH_API_KEY")
    if not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError("api_key_env must be an environment variable name")
    api_profile = config.get("api_profile", "llama-cpp")
    if api_profile not in ("llama-cpp", "openai-compatible"):
        raise ValueError("api_profile must be 'llama-cpp' or 'openai-compatible'")
    profile = config.get("tool_profile", "native")
    if profile not in ("native", "common"):
        raise ValueError("tool_profile must be 'native' or 'common'")
    result.update(
        reasoning=reasoning,
        thinking=thinking,
        api_key_env=api_key_env,
        api_profile=api_profile,
        tool_profile=profile,
    )
    for field in ("input_price", "output_price"):
        value = config.get(field)
        if value is not None and _price(value) is None:
            raise ValueError(f"{field} must be a finite nonnegative number or None")
        result[field] = value
    return result


def prepare_harness(harness: str, config: dict, directory: Path) -> dict:
    """Write secret-free configs below directory and return the four-key runner API.

    ``api_key_env`` names the HOST variable to read, not the container variable.
    Missing/empty host credentials become ``local-no-key``. Config files use the
    harness's own environment-reference syntax and never contain the actual key.
    A fresh container home is required; no prior auth, sessions or plugins are used.
    """
    harness = _harness(harness)
    configuration = _configuration(config)
    model = configuration["model"]
    # CLI thinking levels alone do not disable server-side template reasoning.
    llama_options = {
        "chat_template_kwargs": {"enable_thinking": configuration["thinking"] != "off"}
    }
    selector = f"benchmark/{model}"
    environment = {
        "BENCH_API_KEY": os.environ.get(configuration["api_key_env"]) or "local-no-key",
        "HOME": "/home/bench",
    }
    files: dict[str, dict] = {}
    if harness in ("pi", "oh-my-pi"):
        descriptor = {
            "id": model,
            "name": model,
            "reasoning": configuration["reasoning"],
            "input": ["text"],
            "contextWindow": configuration["context_window"],
            "maxTokens": configuration["max_tokens"],
        }
        if configuration["api_profile"] == "llama-cpp":
            if harness == "pi":
                descriptor["samplingParams"] = llama_options
            else:
                descriptor["compat"] = {"extraBody": llama_options}
        # Harness UIs require numeric tariffs; their built-in cost is deliberately
        # NOT used by parse_usage. Unknown report prices remain None, not free.
        descriptor["cost"] = {
            "input": configuration["input_price"] or 0,
            "output": configuration["output_price"] or 0,
            "cacheRead": configuration["input_price"] or 0,
            "cacheWrite": configuration["input_price"] or 0,
        }
        provider = {
            "baseUrl": configuration["base_url"],
            "api": "openai-completions",
            "apiKey": "$BENCH_API_KEY" if harness == "pi" else "BENCH_API_KEY",
            "models": [descriptor],
        }
        command = [
            "pi" if harness == "pi" else "omp",
            "--mode",
            "json",
            "-p",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--thinking",
            configuration["thinking"],
        ]
        if harness == "pi":
            files[".pi/agent/models.json"] = {"providers": {"benchmark": provider}}
            files[".pi/agent/settings.json"] = {
                "defaultProvider": "benchmark",
                "defaultModel": model,
                "defaultThinkingLevel": configuration["thinking"],
                "enabledModels": [selector],
                "packages": [],
            }
            command += [
                "--provider",
                "benchmark",
                "--model",
                model,
                "--models",
                selector,
                "--offline",
                "--no-prompt-templates",
            ]
            if configuration["tool_profile"] == "common":
                command += ["--tools", _COMMON_TOOLS]
        else:
            files[".omp/agent/models.yml"] = {"providers": {"benchmark": provider}}
            files[".omp/agent/config.yml"] = {
                "modelRoles": {
                    role: selector
                    for role in (
                        "default",
                        "smol",
                        "slow",
                        "plan",
                        "vision",
                        "designer",
                        "commit",
                        "tiny",
                        "task",
                        "advisor",
                    )
                },
                "enabledModels": [selector],
                "defaultThinkingLevel": configuration["thinking"],
                "retry": {
                    "modelFallback": False,
                    "fallbackChains": {},
                    "usageAwareFallback": False,
                },
                "advisor": {"enabled": False},
                "memory": {"backend": "off"},
                "compaction": {"asyncEnabled": False, "idleEnabled": False},
                "magicKeywords": {"enabled": False},
                # xdev can expose excluded task/wait through read/write devices.
                "tools": {"xdev": False},
            }
            command += [
                "--config",
                "/home/bench/.omp/agent/config.yml",
                "--model",
                selector,
                "--models",
                selector,
                "--smol",
                selector,
                "--slow",
                selector,
                "--plan",
                selector,
                "--no-title",
                "--no-prewalk",
                "--auto-approve",
                "--tools",
                _COMMON_TOOLS if configuration["tool_profile"] == "common" else _OMP_TOOLS,
            ]
    else:
        descriptor = {
            "name": model,
            "reasoning": configuration["reasoning"],
            "tool_call": True,
            "limit": {
                "context": configuration["context_window"],
                "output": configuration["max_tokens"],
            },
        }
        # The compatible SDK forwards arbitrary options into every request,
        # including title calls. Limit metadata alone can leave max_tokens unset.
        descriptor["options"] = {"max_tokens": configuration["max_tokens"]}
        if configuration["api_profile"] == "llama-cpp":
            descriptor["options"].update(llama_options)
        elif configuration["reasoning"]:
            # Generic endpoints must implement reasoning_effort for these
            # controls; 'off' maps to its explicit 'none' effort.
            descriptor["options"]["reasoningEffort"] = (
                "none" if configuration["thinking"] == "off" else configuration["thinking"]
            )
        tools = {"task": False}
        permission = {"*": "allow", "task": "deny", "question": "deny"}
        if configuration["tool_profile"] == "common":
            tools = {"*": False, **dict.fromkeys(_COMMON_TOOLS.split(","), True), "task": False}
            # Tools are converted to permissions by OpenCode; explicit permission
            # wildcards must not re-enable tools excluded by the common profile.
            permission = {
                "*": "deny",
                **dict.fromkeys(_COMMON_TOOLS.split(","), "allow"),
                "external_directory": "allow",
                "task": "deny",
                "question": "deny",
            }
        files[".config/opencode/opencode.json"] = {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": ["benchmark"],
            "provider": {
                "benchmark": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Benchmark",
                    "options": {
                        "baseURL": configuration["base_url"],
                        "apiKey": "{env:BENCH_API_KEY}",
                    },
                    "models": {model: descriptor},
                }
            },
            "model": selector,
            "small_model": selector,
            "default_agent": "build",
            "agent": {"build": {"model": selector, "tools": tools}},
            "tools": tools,
            "permission": permission,
            "share": "disabled",
            "autoupdate": False,
        }
        environment.update(
            {
                "OPENCODE_CONFIG": "/home/bench/.config/opencode/opencode.json",
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_MODELS_FETCH": "true",
            }
        )
        command = ["opencode", "run", "--format", "json", "--model", selector, "--agent", "build"]
    command.append("--")
    mounts: list[tuple[Path, str]] = []
    for relative, contents in files.items():
        # JSON is valid YAML, avoiding any YAML dependency on the host.
        source = (Path(directory) / harness / relative).resolve()
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(contents, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        source.chmod(0o644)  # Readable by container UID 1000 even when host UID differs.
        mounts.append((source, f"/home/bench/{relative}"))
    return {
        "command": command,
        "environment": environment,
        "mounts": mounts,
        "configuration": configuration,
    }


def _events(stdout: str):
    # Only LF frames JSONL. splitlines() corrupts valid JSON strings containing
    # Unicode line/paragraph separators, which can appear in model output.
    for line in stdout.split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _identity(record: dict, scope: object) -> tuple | None:
    identifier = record.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    return (str(scope), str(record.get("messageID", "")), identifier)


def parse_usage(harness: str, stdout: str, input_price=None, output_price=None) -> dict:
    """Sum final assistant/step usage, never streaming snapshots or nested copies.

    Counts are None without observations, or if any observed response omits that
    field. Tool counts cover this JSONL stream, not hidden background requests.
    Replayed final events with stable IDs/timestamps and tool IDs are deduplicated;
    genuinely distinct ID-less assistant events are not collapsed by content.
    Estimates require all four token counts and both USD/million tariffs. Cached
    input is charged at the input rate, NOT at unknown provider-specific tariffs.
    """
    harness = _harness(harness)
    result = dict.fromkeys((*_TOKEN_FIELDS, "tool_calls", "estimated_cost_usd", "cost_note"))
    totals = dict.fromkeys(_TOKEN_FIELDS, 0)
    unknown: set[str] = set()
    responses = 0
    tools = 0
    stream_seen = False
    tools_unknown = False
    seen_responses: set[tuple] = set()
    seen_tools: set[tuple] = set()
    for event in _events(stdout):
        kind = event.get("type")
        scope = event.get("sessionID", "")
        usage = None
        if harness == "opencode":
            if kind == "step_finish":
                stream_seen = True
            part = _mapping(event.get("part"))
            if kind == "step_finish":
                identity = _identity(part, scope)
                if identity is not None:
                    if identity in seen_responses:
                        continue
                    seen_responses.add(identity)
                tokens = _mapping(part.get("tokens"))
                cache = _mapping(tokens.get("cache"))
                usage = {
                    "input": tokens.get("input"),
                    "output": tokens.get("output"),
                    "cacheRead": cache.get("read"),
                    "cacheWrite": cache.get("write"),
                }
            elif kind == "tool_use":
                stream_seen = True
                call_id = part.get("callID")
                if not isinstance(call_id, str) or not call_id:
                    tools_unknown = True
                elif (str(scope), call_id) not in seen_tools:
                    seen_tools.add((str(scope), call_id))
                    tools += 1
        else:
            if kind in ("agent_end", "agent_settled"):
                stream_seen = True
            if kind == "message_end":
                message = _mapping(event.get("message"))
                if message.get("role") != "assistant":
                    continue
                stream_seen = True
                identity = _identity(message, scope)
                if identity is None and _count(message.get("timestamp")) is not None:
                    identity = (
                        str(scope),
                        str(message.get("provider", "")),
                        str(message.get("model", "")),
                        message["timestamp"],
                    )
                if identity is not None:
                    if identity in seen_responses:
                        continue
                    seen_responses.add(identity)
                usage = _mapping(message.get("usage"))
            elif kind == "tool_execution_start":
                stream_seen = True
                call_id = event.get("toolCallId")
                if isinstance(call_id, str) and call_id:
                    if (str(scope), call_id) in seen_tools:
                        continue
                    seen_tools.add((str(scope), call_id))
                tools += 1
        if usage is not None:
            responses += 1
            for field, native in zip(_TOKEN_FIELDS, ("input", "output", "cacheRead", "cacheWrite")):
                count = _count(usage.get(native))
                if count is None:
                    unknown.add(field)
                else:
                    totals[field] += count
    if responses:
        result.update(
            {field: None if field in unknown else totals[field] for field in _TOKEN_FIELDS}
        )
    if stream_seen and not tools_unknown:
        result["tool_calls"] = tools
    input_rate, output_rate = _price(input_price), _price(output_price)
    if responses and not unknown and input_rate is not None and output_rate is not None:
        try:
            cost = (
                totals["input_tokens"] + totals["cache_read_tokens"] + totals["cache_write_tokens"]
            ) / 1_000_000 * input_rate + totals["output_tokens"] / 1_000_000 * output_rate
        except OverflowError:
            return result
        if math.isfinite(cost):
            result["estimated_cost_usd"] = cost
            result["cost_note"] = (
                "USD estimate using prices per million tokens; cache reads/writes "
                "charged at the input rate, not provider-specific cache tariffs. "
                "Only emitted primary-stream usage is included."
            )
    return result


def parse_termination(harness: str, stdout: str) -> dict:
    """Describe the last completed response, not aggregate usage or execution status.

    A later response replaces an earlier length stop, even if its reason/usage is
    unknown. Only emitted counts are reported; reasoning is never inferred from
    content or subtracted from output (provider accounting can overlap).
    """
    harness = _harness(harness)
    result: dict[str, Any] = dict.fromkeys(
        ("reason", "output_tokens", "reasoning_tokens", "warning")
    )
    for event in _events(stdout):
        if harness == "opencode":
            if event.get("type") != "step_finish" or not isinstance(event.get("part"), dict):
                continue
            response = event["part"]
            reason = response.get("reason")
            usage = _mapping(response.get("tokens"))
        else:
            if event.get("type") != "message_end":
                continue
            response = _mapping(event.get("message"))
            if response.get("role") != "assistant":
                continue
            reason = response.get("stopReason")
            usage = _mapping(response.get("usage"))
        result.update(
            reason=reason if isinstance(reason, str) and reason else None,
            output_tokens=_count(usage.get("output")),
            reasoning_tokens=_count(usage.get("reasoning")),
        )
    if result["reason"] == "length":
        result["warning"] = (
            "Final assistant response reached a token limit (length); "
            "the response may be incomplete. The limit may be the response-token budget "
            "or the server's context capacity. This is a diagnostic warning, not an "
            "execution error; correctness grading and status are unchanged."
        )
    return result


def _error_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    error = _mapping(value)
    data = _mapping(error.get("data"))
    for candidate in (
        data.get("message"),
        error.get("message"),
        error.get("errorMessage"),
        error.get("name"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def harness_error(harness: str, stdout: str) -> str | None:
    """Return the first observed harness/model error, even after a zero exit code.

    Ordinary tool failures and prose mentioning an error are not harness failures.
    This is an observed-error policy: later recovery does not erase the observation.
    """
    harness = _harness(harness)
    for event in _events(stdout):
        kind = event.get("type")
        if kind in ("error", "session.error", "extension_error"):
            properties = _mapping(event.get("properties"))
            return (
                _error_text(event.get("error"))
                or _error_text(properties.get("error"))
                or _error_text(event.get("message"))
                or _error_text(event.get("errorMessage"))
                or "Harness emitted an error event"
            )
        if harness != "opencode":
            if kind == "message_end":
                message = _mapping(event.get("message"))
                if message.get("role") == "assistant" and message.get("stopReason") in (
                    "error",
                    "aborted",
                ):
                    return (
                        _error_text(message.get("errorMessage"))
                        or "Assistant response failed or was aborted"
                    )
            if kind == "message_update":
                update = _mapping(event.get("assistantMessageEvent"))
                if update.get("type") == "error":
                    return _error_text(update.get("error")) or "Assistant stream failed"
            if kind == "auto_retry_end" and event.get("success") is False:
                return _error_text(event.get("finalError")) or "Automatic retries exhausted"
            if kind in ("auto_compaction_end", "compaction_end") and event.get("errorMessage"):
                return _error_text(event["errorMessage"])
    return None
