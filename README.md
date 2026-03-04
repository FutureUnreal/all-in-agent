[English](README.md) | [中文](README_zh.md)

<p align="center">
  <h1 align="center">all-in-agent</h1>
</p>

<p align="center">
  A minimal, universal agent framework for Python. Zero mandatory dependencies.
</p>

<p align="center">
  <a href="https://pypi.org/project/all-in-agent/"><img src="https://img.shields.io/pypi/v/all-in-agent" alt="PyPI version"></a>
  <a href="https://pypi.org/project/all-in-agent/"><img src="https://img.shields.io/pypi/pyversions/all-in-agent" alt="Python versions"></a>
  <a href="https://pypi.org/project/all-in-agent/"><img src="https://img.shields.io/pypi/l/all-in-agent" alt="License"></a>
  <a href="https://github.com/FutureUnreal/all-in-agent"><img src="https://img.shields.io/github/stars/FutureUnreal/all-in-agent?style=flat" alt="GitHub Stars"></a>
</p>

```bash
pip install all-in-agent
pip install "all-in-agent[openai]"      # OpenAI GPT
pip install "all-in-agent[anthropic]"   # Anthropic Claude
pip install "all-in-agent[all]"         # all optional deps
```

## Why all-in-agent

- 🪶 **Zero dependencies** — pure stdlib core; adapters are opt-in extras
- 🔌 **Pluggable everything** — swap LLM adapter, tools, history, or orchestration without touching other parts
- 🔍 **Transparent by default** — append-only NDJSON event log; every run is replayable
- 🛡️ **Safe by default** — dangerous tools require explicit approval; budget stops runaway agents

## Quick Start

```bash
pip install "all-in-agent[openai]"      # or [anthropic]
```

```python
from all_in_agent import Agent, OpenAIAdapter, ToolRegistry, BUILTIN_TOOLS

llm = OpenAIAdapter()                 # reads OPENAI_API_KEY from env
tools = ToolRegistry()
for t in BUILTIN_TOOLS:               # read_file, write_file, bash
    tools.register(t)

agent = Agent(llm=llm, tools=tools)
result = agent.run_sync("Summarize README.md in three bullet points")
print(result["final_answer"])
```

> **Jupyter Notebook or async framework?** Use `await agent.run(goal)` directly.

## Core Concepts

### Node / Flow

Everything is a node. A flow is a graph of nodes.

```python
from all_in_agent import BaseNode, Flow

class MyNode(BaseNode):
    async def prep(self, shared: dict):
        return shared["input"]

    async def exec(self, prep_result):
        return prep_result.upper()

    async def post(self, shared: dict, exec_result) -> str:
        shared["output"] = exec_result
        return "default"   # action name → next node

node_a = MyNode()
node_b = MyNode()
node_a >> node_b           # default edge
# or: (node_a - "custom_action") >> node_b

flow = Flow()
await flow.run(shared={}, start=node_a)
```

**State contract**: all inter-node state lives in `shared` dict. Node instance fields hold only configuration.

### Budget & Loop Detection

```python
from all_in_agent import Budget

budget = Budget(
    max_llm_calls=40,
    max_tool_calls=80,
    max_wall_ms=1_800_000,       # 30 min wall-clock limit
    loop_same_action_limit=3,    # raise LoopDetectedError after 3 consecutive identical tool calls
)

agent = Agent(llm=llm, tools=tools, budget=budget)
```

### Tool Registry

```python
from all_in_agent import Tool, ToolRegistry, SideEffectLevel, ToolResponse

async def my_tool(args: dict, run) -> ToolResponse:
    result = do_something(args["input"])
    return ToolResponse(status="success", content=result)

registry = ToolRegistry(
    approval_callback=my_approval_fn   # async (name, args) -> bool
)
registry.register(Tool(
    name="my_tool",
    description="Does something useful",
    input_schema={
        "type": "object",
        "properties": {"input": {"type": "string"}},
        "required": ["input"],
    },
    side_effect_level=SideEffectLevel.READ_ONLY,
    execute=my_tool,
))
```

`DANGEROUS` tools call `approval_callback` before executing. Install `jsonschema` for automatic argument validation.

