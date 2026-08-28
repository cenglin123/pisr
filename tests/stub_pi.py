#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pisr e2e 测试桩：冒充 pi CLI 的事件流输出，供 stub 端到端集成测试使用。

行为由 STUB_MODE 环境变量控制（测试里 set/restore）：
  events （默认） 输出标准事件流 JSONL 后退出，STUB_EXIT 控制退出码（默认 0），
                 STUB_TEXT 控制最终文本（默认 STUB_OK），STUB_TOOLS 控制工具事件
  exit3           stdout 空，stderr 写失败信息，退出码 3（模拟 provider 失败）
  garbage         stdout 写非 JSON 垃圾行后退出 0（模拟 schema 漂移）
  sleep           静默挂起 STUB_SLEEP 秒（默认 300）后退出（模拟静默停滞，供 kill 测试）

绝不访问网络、绝不调用任何模型。
"""
import json
import os
import sys
import time

mode = os.environ.get("STUB_MODE", "events")

if mode == "exit3":
    sys.stderr.write("stub boom: simulated provider failure\n")
    sys.exit(3)

if mode == "garbage":
    sys.stdout.write("hello from stub\nnot json at all\n{\"type\":\"unknown_future_event\"}\n")
    sys.stdout.flush()
    sys.exit(0)

if mode == "sleep":
    time.sleep(int(os.environ.get("STUB_SLEEP", "300")))
    sys.exit(0)

# events 模式
text = os.environ.get("STUB_TEXT", "STUB_OK")
tools = [t for t in os.environ.get("STUB_TOOLS", "read").split(",") if t]

events = [
    {"type": "session", "version": 3, "id": "stub-session-0001",
     "timestamp": "2026-08-26T00:00:00Z", "cwd": os.getcwd()},
    {"type": "agent_start"},
]
for i, t in enumerate(tools):
    events.append({"type": "tool_execution_start", "toolCallId": f"tc{i}",
                   "toolName": t, "args": {}})
    events.append({"type": "tool_execution_end", "toolCallId": f"tc{i}",
                   "toolName": t, "result": "ok", "isError": False})
events.append({
    "type": "message_end",
    "message": {"role": "assistant",
                "content": [{"type": "text", "text": text}],
                "provider": "stub", "model": "stub-model",
                "usage": {"input": 11, "output": 7, "totalTokens": 18,
                          "cost": {"input": 0, "output": 0, "total": 0.0}},
                "stopReason": "stop"}})
events.append({"type": "agent_end", "messages": []})
events.append({"type": "agent_settled"})

for e in events:
    sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")
sys.stdout.flush()
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
