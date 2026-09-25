import json
import re

from jev.client import JevError, run_jev

from ..crypto import decrypt
from ..models import LLMProvider, LLMProviderConfig
from ..tracing import trace_generation
from ..validators import validate_llm_base_url


class LLMError(Exception):
    """retryable=False for errors a retry can't fix (bad key, bad model, bad request)."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def parse_json(text: str) -> dict:
    """Models often wrap JSON in prose or ``` fences; take the first {...} object."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise LLMError(f"model did not return JSON: {text[:200]}", retryable=True)
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned invalid JSON: {exc}", retryable=True) from exc


class ChatClient:
    """Provider-agnostic chat: every step and the agent loop speak JSON over plain chat,
    so any provider that can follow instructions works (no provider-specific tool use)."""

    def __init__(self, config: LLMProviderConfig):
        self.config = config
        self.api_key = decrypt(config.api_key_encrypted) if config.api_key_encrypted else ""
        # Reasoning models count thinking against this budget, so keep it generous.
        self.max_tokens = int(config.extra_config.get("max_tokens", 16000))
        # Only sent when set: newer models reject any non-default temperature.
        self.temperature = config.extra_config.get("temperature")

    def _optional(self) -> dict:
        return {} if self.temperature is None else {"temperature": self.temperature}

    def chat(self, system: str, messages: list[dict], name: str = "chat") -> str:
        with trace_generation(name, model=self.config.model, input=messages) as generation:
            text, usage = self._chat(system, messages)
            generation.update(output=text, usage_details=usage)
        return text

    def complete_json(self, system: str, prompt: str, name: str = "complete") -> dict:
        return parse_json(self.chat(system, [{"role": "user", "content": prompt}], name=name))

    def _chat(self, system: str, messages: list[dict]) -> tuple[str, dict]:
        raise NotImplementedError


class AnthropicClient(ChatClient):
    def _chat(self, system, messages):
        import anthropic

        if self.config.base_url:
            validate_llm_base_url(self.config.base_url)
        client = anthropic.Anthropic(api_key=self.api_key, base_url=self.config.base_url or None)
        try:
            resp = client.messages.create(
                model=self.config.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                **self._optional(),
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, anthropic.BadRequestError) as exc:
            raise LLMError(str(exc), retryable=False) from exc
        except anthropic.APIError as exc:
            raise LLMError(str(exc)) from exc
        text = "".join(block.text for block in resp.content if block.type == "text")
        usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
        return text, usage


class OpenAICompatibleClient(ChatClient):
    """OpenAI, and any self-hosted server speaking the OpenAI chat API (vLLM, Ollama, ...)."""

    def _chat(self, system, messages):
        import openai

        if self.config.base_url:
            validate_llm_base_url(self.config.base_url)  # re-check at call time (DNS can change)
        client = openai.OpenAI(
            api_key=self.api_key or "not-needed", base_url=self.config.base_url or None
        )
        # OpenAI's GPT-5/o-series reject max_tokens; self-hosted servers expect it.
        limit_key = (
            "max_completion_tokens" if self.config.provider == LLMProvider.OPENAI else "max_tokens"
        )
        try:
            resp = client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "system", "content": system}, *messages],
                **{limit_key: self.max_tokens},
                **self._optional(),
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError,
                openai.NotFoundError, openai.BadRequestError) as exc:
            raise LLMError(str(exc), retryable=False) from exc
        except openai.APIError as exc:
            raise LLMError(str(exc)) from exc
        text = resp.choices[0].message.content or ""
        usage = {}
        if resp.usage:
            usage = {"input": resp.usage.prompt_tokens, "output": resp.usage.completion_tokens}
        return text, usage


class JevClient:
    """Jev only answers structured questions (choice/score), not free chat — so it can
    serve the classification-style steps and nothing else (see JEV_STEPS)."""

    def __init__(self, config: LLMProviderConfig):
        self.config = config

    def choose(self, state: str, question: str, options: dict[str, str], name: str = "jev") -> str:
        questions = {"answer": {"type": "choice", "instructions": question, "criteria": options}}
        with trace_generation(name, model=self.config.model or "jev", input=questions) as generation:
            try:
                result = run_jev(state, questions)
            except JevError as exc:
                retryable = exc.status_code >= 500 or exc.status_code == 429
                raise LLMError(str(exc), retryable=retryable) from exc
            generation.update(output=result.get("answers"))
        return result["answers"]["answer"]["choice"]


def client_for(config: LLMProviderConfig):
    if config.provider == LLMProvider.ANTHROPIC:
        return AnthropicClient(config)
    if config.provider in (LLMProvider.OPENAI, LLMProvider.SELF_HOSTED):
        return OpenAICompatibleClient(config)
    if config.provider == LLMProvider.JEV_CLOUDFLARE:
        return JevClient(config)
    raise LLMError(f"unknown provider {config.provider}", retryable=False)
