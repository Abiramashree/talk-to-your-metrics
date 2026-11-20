import json


class FakeLLM:
    """Replays scripted assistant messages; records what it was sent. No network."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def chat(self, messages, tools=None, tool_choice=None):
        self.sent.append({"messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice})
        self.usage["requests"] += 1
        return self.script.pop(0)


def call(name, args, id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def final(obj):
    return {"role": "assistant", "content": json.dumps(obj)}
