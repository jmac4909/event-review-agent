import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

from anthropic import AsyncAnthropic
import httpx2
import pytest

from event_review import providers
from event_review.models import validate_verdict
from event_review.providers import AnthropicModel, CodexModel, ModelFailure, make_model, parse_codex_output, usage_counts
from event_review.reviewer import MockModel, build_context
from event_review.settings import Settings


VERDICT = {"decision": "rejected", "severity": None, "rationale": "Verified barrier separates the walkway.", "question": None}


@pytest.fixture
def context():
    source = Path(__file__).resolve().parents[2] / "data/events.json"
    event = json.loads(source.read_text())[0] | {"status": "available"}
    return build_context(event)


def anthropic_transport(monkeypatch, handler):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-secret")
    clients = []

    def client(**kwargs):
        clients.append(kwargs)
        return AsyncAnthropic(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

    monkeypatch.setattr(providers, "AsyncAnthropic", client)
    return clients


def api_message(text, stop_reason="end_turn"):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 20}}


def test_anthropic_drafts_use_the_draft_schema_and_report_usage(monkeypatch):
    result = {"reusable": False, "text": None, "rationale": "One person's permit is case evidence."}
    async def handler(request):
        body = json.loads(request.content)
        schema = body["output_config"]["format"]["schema"]
        assert schema["required"] == ["reusable", "text", "rationale"]
        assert schema["additionalProperties"] is False
        assert "tools" not in body
        return httpx2.Response(200, json=api_message(json.dumps(result)))
    anthropic_transport(monkeypatch, handler)
    reply = AnthropicModel(Settings()).draft({"instructions": "Draft only"}, timeout_seconds=1)
    assert reply.output == result and reply.usage["output_tokens"] == 20


def test_codex_drafts_use_the_draft_schema_without_internal_tools(tmp_path, monkeypatch):
    executable = fake_codex(tmp_path, '''import json, pathlib, sys
schema = json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
assert schema['required'] == ['reusable', 'text', 'rationale']
assert 'shell_tool' in sys.argv and 'multi_agent' in sys.argv
json.load(sys.stdin)
answer = {'reusable': False, 'text': None, 'rationale': 'Case evidence only.'}
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(answer)}}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 3}}))
''', monkeypatch)
    reply = CodexModel(Settings(codex_command=executable)).draft({"instructions": "Draft only"}, timeout_seconds=5)
    assert reply.output["reusable"] is False and reply.usage["input_tokens"] == 7


def test_anthropic_sends_only_recorded_context_and_schema_without_sdk_retries(monkeypatch, context):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        assert request.headers["x-api-key"] == "unit-test-secret"
        return httpx2.Response(200, json=api_message(json.dumps(VERDICT)))

    clients = anthropic_transport(monkeypatch, handler)
    output = AnthropicModel(Settings()).review(context, timeout_seconds=1)
    assert output.output == VERDICT
    assert output.usage == {"provider": "anthropic", "input_tokens": 10, "output_tokens": 20}
    assert clients[0]["max_retries"] == 0
    assert len(requests) == 1
    request = requests[0]
    assert request["system"] == context["instructions"]
    payload = json.loads(request["messages"][0]["content"])
    assert "instructions" not in payload
    assert payload | {"instructions": request["system"]} == context
    assert request["max_tokens"] == 1200
    assert request["output_config"]["format"]["schema"]["required"] == ["decision", "severity", "rationale", "question", "lookup"]


