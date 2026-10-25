"""Minimal, non-sensitive compatibility probe for a user-supplied endpoint."""
import json
import os
import urllib.error
import urllib.request

URL = "http://20.222.143.1:1030/v1/chat/completions"
KEY = os.getenv("EVAL_QWEN_API_KEY", "eval-probe")


def call(name, body):
    req = urllib.request.Request(
        URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)
        message = (data.get("choices") or [{}])[0].get("message") or {}
        print(json.dumps({"probe": name, "status": 200, "model": data.get("model"),
                          "content_prefix": str(message.get("content") or "")[:80],
                          "message_keys": sorted(message),
                          "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
                          "reasoning_length": len(str(message.get("reasoning_content") or "")),
                          "tool_calls": len(message.get("tool_calls") or [])}, ensure_ascii=False))
    except urllib.error.HTTPError as error:
        print(json.dumps({"probe": name, "status": error.code,
                          "error_prefix": error.read(200).decode("utf-8", "replace")},
                         ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"probe": name, "exception": type(error).__name__,
                          "detail": str(error)[:150]}, ensure_ascii=False))


call("chat", {"model": "qwen", "messages": [{"role": "user", "content": "只回复 OK"}],
              "temperature": 0, "max_tokens": 256})
call("tools", {"model": "qwen", "messages": [{"role": "user", "content": "查询北京天气"}],
               "temperature": 0, "max_tokens": 80,
               "tools": [{"type": "function", "function": {"name": "weather",
                          "description": "Query weather by city",
                          "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                         "required": ["city"]}}}],
               "tool_choice": "required"})

try:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="qwen", api_key=KEY, base_url="http://20.222.143.1:1030/v1",
                       temperature=0, max_tokens=256, timeout=20, max_retries=0)
    reply = model.invoke("只回复 OK")
    print(json.dumps({"probe": "langchain_chat", "content_prefix": str(reply.content)[:80],
                      "model": reply.response_metadata.get("model_name")}, ensure_ascii=False))
    tool_model = model.bind_tools([{"name": "weather", "description": "Query weather by city",
                                  "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                 "required": ["city"]}}], tool_choice="required")
    tool_reply = tool_model.invoke("查询北京天气")
    print(json.dumps({"probe": "langchain_tools", "tool_calls": len(tool_reply.tool_calls)},
                     ensure_ascii=False))
except Exception as error:
    print(json.dumps({"probe": "langchain", "exception": type(error).__name__,
                      "detail": str(error)[:180]}, ensure_ascii=False))
