"""Offline checks for the LIVE Codex CLI (ChatGPT subscription) proposer.

`live_ai._run` is replaced by a recorder, so no Codex process, model inference, network, Kiwoom call or
order happens. The only real subprocesses are short local Python children that exercise the bounded runner.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from stocklab import live_ai, live_auto as auto, live_orders as lo, ts_forecast as ts
from stocklab.domain import ValidationError, canonical, digest, now
from test_session_research import KST, FakeKrClient, kr_config, uptrend_session_rows  # tests/ is on sys.path

MODEL = "gpt-test-codex-2026-01-01"
CODEX = os.path.join(tempfile.gettempdir(), "fake-bin", "codex.exe")
SECRETS = {"KIWOOM_APPKEY": "kiwoom-app-NOT-REAL", "KIWOOM_SECRETKEY": "kiwoom-secret-NOT-REAL",
           "OPENAI_API_KEY": "sk-openai-NOT-REAL", "CODEX_API_KEY": "sk-codex-NOT-REAL",
           "ANTHROPIC_API_KEY": "sk-ant-NOT-REAL", "NODE_OPTIONS": "--require=evil.js",
           "GITHUB_TOKEN": "ghp-NOT-REAL"}
KEPT = {"USERPROFILE": r"C:\Users\tester", "APPDATA": r"C:\Users\tester\AppData\Roaming",
        "CODEX_HOME": r"C:\Users\tester\.codex", "SYSTEMROOT": r"C:\Windows"}
STDERR_SECRET = b"raw stderr with token abc-SHOULD-NOT-BE-STORED"


def snapshot():
    start = datetime(2026, 9, 28, 0, 49, tzinfo=timezone.utc)

    def obs(symbol, prices):
        return [{"id": f"{symbol}-{i}", "event_at": (start + timedelta(minutes=i)).isoformat(),
                 "price": str(p), "volume": "1000"} for i, p in enumerate(prices)]
    return {"schema": live_ai.PROMPT_VERSION, "market": "KR", "as_of": (start + timedelta(minutes=6)).isoformat(),
            "candidates": [
                {"symbol": "005930", "exchange": "KRX", "sellable": False,
                 "observations": obs("005930", [10000, 10050, 10100, 10150, 10200])},
                {"symbol": "000660", "exchange": "KRX", "sellable": True,
                 "observations": obs("000660", [20000, 19900, 19800, 19700, 19600])}]}


def proposal_text(**overrides):
    value = {"action": "BUY", "symbol": "005930", "reason": "steady rise", "evidence_ids": ["005930-0", "005930-4"]}
    return json.dumps({**value, **overrides})


USAGE = {"input_tokens": 1200, "cached_input_tokens": 300, "output_tokens": 150, "reasoning_output_tokens": 90}


def events(text=None, *, items=None, extra=(), turns=1):
    """`codex exec --json` shaped JSONL."""
    text = proposal_text() if text is None else text
    items = items if items is not None else [{"id": "item_0", "type": "reasoning", "text": "thinking"},
                                             {"id": "item_1", "type": "agent_message", "text": text}]
    lines = [{"type": "thread.started", "thread_id": "thread_abc123"}, {"type": "turn.started"}]
    lines += [{"type": "item.completed", "item": item} for item in items]
    lines += list(extra) + [{"type": "turn.completed", "usage": USAGE}] * turns
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")


def run_result(returncode=0, stdout=b"", stderr=b"", **flags):
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr, "overflow": False, "stopped": False,
            "timed_out": False, **flags}


class FakeRun:
    """Stands in for live_ai._run; records every invocation and writes the --output-last-message file."""

    def __init__(self, *, login=None, exec_result=None, output=None, write_output=True):
        self.login = login or run_result(0, b"", b"Logged in using ChatGPT\n")
        self.exec_result = exec_result or run_result(0, events())
        self.output = (proposal_text() if output is None else output)
        self.write_output = write_output
        self.calls = []

    def __call__(self, argv, *, env, cwd, stdin_text, timeout, stop_on=None):
        call = {"argv": list(argv), "env": dict(env), "cwd": cwd, "stdin": stdin_text, "timeout": timeout,
                "stop_on": stop_on, "cwd_listing": sorted(os.listdir(cwd))}
        self.calls.append(call)
        if argv[1:] == ["login", "status"]:
            return self.login
        call["schema"] = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        if self.write_output:
            data = self.output if isinstance(self.output, bytes) else self.output.encode("utf-8")
            Path(argv[argv.index("--output-last-message") + 1]).write_bytes(data)
        return self.exec_result

    @property
    def exec_calls(self):
        return [c for c in self.calls if c["argv"][1] == "exec"]


def refuse_network(*_args, **_kwargs):
    raise AssertionError("network access attempted in an offline test")


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {**SECRETS, **KEPT})
        self.env.start()
        self.net = patch.object(socket.socket, "connect", refuse_network)
        self.net.start()
        self.which = patch.object(live_ai.shutil, "which", return_value=CODEX)
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.net.stop()
        self.env.stop()


class CodexInvocationTests(OfflineCase):
    def decide(self, fake, provider="codex_cli", snap=None, **kwargs):
        with patch.object(live_ai, "_run", fake):
            return live_ai.decide(snapshot() if snap is None else snap, provider=provider,
                                  **{"model": MODEL, "timeout": 90, **kwargs})

    def test_exact_invocation_login_first_isolated_dir_and_prompt(self):
        fake = FakeRun()
        proposal, meta = self.decide(fake)
        self.assertIsNone(meta["error"])
        self.assertEqual([c["argv"][1:3] for c in fake.calls], [["login", "status"], ["exec", "--model"]])
        login, run = fake.calls
        self.assertEqual((login["argv"], login["stdin"], login["timeout"]),
                         ([CODEX, "login", "status"], "", live_ai.LOGIN_TIMEOUT_SECONDS))
        workdir = run["cwd"]
        io_dir = os.path.dirname(run["argv"][run["argv"].index("--output-schema") + 1])
        self.assertEqual(run["argv"], [
            CODEX, "exec", "--model", MODEL, "--sandbox", "read-only", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json",
            "--disable", "shell_tool", "--config", "web_search=disabled",
            "--config", "forced_login_method=chatgpt", "--config", "project_doc_max_bytes=0",
            "--cd", workdir,
            "--output-schema", os.path.join(io_dir, "proposal.schema.json"),
            "--output-last-message", os.path.join(io_dir, "last_message.json"), "-"])
        self.assertEqual(login["cwd"], workdir)
        self.assertEqual(run["cwd_listing"], [], "the CLI starts in an empty directory")
        self.assertNotEqual(os.path.dirname(io_dir), str(Path(live_ai.__file__).resolve().parents[1]))
        self.assertTrue(os.path.basename(os.path.dirname(workdir)).startswith("stocklab-codex-"))
        self.assertFalse(os.path.exists(workdir), "temporary directory is removed after the run")
        self.assertEqual(run["schema"], live_ai.PROPOSAL_SCHEMA)
        self.assertFalse(live_ai.PROPOSAL_SCHEMA["additionalProperties"])
        self.assertEqual(run["timeout"], 90)
        self.assertIs(run["stop_on"], live_ai._tool_activity)
        self.assertEqual(run["stdin"], live_ai.build_prompt(snapshot()))
        self.assertTrue(run["stdin"].startswith(live_ai.SYSTEM_PROMPT))
        self.assertIn(json.dumps(snapshot(), ensure_ascii=False), run["stdin"])
        self.assertNotIn(run["stdin"], " ".join(run["argv"]), "prompt goes to stdin, not argv")
        for forbidden in ("Tools are forbidden", "shell commands", "repository", "web"):
            self.assertIn(forbidden, live_ai.SYSTEM_PROMPT)

    def test_subprocess_env_drops_credentials_and_keeps_login_vars(self):
        fake = FakeRun()
        self.decide(fake)
        for call in fake.calls:
            env = call["env"]
            for name, value in SECRETS.items():
                self.assertNotIn(name, env)
                self.assertNotIn(value, canonical(env))
            for name, value in KEPT.items():
                self.assertEqual(env.get(name), value)
        self.assertEqual(live_ai.subprocess_env({"PATH": "p", "codex_home": "c", "SOME_API_KEY": "x",
                                                 "APP_TOKEN": "y", "HOME": "h"}),
                         {"PATH": "p", "codex_home": "c", "HOME": "h"})

    def test_valid_decision_and_subscription_audit(self):
        proposal, meta = self.decide(FakeRun())
        self.assertEqual(proposal, json.loads(proposal_text()))
        self.assertEqual((meta["provider"], meta["billing"], meta["requested_model"], meta["model"],
                          meta["thread_id"], meta["cost_krw"]),
                         ("codex_cli", "chatgpt_subscription", MODEL, MODEL, "thread_abc123", "0"))
        self.assertEqual(meta["usage"], USAGE)
        self.assertEqual((meta["prompt_hash"], meta["snapshot_hash"], meta["prompt_version"]),
                         (digest(live_ai.SYSTEM_PROMPT), digest(snapshot()), live_ai.PROMPT_VERSION))
        for secret in SECRETS.values():
            self.assertNotIn(secret, canonical(meta))
        sell_text = proposal_text(action="SELL", symbol="000660", evidence_ids=["000660-4"])
        sell, _ = self.decide(FakeRun(exec_result=run_result(0, events(sell_text)), output=sell_text))
        self.assertEqual((sell["action"], sell["symbol"]), ("SELL", "000660"))
        hold_text = proposal_text(action="HOLD", symbol="", evidence_ids=[])
        hold, _ = self.decide(FakeRun(exec_result=run_result(0, events(hold_text)), output=hold_text))
        self.assertEqual(hold["action"], "HOLD")

    def assert_hold(self, fake, fragment, *, exec_calls=1, **kwargs):
        proposal, meta = self.decide(fake, **kwargs)
        self.assertEqual((proposal["action"], proposal["symbol"]), ("HOLD", ""), fragment)
        self.assertIn(fragment, meta["error"] or "", meta["error"])
        self.assertEqual(len(fake.exec_calls), exec_calls, "exactly one attempt, no retry")
        self.assertNotIn(STDERR_SECRET.decode(), canonical(meta))
        self.assertNotIn("SHOULD-NOT-BE-STORED", canonical(meta))
        return meta

    def test_login_must_be_chatgpt_not_api_key(self):
        cases = {
            "API key": run_result(0, b"", b"Logged in using an API key - sk-proj-***\n"),
            "not logged in with ChatGPT": run_result(1, b"", b"Not logged in\n"),
            "not logged in with ChatGPT ": run_result(0, b"", b"something else\n"),
            "did not complete": run_result(None, b"", b"", timed_out=True),
        }
        for fragment, login in cases.items():
            fake = FakeRun(login=login)
            self.assert_hold(fake, fragment.strip(), exec_calls=0)
            self.assertEqual(len(fake.calls), 1)

    def test_missing_cli_never_runs(self):
        fake = FakeRun()
        with patch.object(live_ai.shutil, "which", return_value=None):
            self.assert_hold(fake, "Codex CLI not found", exec_calls=0)
        self.assertEqual(fake.calls, [])

    def test_process_failures_fail_closed_without_retry(self):
        cases = {
            "timed out after 90s": run_result(None, b"", STDERR_SECRET, timed_out=True),
            "exceeded the size bound": run_result(-9, b"", b"", overflow=True),
            "tool or unexpected activity": run_result(-9, b"", b"", stopped=True),
            "exited with code 1; no automatic retry": run_result(1, b"", STDERR_SECRET),
            "exited with code 1: subscription usage/rate limit reached": run_result(
                1, b"", b"You've hit your usage limit. " + STDERR_SECRET),
            "exited with code 2: Codex login/auth failure": run_result(2, b"", b"401 Unauthorized " + STDERR_SECRET),
        }
        for fragment, result in cases.items():
            self.assert_hold(FakeRun(exec_result=result), fragment)

    def test_event_stream_failures_fail_closed(self):
        command = {"id": "item_2", "type": "command_execution", "command": "dir", "status": "completed"}
        cases = {
            "tool or unexpected activity": [events(items=[command, {"id": "i", "type": "agent_message",
                                                                    "text": proposal_text()}])],
            "unexpected event": [events(extra=[{"type": "session.configured"}])],
            "not valid JSONL": [b"warning: not json\n" + events(), events() + b"\xff\n",
                                events().replace(b'"turn.started"}', b'"turn.started", "type": "x"}')],
            "exactly one message": [events(items=[]), events(items=[{"id": "a", "type": "agent_message", "text": "x"},
                                                                     {"id": "b", "type": "agent_message", "text": "y"}])],
            "exactly one turn": [events(turns=0), events(turns=2)],
            "turn failed: subscription usage/rate limit reached": [
                events(extra=[{"type": "turn.failed", "error": {"message": "rate limit exceeded"}}])],
            "turn failed; no automatic retry": [events(extra=[{"type": "error", "message": "boom"}])],
        }
        for fragment, streams in cases.items():
            for stream in streams:
                self.assert_hold(FakeRun(exec_result=run_result(0, stream)), fragment)

    def test_output_file_failures_fail_closed_with_audit(self):
        meta = self.assert_hold(FakeRun(write_output=False), "output file is missing")
        self.assertEqual(meta["usage"], USAGE)
        self.assert_hold(FakeRun(output="x" * (live_ai.MAX_OUTPUT_BYTES + 1)), "exceeds the size bound")
        self.assert_hold(FakeRun(output=b"\xff\xfe"), "not valid UTF-8")
        self.assert_hold(FakeRun(output=proposal_text(action="HOLD", symbol="", evidence_ids=[])), "disagree")
        for text, fragment in (("not json {", "not valid JSON"),
                               ('{"action":"HOLD","action":"BUY","symbol":"005930","reason":"r",'
                                '"evidence_ids":["005930-0"]}', "repeats a JSON key")):
            self.assert_hold(FakeRun(exec_result=run_result(0, events(text)), output=text), fragment)

    def test_invalid_proposals_fail_closed(self):
        cases = {
            "unexpected fields": proposal_text()[:-1] + ', "quantity": 5}',
            "outside the allowed universe": proposal_text(symbol="035720"),
            "one lot per symbol": proposal_text(symbol="000660", evidence_ids=["000660-0"]),
            "not owned": proposal_text(action="SELL"),
            "evidence references": proposal_text(evidence_ids=["000660-0"]),
            "requires evidence": proposal_text(evidence_ids=[]),
            "empty symbol": proposal_text(action="HOLD"),
        }
        for fragment, text in cases.items():
            meta = self.assert_hold(FakeRun(exec_result=run_result(0, events(text)), output=text), fragment)
            self.assertEqual(meta["thread_id"], "thread_abc123")

    def test_invalid_request_or_provider_never_runs(self):
        fake = FakeRun()
        for kwargs in ({"model": ""}, {"model": None}, {"model": "Bad Model"}, {"timeout": 5}, {"timeout": 601},
                       {"timeout": True}, {"timeout": None}):
            self.assert_hold(fake, "Live AI", exec_calls=0, **kwargs)
        self.assert_hold(fake, "unexpected fields", exec_calls=0, snap={**snapshot(), "account": "x"})
        for provider in ("openai", "anthropic", "", None):
            self.assert_hold(fake, "codex_cli only", exec_calls=0, provider=provider)
        self.assertEqual(fake.calls, [])

    def test_no_openai_api_path(self):
        self.assertEqual(live_ai.PROVIDERS, ("codex_cli",))
        source = Path(live_ai.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "urlopen", "api.openai.com", "api.anthropic.com", "Authorization", "http://",
                          "https://", "max_output_tokens"):
            self.assertNotIn(forbidden, source)
        self.assertFalse(hasattr(live_ai, "urlopen"))

    def test_streaming_tool_guard(self):
        line = lambda item: json.dumps({"type": "item.started", "item": item}).encode()  # noqa: E731
        for kind in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list", "unknown"):
            self.assertTrue(live_ai._tool_activity(line({"id": "x", "type": kind})), kind)
        for kind in ("agent_message", "reasoning"):
            self.assertFalse(live_ai._tool_activity(line({"id": "x", "type": kind})), kind)
        self.assertFalse(live_ai._tool_activity(b'{"type":"turn.started"}'))
        self.assertFalse(live_ai._tool_activity(b"not json"))

    def test_windows_shim_arguments_are_checked(self):
        with self.assertRaises(ValidationError):
            live_ai._check_argv([r"C:\npm\codex.cmd", "exec", "--cd", r"C:\Temp\a&b"])
        live_ai._check_argv([r"C:\npm\codex.cmd", "exec", "--model", MODEL, "--cd", r"C:\Temp\stocklab-codex-x\work"])


class BoundedRunnerTests(unittest.TestCase):
    """Real local Python children (no Codex, no network) exercise timeout, size bound and the tool kill-switch."""

    def run_child(self, code, timeout=20, stop_on=None):
        with tempfile.TemporaryDirectory() as tmp:
            return live_ai._run([sys.executable, "-c", code], env=live_ai.subprocess_env(), cwd=tmp,
                                stdin_text="", timeout=timeout, stop_on=stop_on)

    def test_normal_exit_captures_output(self):
        result = self.run_child("import sys; print('ok'); sys.stderr.write('e'); sys.exit(3)")
        self.assertEqual((result["returncode"], result["stdout"].strip(), result["stderr"]), (3, b"ok", b"e"))
        self.assertFalse(result["timed_out"] or result["overflow"] or result["stopped"])

    def test_timeout_kills(self):
        result = self.run_child("import time; time.sleep(30)", timeout=1)
        self.assertTrue(result["timed_out"])
        self.assertIsNotNone(result["returncode"])

    def test_stdout_bound_kills(self):
        result = self.run_child("import sys, time; sys.stdout.write('x' * 2_000_000); sys.stdout.flush(); "
                                "time.sleep(30)")
        self.assertTrue(result["overflow"])
        self.assertLessEqual(len(result["stdout"]), live_ai.MAX_STDOUT_BYTES)

    def test_tool_activity_kills_immediately(self):
        code = ("import json, sys, time; print(json.dumps({'type': 'item.started', 'item': "
                "{'id': 'x', 'type': 'command_execution'}}), flush=True); time.sleep(30)")
        result = self.run_child(code, stop_on=live_ai._tool_activity)
        self.assertTrue(result["stopped"])
        self.assertFalse(result["timed_out"])


def model_config(**proposer):
    cfg = kr_config()
    cfg["cycle"]["lookback_bars"] = ts.WINDOW_BARS
    model_proposer = {"kind": "MODEL", "provider": "codex_cli", "model": MODEL,
                      "max_calls_per_day": 20, "timeout_seconds": 120,
                      "forecast": {"artifacts": {"KR": {"005930": {
                          "path": str(Path(tempfile.gettempdir(), "stocklab-test-missing-model.json")),
                          "sha256": "0" * 64}}}, "max_artifact_age_days": 30},
                      "news": {"archive_path": str(Path(tempfile.gettempdir(), "stocklab-test-missing-news.json")),
                               "max_status_age_seconds": 900, "lookback_hours": 24, "max_items_per_symbol": 5,
                               "required_sources": {"KR": ["gdelt"], "US": ["gdelt"]}}}
    model_proposer.update(proposer)
    return {**cfg, "proposer": model_proposer}


def openai_config():
    return {**kr_config(), "proposer": {"kind": "MODEL", "provider": "openai", "model": MODEL,
                                        "max_output_tokens": 900, "max_calls_per_day": 20,
                                        "usd_per_mtok_input": "1.25", "usd_per_mtok_output": "10",
                                        "krw_per_usd_for_cost": "1400"}}


class LiveConfigTests(unittest.TestCase):
    def test_codex_cli_config_is_accepted(self):
        self.assertEqual(auto.validate_config(model_config())["proposer"]["provider"], "codex_cli")

    def test_stale_and_ambiguous_configs_are_rejected(self):
        with self.assertRaises(ValidationError):
            auto.validate_config(openai_config())
        for proposer in ({"provider": "openai"}, {"provider": "anthropic"}, {"provider": "Codex_CLI"},
                         {"provider": None}, {"model": "claude-opus-5-5"}, {"model": None}, {"model": ""},
                         {"timeout_seconds": None}, {"timeout_seconds": 10}, {"timeout_seconds": 10_000},
                         {"max_calls_per_day": None}, {"max_calls_per_day": 0}, {"max_output_tokens": 900},
                         {"usd_per_mtok_input": "1"}, {"krw_per_usd_for_cost": "1400"}):
            with self.assertRaises(ValidationError, msg=str(proposer)):
                auto.validate_config(model_config(**proposer))
        missing = model_config()
        del missing["proposer"]["timeout_seconds"]
        with self.assertRaises(ValidationError):
            auto.validate_config(missing)
        self.assertNotEqual(digest(model_config()), digest(openai_config()))

    def test_template_names_codex_cli_without_price_fields_or_defaults(self):
        proposer = auto.template()["proposer"]
        pin = {"<symbol>": {"path": None, "sha256": None}}
        self.assertEqual(proposer, {"kind": "MODEL | BASELINE", "provider": "codex_cli", "model": None,
                                    "max_calls_per_day": None, "timeout_seconds": None,
                                    "forecast": {"artifacts": {"KR": pin, "US": pin}, "max_artifact_age_days": None},
                                    "news": {"archive_path": None, "max_status_age_seconds": None,
                                             "lookback_hours": None, "max_items_per_symbol": None,
                                             "required_sources": {"KR": [None], "US": [None]}}})
        with self.assertRaises(ValidationError):
            auto.validate_config(auto.template())


class LiveCycleTests(OfflineCase):
    """End-to-end cycle wiring with the Codex runner mocked; no ticket or order is ever prepared or sent."""

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.localappdata = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.localappdata.start()
        self.typed = patch.object(lo, "_typed", return_value=None)
        self.typed.start()
        self.conn = lo.open_db()

    def tearDown(self):
        self.conn.close()
        self.typed.stop()
        self.localappdata.stop()
        self.temp.cleanup()
        super().tearDown()

    def save(self, cfg):
        self.conn.execute("INSERT INTO auto_configs(config_json,config_hash,reason,created_at) VALUES(?,?,?,?)",
                          (canonical(cfg), digest(cfg), "offline test", now()))

    def arm(self):
        lo.set_cap(self.conn, market="KR", max_committed_krw=100_000, max_order_krw=50_000,
                   cash_fraction_pct="100", reason="offline test")
        config_id = self.conn.execute("SELECT MAX(config_id) FROM auto_configs").fetchone()[0]
        self.conn.execute("INSERT INTO auto_arming(action,config_id,kr_cap_id,us_cap_id,expires_at,reason,created_at) "
                          "VALUES('ARM',?,?,NULL,?,?,?)",
                          (config_id, lo._latest_cap(self.conn, "KR")["cap_id"],
                           (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "offline test", now()))
        self.assertTrue(auto.arming(self.conn)["armed"])

    def cycle(self, fake, mode="DRY_RUN"):
        clock = lambda: datetime(2026, 9, 28, 10, 0, 30, tzinfo=KST).astimezone(timezone.utc)  # noqa: E731
        with patch.object(live_ai, "_run", fake), \
                patch("stocklab.kiwoom_bridge.KiwoomReadOnly", FakeKrClient(uptrend_session_rows(), "100020")), \
                patch.object(lo, "_read_broker", return_value={"cash": Decimal("100000"), "fx": None}), \
                patch.object(lo, "prepare", side_effect=AssertionError("no ticket in this test")), \
                patch.object(lo, "send_auto", side_effect=AssertionError("no order in this test")):
            return auto.run_cycle(self.conn, "KR", mode=mode, clock=clock)

    def test_no_config_means_no_call_and_no_order(self):
        fake = FakeRun()
        self.assertEqual(self.cycle(fake)["skipped"], "NO_CONFIG")
        self.assertEqual(fake.calls, [])

    def test_model_config_requires_time_series_and_news_inputs(self):
        for field in ("forecast", "news"):
            config = model_config()
            del config["proposer"][field]
            with self.subTest(field), self.assertRaises(ValidationError):
                auto.validate_config(config)

    def test_live_cycle_without_forecast_is_hold_without_order(self):
        self.save(model_config())
        self.arm()
        fake = FakeRun(login=run_result(0, b"", b"Logged in using an API key - sk-***"))
        result = self.cycle(fake, mode="LIVE")
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["outcome"]["proposal"]["action"], "HOLD")
        self.assertTrue(result["outcome"]["model_error"].startswith("FORECAST:"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)

    def test_stale_openai_config_fails_closed_and_new_config_invalidates_arm(self):
        self.save(openai_config())
        self.arm()
        fake = FakeRun()
        with self.assertRaises(ValidationError):
            self.cycle(fake, mode="LIVE")
        self.assertEqual(fake.calls, [])
        path = Path(self.temp.name, "codex.json")
        path.write_text(json.dumps(model_config()), encoding="utf-8")
        auto.set_config(self.conn, path, "switch to codex_cli")
        authority = auto.arming(self.conn)
        self.assertEqual((authority["armed"], authority["reason"]), (False, "CONFIG_CHANGED"))


if __name__ == "__main__":
    unittest.main()