def test_anthropic_preserves_native_cache_counts_and_usage_for_invalid_output(monkeypatch, context):
    async def handler(request):
        message = api_message("not valid JSON")
        message["usage"].update(cache_read_input_tokens=30, cache_creation_input_tokens=40,
                                account_details="must not persist")
        return httpx2.Response(200, json=message)

    anthropic_transport(monkeypatch, handler)
    reply = AnthropicModel(Settings()).review(context, timeout_seconds=1)
    assert reply.output == {"raw_output": "not valid JSON"}
    assert reply.usage == {"provider": "anthropic", "input_tokens": 10, "output_tokens": 20,
                           "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}


@pytest.mark.parametrize("missing", [True, False])
def test_anthropic_reply_without_usage_does_not_invent_counts(monkeypatch, context, missing):
    async def handler(request):
        message = api_message(json.dumps(VERDICT))
        if missing:
            message.pop("usage")
        else:
            message["usage"] = None
        return httpx2.Response(200, json=message)

    anthropic_transport(monkeypatch, handler)
    reply = AnthropicModel(Settings()).review(context, timeout_seconds=1)
    assert reply.output == VERDICT and reply.usage is None


@pytest.mark.parametrize("raw", [None, {}, {"input_tokens": -1, "output_tokens": True},
                                {"input_tokens": "10", "output_tokens": 2**53}, []])
def test_missing_or_invalid_usage_is_unknown_not_zero(raw):
    assert usage_counts("anthropic", raw) is None
    stream = json.loads(codex_events().splitlines()[-1]) | {"usage": raw}
    reply = parse_codex_output("\n".join(codex_events().splitlines()[:-1] + [json.dumps(stream)]))
    assert reply.output == VERDICT and reply.usage is None


def test_codex_usage_is_cli_metadata_not_model_authored_json():
    authored = VERDICT | {"usage": {"input_tokens": 999999}}
    stream = codex_events(authored).splitlines()
    stream[-1] = json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20,
        "reasoning_output_tokens": 5, "account_details": "must not persist"}})
    reply = parse_codex_output("\n".join(stream))
    assert reply.output == authored
    assert reply.usage == {"provider": "codex", "input_tokens": 100, "cached_input_tokens": 80,
                           "output_tokens": 20, "reasoning_output_tokens": 5}
    with pytest.raises(ValueError):
        validate_verdict(reply.output)


@pytest.mark.parametrize("status, expected", [(401, "access was denied"), (403, "access was denied"),
                                             (429, "rate limit or quota"), (500, "rejected the request")])
def test_anthropic_errors_are_sanitized_and_not_retried(monkeypatch, context, status, expected):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx2.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "unit-test-secret"}})

    anthropic_transport(monkeypatch, handler)
    with pytest.raises(ModelFailure, match=expected) as failure:
        AnthropicModel(Settings()).review(context, timeout_seconds=1)
    assert "unit-test-secret" not in str(failure.value)
    assert len(requests) == 1


def test_anthropic_deadline_cancels_the_running_request(monkeypatch, context):
    cancelled = []

    async def handler(request):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    anthropic_transport(monkeypatch, handler)
    with pytest.raises(TimeoutError):
        AnthropicModel(Settings()).review(context, timeout_seconds=0.05)
    assert cancelled == [True]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_truncated_or_refused_response_is_retained_but_cannot_be_a_verdict(monkeypatch, context, stop_reason):
    async def handler(request):
        return httpx2.Response(200, json=api_message(json.dumps(VERDICT), stop_reason))

    anthropic_transport(monkeypatch, handler)
    result = AnthropicModel(Settings()).review(context, timeout_seconds=1)
    assert result.output["stop_reason"] == stop_reason
    assert json.loads(result.output["raw_output"]) == VERDICT
    assert result.usage == {"provider": "anthropic", "input_tokens": 10, "output_tokens": 20}
    with pytest.raises(ValueError):
        validate_verdict(result.output)


def test_provider_selection_does_not_silently_fall_back(monkeypatch):
    assert isinstance(make_model(Settings()), MockModel)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        make_model(Settings(model_mode="anthropic"))
    with pytest.raises(ValueError, match="not installed"):
        make_model(Settings(model_mode="codex", codex_command="/missing/event-review-codex"))
    with pytest.raises(ValueError, match="must be mock"):
        Settings(model_mode="typo")


