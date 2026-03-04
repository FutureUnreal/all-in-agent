import time
from dataclasses import dataclass, field


class BudgetExceededError(Exception):
    def __init__(self, dimension: str, current: int, limit: int):
        self.dimension = dimension
        super().__init__(f"Budget exceeded: {dimension}={current}/{limit}")


class LoopDetectedError(Exception):
    def __init__(self, action_sig: str, count: int):
        super().__init__(f"Loop detected: '{action_sig}' repeated {count} times")


@dataclass
class Budget:
    max_llm_calls: int = 40
    max_tool_calls: int = 80
    max_wall_ms: int = 1_800_000
    max_input_tokens_per_call: int = 24_000
    max_output_tokens_per_call: int = 2_048
    loop_same_action_limit: int = 3


@dataclass
class Run:
    run_id: str
    goal: str
    budget: Budget = field(default_factory=Budget)
    created_at: str = ""
    llm_calls: int = 0
    tool_calls: int = 0
    _start_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    _last_sig: str = field(default="")
    _consecutive_count: int = field(default=0)

    def check_budget(self, action_type: str, action_sig: str = "") -> None:
        elapsed = int(time.time() * 1000) - self._start_ms
        if elapsed >= self.budget.max_wall_ms:
            raise BudgetExceededError("wall_ms", elapsed, self.budget.max_wall_ms)

        if action_type == "llm_call":
            if self.llm_calls >= self.budget.max_llm_calls:
                raise BudgetExceededError("llm_calls", self.llm_calls, self.budget.max_llm_calls)
            self.llm_calls += 1

        elif action_type == "tool_call":
            if self.tool_calls >= self.budget.max_tool_calls:
                raise BudgetExceededError("tool_calls", self.tool_calls, self.budget.max_tool_calls)
            self.tool_calls += 1

            if action_sig:
                sig_key = action_sig[:128]
                if sig_key == self._last_sig:
                    self._consecutive_count += 1
                else:
                    self._last_sig = sig_key
                    self._consecutive_count = 1
                if self._consecutive_count >= self.budget.loop_same_action_limit:
                    raise LoopDetectedError(action_sig, self._consecutive_count)
