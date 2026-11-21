"""The agent: an LLM that answers questions only through our MCP server.

    python -m ttym.agent "What was revenue in the first quarter of 2018?"
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer

from ttym.core import TODAY
from ttym.llm import LLM, load_env

MAX_TOOL_CALLS = 8

SYSTEM = f"""\
You answer questions about the Olist marketplace (orders 2016-09 to 2018-10) using the tools. Today is {TODAY}.
Use list_metrics, get_dimensions and query_metrics. Never compute or estimate a number yourself; copy numbers from
query results. If no metric can answer the question, do not substitute a different measure: say it cannot be answered.

When you are done, reply with JSON only, no other text:
{{"answer": "<one short sentence>", "columns": [...], "rows": [[...]]}}  with columns and rows copied from the query result
{{"cannot_answer": "<one short sentence: why>"}}  when the data cannot answer the question"""


def _inline_refs(schema: dict) -> dict:
    """Inline $ref/$defs so the tool schema is self-contained for the model API."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def _final(content: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", (content or "").strip(), flags=re.M).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and ("cannot_answer" in value or ("rows" in value and "columns" in value)):
        return value
    return None


async def ask(question: str, server: MCPServer, llm: LLM, system: str = SYSTEM) -> dict:
    """Answer one question. Returns the final answer, every tool call, and token usage."""
    try:
        return await _ask(question, server, llm, system)
    except BaseExceptionGroup as group:
        # The MCP client's task group wraps errors raised inside it; surface the model error itself
        # so callers can handle it (quota, network) instead of seeing an opaque group.
        leaf = _first_leaf(group)
        raise leaf from None


def _first_leaf(group: BaseExceptionGroup) -> BaseException:
    error: BaseException = group
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


async def _ask(question: str, server: MCPServer, llm: LLM, system: str) -> dict:
    trace: list[dict] = []
    async with Client(server) as client:
        tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description or "",
                                              "parameters": _inline_refs(t.input_schema)}}
            for t in (await client.list_tools()).tools
        ]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": question}]
        reminded = False
        while True:
            calls_left = MAX_TOOL_CALLS - len(trace)
            # gpt-oss tends to answer from its reasoning without calling tools unless the first turn requires one.
            message = await llm.chat(messages, tools if calls_left > 0 else None, "required" if not trace else "auto")
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                final = _final(message.get("content", ""))
                if final is not None:
                    return {"final": final, "trace": trace, "usage": dict(llm.usage)}
                if reminded or calls_left <= 0:
                    return {"final": None, "error": "no valid final answer", "content": message.get("content"),
                            "trace": trace, "usage": dict(llm.usage)}
                reminded = True
                messages += [{"role": "assistant", "content": message.get("content") or ""},
                             {"role": "user", "content": "Reply with the final JSON only, in the required format."}]
                continue
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls})
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args, result = {}, {"error": "arguments were not valid JSON"}
                else:
                    if len(trace) >= MAX_TOOL_CALLS:
                        result = {"error": f"tool call limit ({MAX_TOOL_CALLS}) reached; give your final answer"}
                    else:
                        r = await client.call_tool(name, args)
                        result = {"error": r.content[0].text} if r.is_error else r.structured_content
                trace.append({"tool": name, "arguments": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})


def main() -> None:
    import pathlib

    from ttym.core import SemanticLayer
    from ttym.mcp_server import build_server

    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--bare", action="store_true")
    args = parser.parse_args()
    load_env(pathlib.Path(__file__).resolve().parents[2] / ".env")
    out = asyncio.run(ask(args.question, build_server(SemanticLayer(bare=args.bare)), LLM()))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
