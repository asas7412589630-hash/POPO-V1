#!/usr/bin/env python3
"""Talk to the proxy with the official OpenAI SDK.

Two things trip people up, so this file makes both explicit:

  1. base_url points at the PROXY, not the provider. It ends in /v1.
  2. api_key is the key the proxy issued (triad-...), NOT your upstream key.
     The upstream key lives on the proxy; the client never sees it.

Install (this example is the only thing here that needs a package):
    pip install openai

Run:
    API_KEY=triad-... python3 chat.py
"""
import os
import sys

from openai import OpenAI

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8412/v1")
API_KEY = os.environ.get("API_KEY", "")
MODEL = os.environ.get("MODEL", "glm-5.3")

if not API_KEY:
    sys.exit("set API_KEY to the key from the proxy console (金鑰 → 建立)")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

# ---- non-streaming ---------------------------------------------------------
resp = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "reply with one word: ok"}],
    max_tokens=32,
)
print("answer:", resp.choices[0].message.content.strip())

# ---- streaming -------------------------------------------------------------
# The proxy streams properly: the first token arrives in well under a second
# instead of after the whole answer is generated.
print("stream:", end=" ")
with client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "count from 1 to 5"}],
    stream=True,
    max_tokens=200,
) as stream:
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)
print()
