#!/bin/sh
# Minimal chat completion through the proxy.
#
# The only two values that matter:
#   BASE_URL  the proxy, NOT the provider. It must end in /v1.
#   API_KEY   the key the proxy issued (triad-...), not your upstream key.
set -e
BASE_URL=${BASE_URL:-http://127.0.0.1:8412/v1}
API_KEY=${API_KEY:?set API_KEY to the key from the proxy console}
MODEL=${MODEL:-glm-5.3}

curl -s "$BASE_URL/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
        \"model\": \"$MODEL\",
        \"messages\": [{\"role\": \"user\", \"content\": \"reply with one word: ok\"}],
        \"max_tokens\": 32
      }"
