#!/usr/bin/env bash
# One real call to OpenRouter — so you can honestly say "I've called your API."
# Run:  export OPENROUTER_API_KEY=sk-or-v1-...   then   bash test_api.sh
set -euo pipefail

: "${OPENROUTER_API_KEY:?Set OPENROUTER_API_KEY in your environment first}"

curl -s https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o-mini",
    "messages": [{"role": "user", "content": "In one sentence, what does OpenRouter do?"}]
  }' | python3 -m json.tool
