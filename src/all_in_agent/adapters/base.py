from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"


class ConfigError(Exception):
    pass


class LLMError(Exception):
    def __init__(self, message: str, error_class: str, attempts: int = 1):
        self.error_class = error_class
        self.attempts = attempts
        super().__init__(message)


class LLMAdapter(ABC):
    model_id: str
    max_context_tokens: int

    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str = "",
        max_tokens: int = 2048,
    ) -> LLMResponse: ...