@pytest.mark.parametrize("changes", [{"model_timeout_seconds": 0}, {"model_timeout_seconds": float("nan")},
                                    {"model_timeout_seconds": 601}, {"model_max_tokens": 0}, {"model_max_tokens": 8193}])
def test_invalid_provider_limits_fail_at_startup(changes):
    with pytest.raises(ValueError):
        replace(Settings(), **changes)


def codex_events(verdict=VERDICT):
    return "\n".join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "test"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(verdict)}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20}},
    ])


@pytest.mark.parametrize("events", [
    '{"type":"turn.failed"}',
    '{"type":"error","message":"private"}',
    '{"type":"item.completed","item":{"type":"command_execution"}}',
    '{"type":"thread.started"}',
    '{"type":"item.completed","item":{"type":"error","message":"notice"}}',
    '[]',
    'not json',
])
def test_codex_requires_a_completed_answer_without_tools(events):
    with pytest.raises(ModelFailure):
        parse_codex_output(events)


def test_codex_optional_feature_notice_does_not_hide_a_completed_verdict():
    notice = json.dumps({"type": "item.completed", "item": {"type": "error", "message": "Code Mode is unavailable because code-mode host is disabled."}})
    assert parse_codex_output(notice + "\n" + codex_events()).output == VERDICT


def fake_codex(tmp_path, body, monkeypatch, ready_file=None):
    script = tmp_path / "fake_codex.py"
    script.write_text(body)
    popen = providers.subprocess.Popen

    def start(command, **kwargs):
        # The container intentionally mounts /tmp noexec. Read the fixture with
        # Python; still exercise real process creation, pipes, and cancellation.
        process = popen([command[0], str(script), *command[1:]], **kwargs)
        if ready_file is not None:
            # Establish the descendant before testing its cleanup. Startup under
            # a half-CPU container quota can take longer than the short timeout.
            deadline = time.monotonic() + 5
            while not ready_file.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready_file.exists():
                providers.os.killpg(process.pid, providers.signal.SIGKILL)
                process.communicate()
                pytest.fail("The fake Codex process did not create its test child.")
        return process

    monkeypatch.setattr(providers.subprocess, "Popen", start)
    return sys.executable


def test_codex_process_has_only_context_and_no_application_credentials(tmp_path, monkeypatch, context):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-pass")
    monkeypatch.setenv("REVIEW_PGDATABASE", "do-not-pass")
    executable = fake_codex(tmp_path, '''import json, os, pathlib, sys
context = json.load(sys.stdin)
assert 'ANTHROPIC_API_KEY' not in os.environ and 'REVIEW_PGDATABASE' not in os.environ
assert '--ignore-user-config' in sys.argv and '--ephemeral' in sys.argv
assert sys.argv[sys.argv.index('--sandbox') + 1] == 'read-only'
assert 'shell_tool' in sys.argv and 'multi_agent' in sys.argv and 'plugins' in sys.argv
assert pathlib.Path.cwd().name.startswith('event-review-codex-')
assert 'event' in context and 'memory' in context
schema = json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
assert schema['additionalProperties'] is False
print(''' + repr(codex_events()) + ''')
''', monkeypatch)
    model = CodexModel(Settings(model_mode="codex", codex_command=executable))
    reply = model.review(context, timeout_seconds=2)
    assert reply.output == VERDICT
    assert reply.usage["input_tokens"] == 10


@pytest.mark.skipif(os.name != "posix", reason="Docker and hosted workers run on Linux")
def test_codex_timeout_kills_its_process_group(tmp_path, monkeypatch, context):
    child_pid = tmp_path / "child.pid"
    executable = fake_codex(tmp_path, f'''import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))
time.sleep(30)
''', monkeypatch, ready_file=child_pid)
    model = CodexModel(Settings(model_mode="codex", codex_command=executable))
    with pytest.raises(TimeoutError):
        model.review(context, timeout_seconds=0.3)
    pid = int(child_pid.read_text())
    process = Path(f"/proc/{pid}/stat")
    assert not process.exists() or process.read_text().split()[2] == "Z"
