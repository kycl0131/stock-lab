"""Offline checks for the LIVE OpenAI Responses proposer. urlopen is mocked; no network, no real keys."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from stocklab import live_ai, live_auto as auto
from stocklab.domain import ValidationError, canonical, digest
from test_session_research import kr_config   # discover -s tests puts tests/ on sys.path

FAKE_KEY = "sk-test-NOT-A-REAL-KEY-0000"
MODEL = "gpt-test-model-2026-01-01"


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


def response(text=None, *, status="completed", output=None, **extra):
    if output is None:
        output = [{"type": "reasoning", "id": "rs_1", "summary": []},
                  {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                   "content": [{"type": "output_text", "text": text if text is not None else proposal_text(),
                                "annotations": []}]}]
    return {"id": "resp_abc123", "object": "response", "status": status, "error": None,
            "incomplete_details": None, "model": MODEL + "-actual", "output": output,
            "usage": {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 150,
                      "output_tokens_details": {"reasoning_tokens": 90}, "total_tokens": 1350}, **extra}


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self.body[:limit] if limit >= 0 else self.body


class FakeUrlopen:
    """Records every call; returns one canned body or raises one canned error."""

    def __init__(self, payload=None, *, raw=None, error=None):
        self.calls = []
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.error = error

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.raw)


def refuse_network(*_args, **_kwargs):
    raise AssertionError("network access attempted in an offline test")


class OpenAIProposerTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY})
        self.env.start()
        self.net = patch.object(socket.socket, "connect", refuse_network)
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.env.stop()

    def decide(self, fake, provider="openai"):
        with patch.object(live_ai, "urlopen", fake):
            return live_ai.decide(snapshot(), provider=provider, model=MODEL, max_output_tokens=900, timeout=30)

    def test_exact_request_payload(self):
        fake = FakeUrlopen(response())
        self.decide(fake)
        self.assertEqual(len(fake.calls), 1)
        request, timeout = fake.calls[0]
        self.assertEqual((request.full_url, request.get_method(), timeout),
                         ("https://api.openai.com/v1/responses", "POST", 30))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {
            "model": MODEL,
            "instructions": live_ai.SYSTEM_PROMPT,
            "input": json.dumps(snapshot(), ensure_ascii=False),
            "tools": [],
            "store": False,
            "max_output_tokens": 900,
            "text": {"format": {"type": "json_schema", "name": "stocklab_proposal", "strict": True,
                                "schema": live_ai.PROPOSAL_SCHEMA}},
        })
        self.assertNotIn(FAKE_KEY.encode(), request.data)
        schema = live_ai.PROPOSAL_SCHEMA   # strict mode: every property required, no extras
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_invalid_request_parameters_never_call(self):
        fake = FakeUrlopen(response())
        with patch.object(live_ai, "urlopen", fake):
            for kwargs in ({"model": "", "max_output_tokens": 900}, {"model": None, "max_output_tokens": 900},
                           {"model": MODEL, "max_output_tokens": 50}, {"model": MODEL, "max_output_tokens": True}):
                proposal, meta = live_ai.decide(snapshot(), provider="openai", **kwargs)
                self.assertEqual(proposal["action"], "HOLD")
                self.assertIsNotNone(meta["error"])
            proposal, meta = live_ai.decide({**snapshot(), "account": "x"}, provider="openai", model=MODEL,
                                            max_output_tokens=900)
            self.assertEqual(proposal["action"], "HOLD")
        self.assertEqual(fake.calls, [])

    def test_valid_decision_and_audit_metadata(self):
        proposal, meta = self.decide(FakeUrlopen(response()))
        self.assertEqual(proposal, json.loads(proposal_text()))
        self.assertIsNone(meta["error"])
        self.assertEqual((meta["provider"], meta["requested_model"], meta["model"], meta["response_id"]),
                         ("openai", MODEL, MODEL + "-actual", "resp_abc123"))
        self.assertEqual(meta["usage"], {"input_tokens": 1200, "output_tokens": 150, "total_tokens": 1350,
                                         "reasoning_tokens": 90})
        self.assertEqual((meta["prompt_hash"], meta["snapshot_hash"], meta["prompt_version"]),
                         (digest(live_ai.SYSTEM_PROMPT), digest(snapshot()), live_ai.PROMPT_VERSION))
        self.assertNotIn(FAKE_KEY, canonical(meta))
        hold, _ = self.decide(FakeUrlopen(response(proposal_text(action="HOLD", symbol="", evidence_ids=[]))))
        self.assertEqual(hold["action"], "HOLD")
        sell, _ = self.decide(FakeUrlopen(response(proposal_text(action="SELL", symbol="000660",
                                                                 evidence_ids=["000660-4"]))))
        self.assertEqual((sell["action"], sell["symbol"]), ("SELL", "000660"))

    def assert_hold(self, fake, error_fragment, *, audited=True):
        proposal, meta = self.decide(fake)
        self.assertEqual(proposal["action"], "HOLD", error_fragment)
        self.assertEqual(proposal["symbol"], "")
        self.assertIn(error_fragment, meta["error"])
        self.assertEqual(len(fake.calls), 1, "exactly one attempt, no retry")
        self.assertNotIn(FAKE_KEY, canonical(meta))
        if audited:
            self.assertEqual(meta["response_id"], "resp_abc123")
            self.assertEqual(meta["usage"]["output_tokens"], 150)

    def test_refusal_fails_closed(self):
        refusal = [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "I can't help with that."}]}]
        self.assert_hold(FakeUrlopen(response(output=refusal)), "refused")

    def test_incomplete_and_failed_status_fail_closed(self):
        self.assert_hold(FakeUrlopen(response(status="incomplete",
                                              incomplete_details={"reason": "max_output_tokens"})),
                         "status incomplete (max_output_tokens)")
        self.assert_hold(FakeUrlopen(response(status="failed")), "status failed")
        self.assert_hold(FakeUrlopen(response(status="weird<script>")), "status invalid")
        failed = response()
        failed["error"] = {"code": "server_error", "message": "x"}
        self.assert_hold(FakeUrlopen(failed), "reported an error")

    def test_malformed_or_multiple_outputs_fail_closed(self):
        message = response()["output"][1]
        text = message["content"][0]
        cases = {
            "exactly one message": [message, message],
            "exactly one output_text": [{**message, "content": [text, text]}],
            "unexpected output item": [{"type": "function_call", "name": "x", "arguments": "{}"}, message],
            "incomplete or invalid": [{**message, "status": "incomplete"}],
        }
        for fragment, output in cases.items():
            self.assert_hold(FakeUrlopen(response(output=output)), fragment)
        self.assert_hold(FakeUrlopen(response(output=[])), "exactly one message")
        self.assert_hold(FakeUrlopen(response("not json {")), "not valid JSON")
        self.assert_hold(FakeUrlopen(response('{"action":"HOLD","action":"BUY","symbol":"005930",'
                                              '"reason":"r","evidence_ids":["005930-0"]}')), "repeats a JSON key")

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
            self.assert_hold(FakeUrlopen(response(text)), fragment)

    def test_transport_errors_fail_closed_without_retry(self):
        http = HTTPError("https://api.openai.com/v1/responses", 500, "err", {}, io.BytesIO(b"{}"))
        self.assert_hold(FakeUrlopen(error=http), "HTTP 500", audited=False)
        self.assert_hold(FakeUrlopen(error=URLError("down")), "unavailable", audited=False)
        self.assert_hold(FakeUrlopen(error=TimeoutError()), "unavailable", audited=False)
        self.assert_hold(FakeUrlopen(raw=b"<html>"), "unavailable", audited=False)
        self.assert_hold(FakeUrlopen(raw=b"x" * 1_000_001), "exceeds 1 MB", audited=False)

    def test_missing_key_and_non_openai_provider_never_call(self):
        for provider in ("anthropic", "", None):
            fake = FakeUrlopen(response())
            proposal, meta = self.decide(fake, provider=provider)
            self.assertEqual((proposal["action"], fake.calls), ("HOLD", []))
            self.assertIn("not supported", meta["error"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            fake = FakeUrlopen(response())
            proposal, meta = self.decide(fake)
            self.assertEqual((proposal["action"], fake.calls), ("HOLD", []))
            self.assertIn("OPENAI_API_KEY", meta["error"])
        source = Path(live_ai.__file__).read_text(encoding="utf-8")
        self.assertNotIn("api.anthropic.com", source)
        self.assertNotIn("ANTHROPIC_API_KEY", source)
        self.assertEqual(live_ai.PROVIDERS, ("openai",))


def model_config(**proposer):
    return {**kr_config(), "proposer": {"kind": "MODEL", "provider": "openai", "model": MODEL,
                                        "max_output_tokens": 900, "max_calls_per_day": 20,
                                        "usd_per_mtok_input": "1.25", "usd_per_mtok_output": "10",
                                        "krw_per_usd_for_cost": "1400", **proposer}}


class LiveConfigProviderTests(unittest.TestCase):
    def test_openai_model_config_is_accepted(self):
        self.assertEqual(auto.validate_config(model_config())["proposer"]["provider"], "openai")

    def test_anthropic_and_ambiguous_model_configs_are_rejected(self):
        for proposer in ({"provider": "anthropic"}, {"provider": "Anthropic"}, {"provider": None},
                         {"model": "claude-opus-5-5"}, {"model": None}, {"model": ""},
                         {"max_output_tokens": None}, {"max_output_tokens": 100_000}):
            with self.assertRaises(ValidationError, msg=str(proposer)):
                auto.validate_config(model_config(**proposer))
        legacy = model_config()
        legacy["proposer"]["max_tokens"] = legacy["proposer"].pop("max_output_tokens")
        with self.assertRaises(ValidationError):
            auto.validate_config(legacy)

    def test_template_names_openai_and_has_no_model_default(self):
        proposer = auto.template()["proposer"]
        self.assertEqual((proposer["provider"], proposer["model"], proposer["max_output_tokens"]),
                         ("openai", None, None))
        with self.assertRaises(ValidationError):
            auto.validate_config(auto.template())


if __name__ == "__main__":
    unittest.main()
