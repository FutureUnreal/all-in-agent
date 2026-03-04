import asyncio
import subprocess
from pathlib import Path

from .registry import SideEffectLevel, Tool, ToolResponse

_MAX_FILE_READ = 100 * 1024  # 100 KB


async def _read_file_impl(args: dict, run) -> ToolResponse:
    try:
        path = Path(args["path"])
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > _MAX_FILE_READ:
            content = content[:_MAX_FILE_READ] + "\n[TRUNCATED]"
        return ToolResponse("success", content)
    except FileNotFoundError:
        return ToolResponse("error", f"File not found: {args['path']}", "NOT_FOUND")
    except Exception as e:
        return ToolResponse("error", str(e), "INTERNAL_BUG")


async def _write_file_impl(args: dict, run) -> ToolResponse:
    try:
        path = Path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return ToolResponse("success", f"Written {len(args['content'])} bytes to {args['path']}")
    except Exception as e:
        return ToolResponse("error", str(e), "INTERNAL_BUG")


async def _bash_impl(args: dict, run) -> ToolResponse:
    timeout = int(args.get("timeout", 30))
    try:
        proc = await asyncio.create_subprocess_shell(
            args["command"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResponse("error", f"Command timed out after {timeout}s", "TIMEOUT")

        output = (stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")).strip()
        status = "success" if proc.returncode == 0 else "error"
        return ToolResponse(status, output or f"(exit code {proc.returncode})")
    except Exception as e:
        return ToolResponse("error", str(e), "INTERNAL_BUG")


read_file = Tool(
    name="read_file",
    description="Read a file and return its contents.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path to read"}},
        "required": ["path"],
    },
    side_effect_level=SideEffectLevel.READ_ONLY,
    execute=_read_file_impl,
)

write_file = Tool(
    name="write_file",
    description="Write content to a file (creates parent directories as needed).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
    side_effect_level=SideEffectLevel.WRITES_LOCAL,
    execute=_write_file_impl,
)

bash = Tool(
    name="bash",
    description="Execute a shell command and return stdout+stderr.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
        },
        "required": ["command"],
    },
    side_effect_level=SideEffectLevel.DANGEROUS,
    execute=_bash_impl,
)

BUILTIN_TOOLS = [read_file, write_file, bash]
