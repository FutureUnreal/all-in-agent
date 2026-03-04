from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

from ..core.run import Run

MAX_CONTENT_LEN = 200_000


class SideEffectLevel(str, Enum):
    READ_ONLY = "read_only"
    WRITES_LOCAL = "writes_local"
    NETWORK = "network"
    DANGEROUS = "dangerous"


@dataclass
class ToolResponse:
    status: str  # "success" | "error"
    content: str
    error_class: str | None = None


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    side_effect_level: SideEffectLevel
    execute: Callable  # async (args: dict, run: Run) -> ToolResponse


async def _default_approve(name: str, args: dict) -> bool:
    return True


class ToolRegistry:
    def __init__(self, approval_callback: Callable[[str, dict], Awaitable[bool]] | None = None):
        self._tools: dict[str, Tool] = {}
        self._approval_callback = approval_callback or _default_approve

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get_schemas(self) -> list[dict]:
        return [_to_schema(t) for t in self._tools.values()]

    async def execute(self, name: str, args: dict, run: Run) -> ToolResponse:
        tool = self._tools.get(name)
        if not tool:
            return ToolResponse("error", f"Unknown tool: {name}", "NOT_FOUND")

        if _HAS_JSONSCHEMA:
            try:
                jsonschema.validate(args, tool.input_schema)
            except jsonschema.ValidationError as e:
                return ToolResponse("error", str(e.message), "VALIDATION")

        if tool.side_effect_level == SideEffectLevel.DANGEROUS:
            try:
                approved = await self._approval_callback(name, args)
            except Exception:
                approved = False
            if not approved:
                return ToolResponse("error", f"Tool '{name}' denied by approval", "POLICY_BLOCKED")

        try:
            result = await tool.execute(args, run)
            if len(result.content) > MAX_CONTENT_LEN:
                result = ToolResponse(
                    result.status,
                    result.content[:MAX_CONTENT_LEN] + "[TRUNCATED]",
                    result.error_class,
                )
            return result
        except Exception as e:
            return ToolResponse("error", str(e), "INTERNAL_BUG")


def _to_schema(tool: Tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }
