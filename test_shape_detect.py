#!/usr/bin/env python3
"""形狀判定測試 — anthropic body 偵測必須保守（誤判會改寫呼叫端的請求）。"""
import sys
sys.path.insert(0, "/var/hark/shared/armor_triad")
import os
os.environ.setdefault("ARMOR_PROXY_PORT", "1")
import armor_proxy as AP

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %-50s %s" % (name, extra))
    else:
        fail += 1
        print("  FAIL  %-50s %s" % (name, extra))


def looks(body):
    return AP._looks_anthropic_body(body)


# --- 明確的 Anthropic 形狀 ---------------------------------------------------
check("system 在頂層", looks({"model": "m", "system": "x",
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 9}))
check("stop_sequences", looks({"model": "m", "stop_sequences": ["\n\n"],
                              "messages": [{"role": "user", "content": "hi"}]}))
check("anthropic_version", looks({"model": "m", "anthropic_version": "2023-06-01",
                                 "messages": [{"role": "user", "content": "hi"}]}))
check("tools/input_schema", looks({"model": "m",
                                  "messages": [{"role": "user", "content": "hi"}],
                                  "tools": [{"name": "f",
                                             "input_schema": {"type": "object"}}]}))
check("tool_use block", looks({"model": "m", "messages": [
    {"role": "user", "content": [{"type": "tool_use", "id": "t", "name": "f"}]}]}))
check("image block", looks({"model": "m", "messages": [
    {"role": "user", "content": [{"type": "image", "source": {}}]}]}))

# --- 明確的 OpenAI 形狀（必須否決）-------------------------------------------
check("OpenAI 基本訊息", not looks({"model": "m",
                                "messages": [{"role": "user", "content": "hi"}]}))
check("OpenAI tools/function", not looks({
    "model": "m", "messages": [{"role": "user", "content": "hi"}],
    "tools": [{"type": "function", "function": {"name": "f"}}]}))
check("OpenAI response_format", not looks({
    "model": "m", "messages": [{"role": "user", "content": "hi"}],
    "response_format": {"type": "json_object"}}))
check("OpenAI n>1", not looks({"model": "m", "n": 2,
                              "messages": [{"role": "user", "content": "hi"}]}))
check("OpenAI seed", not looks({"model": "m", "seed": 7,
                               "messages": [{"role": "user", "content": "hi"}]}))
check("OpenAI stream_options", not looks({
    "model": "m", "stream_options": {"include_usage": True},
    "messages": [{"role": "user", "content": "hi"}]}))
check("OpenAI presence_penalty", not looks({
    "model": "m", "presence_penalty": 0.5,
    "messages": [{"role": "user", "content": "hi"}]}))

# --- 模糊情況一律當 OpenAI（直通安全）----------------------------------------
check("text-only blocks 視為模糊", not looks({
    "model": "m", "max_tokens": 5,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}))
check("空 body", not looks({}))
check("None", not looks(None))
check("只有 model", not looks({"model": "m"}))
check("messages 不是 list", not looks({"model": "m", "messages": "hi"}))
check("messages 空陣列", not looks({"model": "m", "messages": []}))

# --- veto 優先於 signal ------------------------------------------------------
check("system + response_format（OpenAI 勝）", not looks({
    "model": "m", "system": "x", "response_format": {"type": "json_object"},
    "messages": [{"role": "user", "content": "hi"}]}))
check("system + tools/function（OpenAI 勝）", not looks({
    "model": "m", "system": "x",
    "tools": [{"type": "function", "function": {"name": "f"}}],
    "messages": [{"role": "user", "content": "hi"}]}))

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
