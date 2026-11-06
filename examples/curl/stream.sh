#!/bin/sh
# Streaming, with the diagnostic headers shown.
#
# -D- prints the response headers, which is where X-Armor-* lives: how much
# armour was injected, how many retries were burned, and the final verdict.
set -e
BASE_URL=${BASE_URL:-http://127.0.0.1:8412/v1}
API_KEY=${API_KEY:?set API_KEY}

curl -sN "$BASE_URL/chat/completions" \
  -D- \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": "count from 1 to 5"}],
        "stream": true,
        "max_tokens": 200
      }'
