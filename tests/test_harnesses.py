"""Offline adapter contract tests; no harness install, Docker, or API key required."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from agent_bench.harnesses import (
    HARNESS_VERSIONS,
    harness_error,
    image_tag,
    parse_termination,
    parse_usage,
    prepare_harness,
)

CONFIG = {
    "model": "served-model",
    "base_url": "http://host.docker.internal:8000/v1",
    "context_window": 32768,
    "max_tokens": 4096,
    "reasoning": False,
}


def jsonl(*events):
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"


def assistant(usage=None, **extra):
    message = {"role": "assistant", "usage": usage or {}, **extra}
    return {"type": "message_end", "message": message}


def step(tokens=None, **extra):
    return {"type": "step_finish", "part": {"tokens": tokens or {}, **extra}}


PI_USAGE = {"input": 100, "output": 20, "cacheRead": 10, "cacheWrite": 5}
OC_TOKENS = {"input": 100, "output": 20, "cache": {"read": 10, "write": 5}}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def prepare(self, harness, **updates):
        return prepare_harness(harness, {**CONFIG, **updates}, self.directory)

    def files(self, result):
        return {
            destination: json.loads(source.read_text(encoding="utf-8"))
            for source, destination in result["mounts"]
        }

    def test_versions_and_tags(self):
        self.assertEqual(
            HARNESS_VERSIONS,
            {
                "pi": "0.99.2",
                "oh-my-pi": "18.4.8",
                "opencode": "1.18.34",
            },
        )
        for harness, version in HARNESS_VERSIONS.items():
            self.assertEqual(image_tag(harness, version), f"agent-bench-{harness}:{version}")
        self.assertEqual(image_tag("pi", "v0.99.2-rc.1"), "agent-bench-pi:v0.99.2-rc.1")
        self.assertEqual(image_tag("omp", "18.4.8"), image_tag("oh-my-pi", "18.4.8"))

    def test_bad_versions_and_unknown_harnesses(self):
        invalid_versions: tuple[Any, ...] = (
            "",
            "../latest",
            "foo:bar",
            "1.0+build",
            "$(id)",
            "1;id",
            "x\n",
            ".bad",
            "x" * 129,
            None,
        )
        for version in invalid_versions:
            with self.subTest(version=version), self.assertRaises(ValueError):
                image_tag("pi", cast(str, version))
        invalid_calls: tuple[tuple[Callable[..., Any], tuple[Any, ...]], ...] = (
            (image_tag, ("unknown", "1")),
            (prepare_harness, ("unknown", CONFIG, self.directory)),
            (parse_usage, ("unknown", "")),
            (harness_error, ("unknown", "")),
        )
        for function, args in invalid_calls:
            with self.subTest(function=function.__name__), self.assertRaises(ValueError):
                cast(Callable[..., Any], function)(*args)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_exact_api_secret_is_only_in_environment_and_mounts_are_individual(self):
        secret = "sk-host-only-not-persisted"
        config = {
            **CONFIG,
            "api_key": secret,
            "extra": {"secret": secret},
            "api_key_env": "CUSTOM_KEY",
            "input_price": 1.25,
            "output_price": 3,
        }
        original = copy.deepcopy(config)
        with patch.dict(os.environ, {"CUSTOM_KEY": secret}, clear=True):
            for harness in HARNESS_VERSIONS:
                with self.subTest(harness=harness):
                    result = prepare_harness(harness, config, self.directory)
                    self.assertEqual(
                        set(result), {"command", "environment", "mounts", "configuration"}
                    )
                    self.assertIsInstance(result["command"], list)
                    self.assertTrue(all(isinstance(arg, str) for arg in result["command"]))
                    self.assertEqual(result["command"][-1], "--")
                    self.assertNotIn(result["command"][0], ("sh", "bash"))
                    self.assertNotIn("--api-key", result["command"])
                    self.assertEqual(result["environment"]["BENCH_API_KEY"], secret)
                    self.assertNotIn(secret, json.dumps(result["command"]))
                    self.assertNotIn(secret, json.dumps(result["configuration"]))
                    self.assertNotIn("extra", result["configuration"])
                    for source, destination in result["mounts"]:
                        self.assertIsInstance(source, Path)
                        self.assertTrue(source.is_file())
                        self.assertTrue(source.is_relative_to(self.directory))
                        self.assertTrue(destination.startswith("/home/bench/"))
                        self.assertNotEqual(destination, "/home/bench")
                        self.assertNotIn("docker.sock", destination)
                        self.assertEqual(source.stat().st_mode & 0o777, 0o644)
                        self.assertNotIn(secret, source.read_text())
        self.assertEqual(config, original)

    def test_missing_or_empty_key_is_dummy_and_custom_env_is_respected(self):
        for values in ({}, {"AGENT_BENCH_API_KEY": ""}, {"OTHER_KEY": "wrong-key"}):
            with patch.dict(os.environ, values, clear=True):
                for harness in HARNESS_VERSIONS:
                    self.assertEqual(
                        self.prepare(harness)["environment"]["BENCH_API_KEY"], "local-no-key"
                    )
        with patch.dict(os.environ, {"BENCH_API_KEY": "runner-key"}, clear=True):
            self.assertEqual(
                self.prepare("pi", api_key_env="BENCH_API_KEY")["environment"]["BENCH_API_KEY"],
                "runner-key",
            )

    def test_pi_provider_and_native_defaults(self):
        result = self.prepare("pi")
        files = self.files(result)
        provider = files["/home/bench/.pi/agent/models.json"]["providers"]
        self.assertEqual(list(provider), ["benchmark"])
        self.assertEqual(provider["benchmark"]["apiKey"], "$BENCH_API_KEY")
        self.assertEqual(provider["benchmark"]["api"], "openai-completions")
        self.assertEqual(provider["benchmark"]["baseUrl"], CONFIG["base_url"])
        self.assertEqual(provider["benchmark"]["models"][0]["contextWindow"], 32768)
        self.assertEqual(provider["benchmark"]["models"][0]["maxTokens"], 4096)
        self.assertEqual(len(provider["benchmark"]["models"]), 1)
        settings = files["/home/bench/.pi/agent/settings.json"]
        self.assertEqual(settings["enabledModels"], ["benchmark/served-model"])
        self.assertEqual(settings["packages"], [])
        self.assertNotIn("--tools", result["command"])
        for flag in ("--offline", "--no-extensions", "--no-skills", "--no-session"):
            self.assertIn(flag, result["command"])

    def test_omp_all_model_roles_and_no_delegation_or_background_models(self):
        result = self.prepare("oh-my-pi")
        files = self.files(result)
        provider = files["/home/bench/.omp/agent/models.yml"]["providers"]["benchmark"]
        self.assertEqual(provider["apiKey"], "BENCH_API_KEY")
        self.assertEqual(len(provider["models"]), 1)
        settings = files["/home/bench/.omp/agent/config.yml"]
        self.assertEqual(set(settings["modelRoles"].values()), {"benchmark/served-model"})
        self.assertGreaterEqual(len(settings["modelRoles"]), 9)
        self.assertFalse(settings["retry"]["modelFallback"])
        self.assertFalse(settings["advisor"]["enabled"])
        self.assertFalse(settings["compaction"]["asyncEnabled"])
        self.assertFalse(settings["compaction"]["idleEnabled"])
        self.assertFalse(settings["tools"]["xdev"])
        command = result["command"]
        for flag in ("--model", "--smol", "--slow", "--plan"):
            self.assertEqual(command[command.index(flag) + 1], "benchmark/served-model")
        for flag in (
            "--no-title",
            "--no-prewalk",
            "--auto-approve",
            "--no-session",
            "--no-skills",
            "--no-extensions",
        ):
            self.assertIn(flag, command)
        tools = set(command[command.index("--tools") + 1].split(","))
        self.assertFalse(tools & {"task", "wait", "ask"})
        self.assertTrue({"bash", "eval", "lsp", "debug", "ast_edit", "todo"} <= tools)
        self.assertNotIn("ast_grep", tools)  # Disabled-by-default tools make OMP reject startup.

    def test_opencode_single_provider_and_main_small_build_models(self):
        result = self.prepare("opencode")
        config = self.files(result)["/home/bench/.config/opencode/opencode.json"]
        self.assertEqual(config["enabled_providers"], ["benchmark"])
        self.assertEqual(list(config["provider"]), ["benchmark"])
        provider = config["provider"]["benchmark"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(provider["options"]["apiKey"], "{env:BENCH_API_KEY}")
        self.assertEqual(list(provider["models"]), ["served-model"])
        self.assertEqual(config["model"], "benchmark/served-model")
        self.assertEqual(config["small_model"], config["model"])
        self.assertEqual(config["agent"]["build"]["model"], config["model"])
        self.assertFalse(config["tools"]["task"])
        self.assertEqual(config["permission"]["task"], "deny")
        self.assertEqual(config["permission"]["*"], "allow")
        self.assertFalse(config["autoupdate"])
        self.assertEqual(config["share"], "disabled")
        self.assertEqual(
            provider["models"]["served-model"]["options"],
            {"max_tokens": 4096, "chat_template_kwargs": {"enable_thinking": False}},
        )
        self.assertEqual(result["command"][:4], ["opencode", "run", "--format", "json"])

    def test_common_profile(self):
        for harness in HARNESS_VERSIONS:
            with self.subTest(harness=harness):
                result = self.prepare(harness, tool_profile="common")
                if harness == "opencode":
                    config = self.files(result)["/home/bench/.config/opencode/opencode.json"]
                    tools = config["tools"]
                    self.assertFalse(tools["*"])
                    self.assertEqual(
                        {name for name, enabled in tools.items() if enabled},
                        {"read", "write", "edit", "bash"},
                    )
                    self.assertEqual(config["permission"]["*"], "deny")
                    self.assertEqual(config["permission"]["bash"], "allow")
                else:
                    command = result["command"]
                    self.assertEqual(command[command.index("--tools") + 1], "read,write,edit,bash")

    def test_thinking_default_and_reasoning_effort(self):
        for harness in HARNESS_VERSIONS:
            for thinking in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
                with self.subTest(harness=harness, thinking=thinking):
                    result = self.prepare(
                        harness, reasoning=True, thinking=thinking, api_profile="openai-compatible"
                    )
                    self.assertEqual(result["configuration"]["thinking"], thinking)
                    if harness == "opencode":
                        config = next(iter(self.files(result).values()))
                        options = config["provider"]["benchmark"]["models"]["served-model"][
                            "options"
                        ]
                        self.assertEqual(
                            options["reasoningEffort"], "none" if thinking == "off" else thinking
                        )
                    else:
                        self.assertEqual(
                            result["command"][result["command"].index("--thinking") + 1], thinking
                        )
        self.assertEqual(self.prepare("pi")["configuration"]["thinking"], "off")
        self.assertIsNone(self.prepare("pi")["configuration"]["input_price"])

    def test_llama_cpp_default_controls_template_without_changing_capabilities(self):
        for harness in HARNESS_VERSIONS:
            for reasoning, thinking in (
                (False, "off"),
                (True, "off"),
                *((True, level) for level in ("minimal", "low", "medium", "high", "xhigh", "max")),
            ):
                with self.subTest(harness=harness, reasoning=reasoning, thinking=thinking):
                    result = self.prepare(
                        harness, reasoning=reasoning, thinking=thinking, max_tokens=1731
                    )
                    self.assertEqual(result["configuration"]["api_profile"], "llama-cpp")
                    if harness == "opencode":
                        provider = next(iter(self.files(result).values()))["provider"]["benchmark"]
                        model = provider["models"]["served-model"]
                        options = model["options"]
                        self.assertEqual(options["max_tokens"], 1731)
                        self.assertEqual(model["limit"]["output"], 1731)
                        self.assertNotIn("reasoningEffort", options)
                    else:
                        files = self.files(result)
                        path = (
                            "/home/bench/.pi/agent/models.json"
                            if harness == "pi"
                            else "/home/bench/.omp/agent/models.yml"
                        )
                        model = files[path]["providers"]["benchmark"]["models"][0]
                        options = (
                            model["samplingParams"]
                            if harness == "pi"
                            else model["compat"]["extraBody"]
                        )
                        self.assertEqual(model["id"], CONFIG["model"])
                        self.assertEqual(model["maxTokens"], 1731)
                    self.assertIs(model["reasoning"], reasoning)
                    self.assertEqual(
                        options["chat_template_kwargs"], {"enable_thinking": thinking != "off"}
                    )

    def test_generic_profile_omits_llama_overrides_and_preserves_token_limit(self):
        for harness in HARNESS_VERSIONS:
            with self.subTest(harness=harness):
                result = self.prepare(harness, api_profile="openai-compatible", max_tokens=1731)
                self.assertEqual(result["configuration"]["api_profile"], "openai-compatible")
                serialized = json.dumps(self.files(result))
                for field in (
                    "chat_template_kwargs",
                    "samplingParams",
                    "extraBody",
                    "reasoningEffort",
                ):
                    self.assertNotIn(field, serialized)
                if harness == "opencode":
                    config = next(iter(self.files(result).values()))
                    self.assertEqual(
                        config["provider"]["benchmark"]["models"]["served-model"]["options"],
                        {"max_tokens": 1731},
                    )

    def test_literal_namespaced_model_is_not_split_into_another_provider(self):
        for harness in HARNESS_VERSIONS:
            result = self.prepare(harness, model="vendor/model:7b")
            self.assertIn("benchmark/vendor/model:7b", result["command"])
            self.assertEqual(result["configuration"]["model"], "vendor/model:7b")

    def test_invalid_configuration_rejected_before_writing(self):
        invalid = (
            {"model": ""},
            {"model": "x; touch pwned"},
            {"model": "$(id)"},
            {"model": "{env:KEY}"},
            {"model": "foo,*"},
            {"base_url": "file:///tmp/a"},
            {"base_url": "https://user:password@example.com/v1"},
            {"base_url": "https://example.com/v1?api_key=secret"},
            {"base_url": "https://example.com/{env:KEY}"},
            {"base_url": "http://localhost:bad/v1"},
            {"base_url": "http://localhost/\n"},
            {"context_window": 0},
            {"context_window": True},
            {"max_tokens": 4096.5},
            {"max_tokens": 40000},
            {"reasoning": "true"},
            {"thinking": "high"},
            {"thinking": "auto"},
            {"tool_profile": "all"},
            {"api_profile": "generic"},
            {"api_profile": ""},
            {"api_profile": None},
            {"api_profile": False},
            {"api_profile": []},
            {"api_profile": {}},
            {"api_profile": "LLAMA-CPP"},
            {"api_key_env": "$(cat key)"},
            {"input_price": -1},
            {"input_price": float("nan")},
            {"output_price": float("inf")},
            {"output_price": True},
            {"input_price": "1"},
        )
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.prepare("pi", **update)
        self.assertEqual(list(self.directory.iterdir()), [])


class UsageTests(unittest.TestCase):
    def test_pi_and_omp_only_final_assistant_usage_and_tool_start(self):
        stream = jsonl(
            {"type": "session", "usage": PI_USAGE},
            {"type": "message_start", "message": {"role": "assistant", "usage": PI_USAGE}},
            {"type": "message_update", "usage": PI_USAGE, "message": {"usage": PI_USAGE}},
            {"type": "message_end", "message": {"role": "user", "usage": PI_USAGE}},
            assistant(PI_USAGE, timestamp=1),
            assistant(PI_USAGE, timestamp=1),
            {"type": "turn_end", "message": {"role": "assistant", "usage": PI_USAGE}},
            {"type": "tool_execution_start", "toolCallId": "a"},
            {"type": "tool_execution_start", "toolCallId": "a"},
            {"type": "tool_execution_update", "toolCallId": "a"},
            {"type": "tool_execution_end", "toolCallId": "a"},
            {"type": "tool_execution_start", "toolCallId": "b"},
            assistant(PI_USAGE, timestamp=2),
            {"type": "agent_end", "messages": [{"role": "assistant", "usage": PI_USAGE}]},
        )
        for harness in ("pi", "oh-my-pi", "omp"):
            with self.subTest(harness=harness):
                usage = parse_usage(harness, stream)
                self.assertEqual([usage[field] for field in TOKEN_FIELDS], [200, 40, 20, 10])
                self.assertEqual(usage["tool_calls"], 2)
                self.assertIsNone(usage["estimated_cost_usd"])

    def test_opencode_deduplicates_part_ids_and_call_ids_not_wrapped_usage(self):
        stream = jsonl(
            {"type": "step_start", "part": {"tokens": OC_TOKENS}},
            step(OC_TOKENS, id="p1", messageID="m1"),
            step(OC_TOKENS, id="p1", messageID="m1"),
            {"type": "text", "part": {"tokens": OC_TOKENS}},
            {"type": "message.updated", "properties": {"info": {"tokens": OC_TOKENS}}},
            {"type": "tool_use", "part": {"callID": "a", "state": {"status": "completed"}}},
            {"type": "tool_use", "part": {"callID": "a", "state": {"status": "error"}}},
            {"type": "tool_use", "part": {"callID": "b"}},
            step(OC_TOKENS, id="p2", messageID="m2"),
        )
        usage = parse_usage("opencode", stream)
        self.assertEqual([usage[field] for field in TOKEN_FIELDS], [200, 40, 20, 10])
        self.assertEqual(usage["tool_calls"], 2)

    def test_dedup_scopes_and_equal_idless_responses_are_distinct(self):
        for harness, event in (
            ("pi", assistant(PI_USAGE, id="m1")),
            ("opencode", step(OC_TOKENS, id="p1")),
        ):
            stream = jsonl({**event, "sessionID": "s1"}, {**event, "sessionID": "s2"})
            self.assertEqual(parse_usage(harness, stream)["input_tokens"], 200)
        for harness, event in (("pi", assistant(PI_USAGE)), ("opencode", step(OC_TOKENS))):
            self.assertEqual(parse_usage(harness, jsonl(event, event))["input_tokens"], 200)

    def test_unknown_is_not_zero_and_malformed_nested_data_is_ignored(self):
        junk = "not json\n{broken\nnull\n[]\n42\n" + jsonl(
            {"type": "text", "payload": {"usage": PI_USAGE, "tokens": OC_TOKENS}},
            {"type": "tool_execution_end"},
            {"type": "message_end", "message": []},
        )
        for harness in HARNESS_VERSIONS:
            with self.subTest(harness=harness):
                usage = parse_usage(harness, junk, 1, 2)
                self.assertTrue(all(value is None for value in usage.values()))
                self.assertIsNone(harness_error(harness, junk))
        unknown = parse_usage("opencode", jsonl({"type": "tool_use", "part": {}}))
        self.assertIsNone(unknown["tool_calls"])

    def test_start_only_stream_does_not_claim_zero_tool_calls(self):
        for harness, kind in (
            ("pi", "agent_start"),
            ("oh-my-pi", "agent_start"),
            ("opencode", "step_start"),
        ):
            with self.subTest(harness=harness):
                usage = parse_usage(harness, jsonl({"type": kind}))
                self.assertTrue(all(value is None for value in usage.values()))

    def test_explicit_zero_is_known_and_unpriced_reported_cost_is_ignored(self):
        for harness, event in (
            ("pi", assistant(dict.fromkeys(PI_USAGE, 0))),
            ("opencode", step({"input": 0, "output": 0, "cache": {"read": 0, "write": 0}})),
        ):
            usage = parse_usage(harness, jsonl(event), 0, 0)
            self.assertEqual([usage[field] for field in TOKEN_FIELDS], [0, 0, 0, 0])
            self.assertEqual(usage["tool_calls"], 0)
            self.assertEqual(usage["estimated_cost_usd"], 0)
        event = assistant({**PI_USAGE, "cost": {"total": 999}})
        self.assertIsNone(parse_usage("pi", jsonl(event))["estimated_cost_usd"])

    def test_missing_counts_make_aggregate_unknown_not_a_partial_total(self):
        for harness, complete, partial in (
            ("pi", assistant(PI_USAGE), assistant({"output": 3})),
            ("opencode", step(OC_TOKENS), step({"output": 3})),
        ):
            for stream in (jsonl(complete, partial), jsonl(partial, complete)):
                usage = parse_usage(harness, stream, 1, 2)
                self.assertIsNone(usage["input_tokens"])
                self.assertEqual(usage["output_tokens"], 23)
                self.assertIsNone(usage["cache_read_tokens"])
                self.assertIsNone(usage["cache_write_tokens"])
                self.assertIsNone(usage["estimated_cost_usd"])
        self.assertIsNone(
            parse_usage("pi", jsonl(assistant(PI_USAGE), assistant()))["output_tokens"]
        )

    def test_invalid_counts_and_prices_never_become_free_usage(self):
        for invalid in (-1, True, "100", 1.5, float("nan"), float("inf"), None):
            with self.subTest(invalid=invalid):
                usage = parse_usage("pi", jsonl(assistant({**PI_USAGE, "input": invalid})), 1, 2)
                self.assertIsNone(usage["input_tokens"])
                self.assertIsNone(usage["estimated_cost_usd"])
        for invalid in (-1, True, "1", float("nan"), float("inf"), None):
            with self.subTest(price=invalid):
                self.assertIsNone(
                    parse_usage("pi", jsonl(assistant(PI_USAGE)), invalid, 2)["estimated_cost_usd"]
                )
                self.assertIsNone(
                    parse_usage("pi", jsonl(assistant(PI_USAGE)), 1, invalid)["estimated_cost_usd"]
                )

    def test_cost_uses_supplied_per_million_prices_and_disclaims_cache_tariffs(self):
        for harness, event in (("pi", assistant(PI_USAGE)), ("opencode", step(OC_TOKENS))):
            usage = parse_usage(harness, jsonl(event), 2, 5)
            self.assertAlmostEqual(usage["estimated_cost_usd"], (115 * 2 + 20 * 5) / 1_000_000)
            self.assertIn("cache reads/writes", usage["cost_note"])
            self.assertIn("not provider-specific", usage["cost_note"])

    def test_cost_overflow_does_not_discard_counts_or_crash(self):
        huge = 10**400
        usage = parse_usage("pi", jsonl(assistant({**PI_USAGE, "input": huge})), 1, 2)
        self.assertEqual(usage["input_tokens"], huge)
        self.assertIsNone(usage["estimated_cost_usd"])
        self.assertIsNone(
            parse_usage("pi", jsonl(assistant(PI_USAGE)), huge, 2)["estimated_cost_usd"]
        )

    def test_jsonl_splits_only_lf(self):
        event = assistant(PI_USAGE, content="Unicode separators\u2028and\u2029within a JSON string")
        stream = jsonl(event).replace("\n", "\r\n")
        self.assertEqual(parse_usage("pi", stream)["input_tokens"], 100)


class TerminationTests(unittest.TestCase):
    def test_final_length_warns_with_final_not_aggregate_tokens(self):
        for harness in ("pi", "oh-my-pi", "omp", "opencode"):
            make = step if harness == "opencode" else assistant
            reason_key = "reason" if harness == "opencode" else "stopReason"
            stream = jsonl(
                make({"output": 15, "reasoning": 5}, **{reason_key: "tool-calls"}),
                make({"output": 4096, "reasoning": 4000}, **{reason_key: "length"}),
                {"type": "agent_end"},
                {"type": "agent_settled"},
                {"type": "text", "part": {"reason": "stop"}},
            )
            with self.subTest(harness=harness):
                termination = parse_termination(harness, stream)
                self.assertEqual(termination["reason"], "length")
                self.assertEqual(termination["output_tokens"], 4096)
                self.assertEqual(termination["reasoning_tokens"], 4000)
                self.assertIn("token limit (length)", termination["warning"])
                self.assertIn("server's context capacity", termination["warning"])
                self.assertIn("not an execution error", termination["warning"])
                self.assertIsNone(harness_error(harness, stream))
                self.assertEqual(parse_usage(harness, stream)["output_tokens"], 4111)

    def test_later_response_replaces_length_including_unknown_reason_and_counts(self):
        for harness in HARNESS_VERSIONS:
            make = step if harness == "opencode" else assistant
            reason_key = "reason" if harness == "opencode" else "stopReason"
            for reason in ("stop", "tool-calls", "future-reason", None, [], {}):
                stream = jsonl(
                    make({"output": 4096, "reasoning": 4000}, **{reason_key: "length"}),
                    make({"output": 0}, **{reason_key: reason}),
                )
                with self.subTest(harness=harness, reason=reason):
                    termination = parse_termination(harness, stream)
                    self.assertEqual(
                        termination["reason"], reason if isinstance(reason, str) else None
                    )
                    self.assertEqual(termination["output_tokens"], 0)
                    self.assertIsNone(termination["reasoning_tokens"])
                    self.assertIsNone(termination["warning"])

    def test_unknown_and_malformed_streams_do_not_invent_termination(self):
        junk = "not json\n{broken\nnull\n[]\n42\n" + jsonl(
            {"type": "message_end", "message": []},
            {"type": "message_end", "message": {"role": "user", "stopReason": "length"}},
            {"type": "message_start", "message": {"role": "assistant", "stopReason": "length"}},
            {"type": "message_update", "message": {"role": "assistant", "stopReason": "length"}},
            {"type": "step_finish", "part": []},
            {"type": "step_start", "part": {"reason": "length"}},
            {"type": "tool_use", "part": {"reason": "length"}},
            {"type": "agent_end", "messages": [{"role": "assistant", "stopReason": "length"}]},
            {"type": [], "part": {"reason": "length"}},
        )
        for harness in HARNESS_VERSIONS:
            for stream in ("", junk):
                with self.subTest(harness=harness, stream=stream):
                    self.assertEqual(
                        parse_termination(harness, stream),
                        dict.fromkeys(("reason", "output_tokens", "reasoning_tokens", "warning")),
                    )
        with self.assertRaises(ValueError):
            parse_termination("unknown", junk)

    def test_invalid_or_absent_tokens_are_unknown_but_length_still_warns(self):
        for harness in HARNESS_VERSIONS:
            make = step if harness == "opencode" else assistant
            reason_key = "reason" if harness == "opencode" else "stopReason"
            invalid_counts = [
                {"output": value, "reasoning": value}
                for value in (-1, True, "4096", 1.5, float("nan"), float("inf"))
            ]
            for counts in (None, [], "bad", {}, *invalid_counts):
                with self.subTest(harness=harness, counts=counts):
                    termination = parse_termination(
                        harness, jsonl(make(counts, **{reason_key: "length"}))
                    )
                    self.assertEqual(termination["reason"], "length")
                    self.assertIsNone(termination["output_tokens"])
                    self.assertIsNone(termination["reasoning_tokens"])
                    self.assertIsNotNone(termination["warning"])

    def test_unrelated_or_malformed_events_after_length_do_not_clear_it(self):
        for harness in HARNESS_VERSIONS:
            make = step if harness == "opencode" else assistant
            reason_key = "reason" if harness == "opencode" else "stopReason"
            final = make({"output": 10}, **{reason_key: "length"})
            stream = (
                jsonl(
                    final,
                    {"type": "message_end", "message": None},
                    {"type": "step_finish", "part": None},
                    {"type": "tool_execution_end", "stopReason": "stop"},
                )
                + "{truncated\n"
            )
            self.assertEqual(
                parse_termination(harness, stream), parse_termination(harness, jsonl(final))
            )

    def test_jsonl_unicode_separators_and_unknown_reason_are_preserved(self):
        reason = "future\u2028reason\u2029&<value>"
        event = assistant({"output": 0, "reasoning": 0}, stopReason=reason)
        termination = parse_termination("pi", jsonl(event).replace("\n", "\r\n"))
        self.assertEqual(
            termination,
            {"reason": reason, "output_tokens": 0, "reasoning_tokens": 0, "warning": None},
        )


class ErrorTests(unittest.TestCase):
    def test_explicit_error_events_and_empty_error(self):
        for harness in HARNESS_VERSIONS:
            for event, expected in (
                ({"type": "error", "error": "provider failed"}, "provider failed"),
                (
                    {
                        "type": "error",
                        "error": {"name": "APIError", "data": {"message": "HTTP 401"}},
                    },
                    "HTTP 401",
                ),
                (
                    {
                        "type": "session.error",
                        "properties": {"error": {"message": "quota exhausted"}},
                    },
                    "quota exhausted",
                ),
                ({"type": "error"}, "Harness emitted an error event"),
            ):
                with self.subTest(harness=harness, event=event):
                    self.assertEqual(harness_error(harness, jsonl(event)), expected)

    def test_pi_and_omp_emitted_assistant_retry_extension_and_compaction_errors(self):
        for harness in ("pi", "oh-my-pi"):
            for event, expected in (
                (
                    assistant(PI_USAGE, stopReason="error", errorMessage="bad request"),
                    "bad request",
                ),
                (assistant(stopReason="aborted"), "Assistant response failed or was aborted"),
                (
                    {"type": "auto_retry_end", "success": False, "finalError": "retry failed"},
                    "retry failed",
                ),
                ({"type": "extension_error", "error": "extension failed"}, "extension failed"),
                ({"type": "compaction_end", "errorMessage": "context failed"}, "context failed"),
                (
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "error",
                            "error": {"message": "stream failed"},
                        },
                    },
                    "stream failed",
                ),
            ):
                with self.subTest(harness=harness, event=event):
                    self.assertEqual(harness_error(harness, jsonl(event)), expected)

    def test_tool_errors_and_prose_are_not_harness_errors(self):
        stream = jsonl(
            {"type": "text", "part": {"text": "Error: example"}},
            {"type": "tool_use", "part": {"state": {"status": "error", "error": "pytest failed"}}},
            {"type": "tool_execution_end", "isError": True},
            assistant(stopReason="stop", content="error", errorMessage="not a stop error"),
            {"type": "auto_retry_end", "success": True},
        )
        for harness in HARNESS_VERSIONS:
            self.assertIsNone(harness_error(harness, stream))

    def test_observed_error_is_retained_even_if_later_recovered(self):
        stream = jsonl(
            assistant(stopReason="error", errorMessage="first failure"),
            assistant(PI_USAGE, stopReason="stop"),
        )
        self.assertEqual(harness_error("pi", stream), "first failure")


class DockerfileTests(unittest.TestCase):
    def test_common_toolchain_nonroot_home_and_no_entrypoint(self):
        base = (PROJECT / "src" / "agent_bench" / "docker" / "Dockerfile.base").read_text()
        self.assertIn("FROM node:22.19.0-bookworm", base)
        self.assertIn("FROM oven/bun:1.3.14 AS bun", base)
        self.assertIn("COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun", base)
        for package in (
            "python3",
            "python3-venv",
            "git",
            "bash",
            "curl",
            "ripgrep",
            "jq",
            "build-essential",
            "pytest",
            "coverage",
            "ruff",
            "pyright",
            "debugpy",
            "IPython",
            "jupyter-client",
            "ipykernel",
        ):
            self.assertIn(package, base)
        self.assertIn("python3 -m venv /opt/venv", base)
        self.assertIn("HOME=/home/bench", base)
        self.assertIn("USER bench", base)
        self.assertIn("WORKDIR /workspace", base)
        self.assertIn("ENTRYPOINT []", base)
        self.assertNotIn("VOLUME", base)
        self.assertNotIn("docker.sock", base)

    def test_pinned_global_installs_and_nonroot_build_smokes(self):
        for harness, package, executable in (
            ("pi", "@earendil-works/pi-coding-agent", "pi"),
            ("oh-my-pi", "@oh-my-pi/pi-coding-agent", "omp"),
            ("opencode", "opencode-ai", "opencode"),
        ):
            with self.subTest(harness=harness):
                dockerfile = (
                    PROJECT / "src" / "agent_bench" / "docker" / f"Dockerfile.{harness}"
                ).read_text()
                self.assertIn("ARG BASE_IMAGE=agent-bench-base:local", dockerfile)
                self.assertIn("FROM ${BASE_IMAGE}", dockerfile)
                self.assertIn(f"ARG HARNESS_VERSION={HARNESS_VERSIONS[harness]}", dockerfile)
                self.assertIn(package + "@${HARNESS_VERSION}", dockerfile)
                self.assertIn("--global", dockerfile)
                installer = "bun" if harness == "oh-my-pi" else "npm"
                self.assertIn(f"RUN HOME=/root {installer} install --global", dockerfile)
                self.assertIn("chown -R bench:bench /home/bench", dockerfile)
                self.assertLess(dockerfile.index("USER root"), dockerfile.index("--global"))
                self.assertLess(
                    dockerfile.index("USER bench"), dockerfile.index(f"RUN {executable} --version")
                )
                self.assertIn("WORKDIR /workspace", dockerfile)
                self.assertIn("ENTRYPOINT []", dockerfile)


if __name__ == "__main__":
    unittest.main()
