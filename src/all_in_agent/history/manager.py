from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..adapters.base import LLMAdapter
    from ..tools.registry import ToolResponse

COMPRESS_THRESHOLD_TOKENS = 14_000
KEEP_RECENT_TURNS = 12
KEEP_RECENT_TOOL_RESULTS = 3
SUMMARY_MAX_TOKENS = 1_200

_SUMMARY_PROMPT = (
    "Summarize the conversation history below into structured JSON with keys: "
    '"facts" (list of strings), "decisions" (list of strings), "open_threads" (list of strings). '
    "Be concise. Output only valid JSON.\n\nHistory:\n{history}"
)


def _estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            total += len(c) // 4
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    total += len(str(block.get("text", "") or block.get("content", ""))) // 4
    return total


@dataclass
class HistoryManager:
    max_context_tokens: int = 32_000
    _messages: list[dict] = field(default_factory=list)
    _summary: str = ""

    def add(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})

    def add_assistant_tool_calls(self, content: str | None, tool_calls: list) -> None:
        blocks = []
        if content:
            blocks.append({"type": "text", "text": content})
        for tc in tool_calls:
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
        self._messages.append({"role": "assistant", "content": blocks})

    def add_tool_result(self, tool_use_id: str, result: "ToolResponse") -> None:
        self._messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": result.content}],
            "_source": "tool_result",
        })

    def get_messages(self) -> list[dict]:
        msgs = self._build_context()
        while _estimate_tokens(msgs) > self.max_context_tokens and len(msgs) > 1:
            msgs = msgs[1:]
        # Truncate the sole remaining message if it still exceeds budget
        if msgs and _estimate_tokens(msgs) > self.max_context_tokens:
            msg = msgs[0]
            if isinstance(msg.get("content"), str):
                max_chars = self.max_context_tokens * 4
                msgs = [{**msg, "content": msg["content"][:max_chars]}]
        return msgs

    def needs_compression(self) -> bool:
        return _estimate_tokens(self._messages) > COMPRESS_THRESHOLD_TOKENS

    async def compress(self, llm: "LLMAdapter") -> None:
        if not self.needs_compression():
            return

        recent = self._split_recent()
        old_msgs = self._messages[: len(self._messages) - len(recent)]
        if not old_msgs:
            return

        history_text = "\n".join(
            f"{m['role']}: {m['content'] if isinstance(m['content'], str) else str(m['content'])}"
            for m in old_msgs
        )
        prompt = _SUMMARY_PROMPT.format(history=history_text)

        try:
            resp = await llm.generate([{"role": "user", "content": prompt}], max_tokens=512)
            summary_text = resp.content or ""
        except Exception:
            summary_text = "[compression failed]"

        if len(summary_text) // 4 > SUMMARY_MAX_TOKENS:
            summary_text = summary_text[: SUMMARY_MAX_TOKENS * 4]

        self._summary = summary_text
        self._messages = recent

    def _split_recent(self) -> list[dict]:
        tool_results = [m for m in self._messages if m.get("_source") == "tool_result"]
        keep_tools = set(id(m) for m in tool_results[-KEEP_RECENT_TOOL_RESULTS:])

        regular = [m for m in self._messages if m.get("_source") != "tool_result"]
        keep_regular = set(id(m) for m in regular[-KEEP_RECENT_TURNS:])

        return [m for m in self._messages if id(m) in keep_regular or id(m) in keep_tools]

    def _build_context(self) -> list[dict]:
        msgs = []
        if self._summary:
            msgs.append({"role": "user", "content": f"[Previous conversation summary]\n{self._summary}"})
            msgs.append({"role": "assistant", "content": "Understood."})
        msgs.extend({"role": m["role"], "content": m["content"]} for m in self._messages)
        return msgs
