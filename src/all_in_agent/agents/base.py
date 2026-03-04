from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from ..core.flow import Flow
from ..core.node import BaseNode
from ..core.run import BudgetExceededError, Budget, LoopDetectedError, Run
from ..history.manager import HistoryManager
from ..history.store import FileEventStore
from ..utils import make_ulid as _make_ulid, iso_now as _iso_now

if TYPE_CHECKING:
    from ..adapters.base import LLMAdapter, LLMResponse
    from ..tools.registry import ToolRegistry


# Deprecated: use LLMCallNode + ToolDispatchNode
class ReActNode(BaseNode):
    async def exec(self, prep: dict) -> "LLMResponse":
        run: Run = prep["run"]
        run.check_budget("llm_call")
        return await prep["llm"].generate(
            messages=prep["messages"],
            tools=prep["tools"],
            system=prep["system"],
        )

    async def prep(self, shared: dict) -> dict:
        return {
            "messages": shared["history"].get_messages(),
            "tools": shared["tools"].get_schemas(),
            "run": shared["run"],
            "llm": shared["llm"],
            "system": shared.get("system", ""),
        }

    async def post(self, shared: dict, resp: "LLMResponse") -> str:
        run: Run = shared["run"]
        store: FileEventStore = shared.get("store")
        history: HistoryManager = shared["history"]
        tools: "ToolRegistry" = shared["tools"]

        if store:
            await store.append(run.run_id, "ASSISTANT_MESSAGE", {
                "content": resp.content,
                "tool_calls": [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in resp.tool_calls],
                "stop_reason": resp.stop_reason,
            })

        if resp.stop_reason == "end_turn" or not resp.tool_calls:
            shared["final_answer"] = resp.content or ""
            return "done"

        history.add("assistant", resp.content or "")

        for tc in resp.tool_calls:
            run.check_budget("tool_call", tc.name)
            result = await tools.execute(tc.name, tc.args, run)

            if store:
                await store.append(run.run_id, "TOOL_RESULT", {
                    "tool_use_id": tc.id,
                    "name": tc.name,
                    "status": result.status,
                    "content": result.content[:500],
                })

            history.add_tool_result(tc.id, result)

        if history.needs_compression():
            await history.compress(shared["llm"])
            if store:
                await store.append(run.run_id, "MEMORY_UPDATED", {"summary": history._summary[:200]})

        return "continue"


class LLMCallNode(BaseNode):
    async def prep(self, shared: dict) -> dict:
        return {
            "messages": shared["history"].get_messages(),
            "tools": shared["tools"].get_schemas(),
            "run": shared["run"],
            "llm": shared["llm"],
            "system": shared.get("system", ""),
        }

    async def exec(self, prep: dict) -> "LLMResponse":
        run: Run = prep["run"]
        run.check_budget("llm_call")
        return await prep["llm"].generate(
            messages=prep["messages"],
            tools=prep["tools"],
            system=prep["system"],
        )

    async def post(self, shared: dict, resp: "LLMResponse") -> str:
        run: Run = shared["run"]
        store: FileEventStore = shared.get("store")
        history: HistoryManager = shared["history"]

        if store:
            await store.append(run.run_id, "ASSISTANT_MESSAGE", {
                "content": resp.content,
                "tool_calls": [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in resp.tool_calls],
                "stop_reason": resp.stop_reason,
            })

        if resp.stop_reason == "end_turn" or not resp.tool_calls:
            shared["final_answer"] = resp.content or ""
            return "done"

        # Write into shared (HC-5: Flow.copy causes node instance fields to not persist)
        shared["llm_response"] = resp
        history.add_assistant_tool_calls(resp.content or "" if resp.content else None, resp.tool_calls)
        return "dispatch_tools"


class ToolDispatchNode(BaseNode):
    async def prep(self, shared: dict) -> "LLMResponse | None":
        return shared.get("llm_response")

    async def exec(self, resp: "LLMResponse | None") -> None:
        # exec cannot access shared; tool execution must happen in post
        return resp

    async def post(self, shared: dict, resp: "LLMResponse | None") -> str:
        if resp is None:
            shared["final_answer"] = shared.get("final_answer", "")
            return "done"

        run: Run = shared["run"]
        store: FileEventStore = shared.get("store")
        history: HistoryManager = shared["history"]
        tools: "ToolRegistry" = shared["tools"]

        for tc in resp.tool_calls:
            run.check_budget("tool_call", tc.name)
            result = await tools.execute(tc.name, tc.args, run)

            if store:
                await store.append(run.run_id, "TOOL_RESULT", {
                    "tool_use_id": tc.id,
                    "name": tc.name,
                    "status": result.status,
                    "content": result.content[:500],
                })

            history.add_tool_result(tc.id, result)

        if history.needs_compression():
            await history.compress(shared["llm"])
            if store:
                await store.append(run.run_id, "MEMORY_UPDATED", {"summary": history._summary[:200]})

        return "continue"


class Agent:
    def __init__(
        self,
        llm: "LLMAdapter",
        tools: "ToolRegistry",
        budget: Budget | None = None,
        run_dir: str = "./runs",
        system: str = "",
    ):
        self._llm = llm
        self._tools = tools
        self._budget = budget or Budget()
        self._run_dir = run_dir
        self._system = system
        self._flow = Flow()

        # New decomposed nodes
        self._llm_node = LLMCallNode()
        self._tool_node = ToolDispatchNode()
        self._llm_node - "dispatch_tools" >> self._tool_node
        self._tool_node - "continue" >> self._llm_node

        self._node = ReActNode()
        self._node - "continue" >> self._node  # loop back

    async def run(self, goal: str) -> dict:
        run = Run(run_id=_make_ulid(), goal=goal, budget=self._budget, created_at=_iso_now())
        store = FileEventStore(self._run_dir)
        history = HistoryManager(max_context_tokens=self._llm.max_context_tokens)

        await store.append(run.run_id, "RUN_CREATED", {"goal": goal})
        history.add("user", goal)

        shared: dict = {
            "run": run,
            "llm": self._llm,
            "tools": self._tools,
            "history": history,
            "store": store,
            "system": self._system,
            "final_answer": "",
        }

        try:
            await self._flow.run(shared, self._llm_node)
        except (BudgetExceededError, LoopDetectedError) as e:
            shared["final_answer"] = shared.get("final_answer") or f"[stopped: {e}]"
            await store.append(run.run_id, "RUN_STOPPED", {"reason": str(e)})
        else:
            await store.append(run.run_id, "RUN_STOPPED", {"reason": "goal_met"})
        return shared

    def run_sync(self, goal: str) -> dict:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError(
                "A running event loop was detected (e.g. Jupyter Notebook or an async framework). "
                "Use `await agent.run(goal)` instead of `agent.run_sync(goal)`."
            )
        return asyncio.run(self.run(goal))