### History & Compression

`HistoryManager` compresses conversation history when it exceeds `COMPRESS_THRESHOLD_TOKENS` (14 000 tokens). It keeps the 12 most recent turns and the 3 most recent tool results verbatim, then asks the LLM to summarize everything older into structured JSON (facts / decisions / open_threads).

### Event Store

Every run writes an append-only NDJSON log to `./runs/<run_id>/events.ndjson`:

```
{"run_id": "...", "event": "RUN_CREATED", "data": {...}, "ts": "..."}
{"run_id": "...", "event": "ASSISTANT_MESSAGE", "data": {...}, "ts": "..."}
{"run_id": "...", "event": "TOOL_RESULT", "data": {...}, "ts": "..."}
{"run_id": "...", "event": "RUN_STOPPED", "data": {"reason": "goal_met"}, "ts": "..."}
```

### Multi-Agent

```python
from all_in_agent import MessageBus, TaskManager, MessageEnvelope, Task

bus = MessageBus(run_dir="./runs/session_1")
tm  = TaskManager(run_dir="./runs/session_1")

# coordinator creates tasks
task = await tm.create_task(goal="Analyze file X")

# worker claims and runs
available = await tm.get_available(agent_id="worker_1")
claimed   = await tm.claim_task(available[0].task_id, "worker_1")

# agents communicate
await bus.send(MessageEnvelope(
    msg_id="...", run_id="...",
    from_agent="worker_1", to_agent="coordinator",
    msg_type="TASK_DONE", payload={"result": "..."}, ts="...",
))
```

`TaskManager` uses file-based locking (`fcntl` on Unix, `.lock` file on Windows) for safe concurrent access. Tasks support dependency chains via `dependencies: list[str]`.

## LLM Adapters

| Adapter | Install extra | Environment variable |
|---------|--------------|---------------------|
| `OpenAIAdapter`    | `all-in-agent[openai]`    | `OPENAI_API_KEY`    |
| `AnthropicAdapter` | `all-in-agent[anthropic]` | `ANTHROPIC_API_KEY` |

Both adapters retry on transient errors with exponential backoff + jitter.

```python
from all_in_agent import OpenAIAdapter, AnthropicAdapter

llm = OpenAIAdapter(model="gpt-4o-mini", max_retries=3)
llm = AnthropicAdapter(model="claude-sonnet-4-6", max_retries=3)
```

## Architecture

<details>
<summary>📁 Directory Structure</summary>

```
all_in_agent/
├── core/
│   ├── node.py      BaseNode · Node · BatchNode
│   ├── flow.py      Flow (graph runner)
│   └── run.py       Run · Budget · BudgetExceededError · LoopDetectedError
├── adapters/
│   ├── base.py      LLMAdapter · LLMResponse · ToolCall · LLMError · ConfigError
│   ├── anthropic.py AnthropicAdapter (exponential backoff, retry)
│   └── openai.py    OpenAIAdapter
├── tools/
│   ├── registry.py  ToolRegistry (approval callbacks, jsonschema validation)
│   └── builtin.py   read_file · write_file · bash
├── history/
│   ├── manager.py   HistoryManager (LLM-based compression)
│   └── store.py     FileEventStore (append-only NDJSON)
└── agents/
    ├── base.py      Agent · ReActNode · LLMCallNode · ToolDispatchNode
    └── multi.py     MessageBus · TaskManager · MessageEnvelope · Task · TaskStatus
```

</details>

## Package Naming

The PyPI package is `all-in-agent`, but the Python import name is `all_in_agent`:

```bash
pip install all-in-agent
```

```python
from all_in_agent import Agent   # Python import name is 'all_in_agent'
```

The hyphen in the PyPI name can't be used in Python imports, so the module name uses underscores.

## Design Goals

- **Zero mandatory deps** — pure stdlib core; adapters opt-in
- **Small** — ~120 LOC core loop, readable in one sitting
- **Composable** — every piece (Node, Tool, Adapter, History) is replaceable
- **Safe by default** — dangerous tools require approval; budget stops runaway agents

## Requirements

Python 3.11+

Optional: `anthropic`, `openai`, `jsonschema`

## License

MIT
