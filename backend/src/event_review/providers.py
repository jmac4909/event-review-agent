"""Provider adapters. The worker owns claims, retries, validation, and persistence."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory

from anthropic import AsyncAnthropic, APIConnectionError, APIStatusError

from .models import GuidanceDraft, ModelReply, ReviewResponse
from .reviewer import MockModel
from .settings import Settings


class ModelFailure(RuntimeError):
    """Only fixed, credential-free messages from this module reach the UI."""


def usage_counts(provider: str, raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    fields = ("input_tokens", "output_tokens")
    if provider == "anthropic":
        fields += ("cache_read_input_tokens", "cache_creation_input_tokens")
    elif provider == "codex":
        fields += ("cached_input_tokens", "reasoning_output_tokens")
    # No arbitrary response fields, and no invented zero for missing counts.
    # Reject booleans and counts outside the browser's exact integer range.
    counts = {key: raw[key] for key in fields
              if type(raw.get(key)) is int and 0 <= raw[key] <= 2**53 - 1}
    return {"provider": provider, **counts} if counts else None


def output_schema(response_type=ReviewResponse) -> dict:
    schema = response_type.model_json_schema()
    # Provider schemas describe shape; our Pydantic validation enforces bounds.
    def clean(node):
        if isinstance(node, dict):
            for key in ("default", "minLength", "maxLength", "minimum", "maximum"):
                node.pop(key, None)
            if node.get("type") == "object":
                node["required"] = list(node["properties"])
            for child in node.values():
                clean(child)
        elif isinstance(node, list):
            for child in node:
                clean(child)
    clean(schema)
    return schema


class AnthropicModel:
    def __init__(self, settings: Settings):
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise ValueError("Set ANTHROPIC_API_KEY in the worker environment for Anthropic mode.")
        self.name = f"anthropic/{settings.anthropic_model}"
        self.model = settings.anthropic_model
        self.max_tokens = settings.model_max_tokens

    def review(self, context: dict, timeout_seconds: float = 90) -> ModelReply:
        return asyncio.run(self._generate(context, timeout_seconds, ReviewResponse))

    def draft(self, context: dict, timeout_seconds: float = 90) -> ModelReply:
        return asyncio.run(self._generate(context, timeout_seconds, GuidanceDraft))

    async def _generate(self, context: dict, timeout_seconds: float, response_type) -> ModelReply:
        try:
            # The outer deadline cancels the whole request, including a slow body.
            async with asyncio.timeout(timeout_seconds):
                async with AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0,
                                          timeout=timeout_seconds) as client:
                    response = await client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=context["instructions"],
                        messages=[{"role": "user", "content": json.dumps(
                            {key: value for key, value in context.items() if key != "instructions"}, ensure_ascii=False)}],
                        output_config={"format": {"type": "json_schema", "schema": output_schema(response_type)}},
                    )
        except APIStatusError as error:
            if error.status_code in (401, 403):
                raise ModelFailure("Model access was denied. Check the worker's API key and model access.") from None
            if error.status_code == 429:
                raise ModelFailure("The model rate limit or quota was reached.") from None
            raise ModelFailure("The model service rejected the request.") from None
        except APIConnectionError:
            raise ModelFailure("The model service could not be reached.") from None
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = usage_counts("anthropic", response.usage.model_dump() if response.usage else None)
        if response.stop_reason != "end_turn":
            # Preserve refusals/truncated output for inspection, but never accept it.
            return ModelReply({"stop_reason": response.stop_reason, "raw_output": text}, usage)
        try:
            output = json.loads(text)
        except json.JSONDecodeError:
            output = {"raw_output": text}
        return ModelReply(output, usage)


class CodexModel:
    def __init__(self, settings: Settings):
        command = shutil.which(settings.codex_command)
        if command is None:
            raise ValueError("Codex CLI is not installed. Install it and run codex login before starting this worker.")
        self.command = command
        self.model = settings.codex_model
        self.name = f"codex/{self.model or 'cli-default'}"

    def review(self, context: dict, timeout_seconds: float = 90) -> ModelReply:
        return self._generate(context, timeout_seconds, ReviewResponse)

    def draft(self, context: dict, timeout_seconds: float = 90) -> ModelReply:
        return self._generate(context, timeout_seconds, GuidanceDraft)

    def _generate(self, context: dict, timeout_seconds: float, response_type) -> ModelReply:
        # Keep personal config, repository instructions, tools, and saved threads
        # out of this application call. Codex still supplies its internal prompt.
        with TemporaryDirectory(prefix="event-review-codex-") as directory:
            schema_file = Path(directory) / "response.json"
            schema_file.write_text(json.dumps(output_schema(response_type)))
            command = [self.command, "exec", "--ignore-user-config", "--ignore-rules",
                       "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                       "--json", "--color", "never", "--output-schema", str(schema_file),
                       "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',
                       "-c", "mcp_servers={}", "-c", 'model_reasoning_effort="low"']
            for feature in ("shell_tool", "apps", "plugins", "multi_agent", "browser_use",
                            "computer_use", "image_generation", "view_image", "memories",
                            "hooks", "skill_search", "code_mode", "code_mode_host", "goals", "sleep_tool"):
                command.extend(["--disable", feature])
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")
            # Do not pass database, API-key, or deployment credentials to Codex.
            environment = {key: value for key, value in os.environ.items()
                           if key in ("PATH", "HOME", "CODEX_HOME", "TMPDIR", "SYSTEMROOT",
                                      "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")}
            process = subprocess.Popen(command, cwd=directory, env=environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                       start_new_session=os.name == "posix")
            try:
                output, _ = process.communicate(json.dumps(context, ensure_ascii=False), timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.communicate()
                raise TimeoutError("Codex call timed out.") from None
            if process.returncode != 0:
                raise ModelFailure("Codex could not complete the request. Check its login, model access, and usage limits.")
            return parse_codex_output(output)


def parse_codex_output(output: str) -> ModelReply:
    if len(output) > 1_000_000:
        raise ModelFailure("Codex returned more output than this review allows.")
    completed = False
    answer = ""
    usage = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ModelFailure("Codex returned an unreadable response.") from None
        if not isinstance(event, dict):
            raise ModelFailure("Codex returned an unreadable response.")
        if event.get("type") in ("error", "turn.failed"):
            raise ModelFailure("Codex did not finish the review.")
        if event.get("type") in ("item.started", "item.completed"):
            item = event.get("item", {})
            if item.get("type") == "error":
                # Disabled optional features can emit notices. A completed turn
                # and valid answer are still required; fatal events fail above.
                continue
            if item.get("type") not in ("agent_message", "reasoning"):
                raise ModelFailure("Codex attempted an internal tool. Lookups must go through the application's review loop.")
            if item.get("type") == "agent_message" and event["type"] == "item.completed":
                answer = item.get("text", "")
        if event.get("type") == "turn.completed":
            completed = True
            usage = usage_counts("codex", event.get("usage"))
    if not completed or not answer:
        raise ModelFailure("Codex did not return a completed verdict.")
    try:
        result = json.loads(answer)
    except json.JSONDecodeError:
        result = {"raw_output": answer}
    return ModelReply(result, usage)


def make_model(settings: Settings, mode: str | None = None):
    mode = mode or settings.default_reviewer
    if mode == "mock":
        return MockModel()
    if mode == "anthropic":
        return AnthropicModel(settings)
    if mode == "codex":
        return CodexModel(settings)
    raise ValueError("Unknown model provider.")
