#!/usr/bin/env bash
# Launch Claude Code against the local gateway -> Foundry FW-GLM-5.2.
# Start the gateway first: ./start-gateway.sh
#
#   ./claude-foundry.sh              # interactive
#   ./claude-foundry.sh -p "hello"   # one-shot; any claude flags pass through
set -euo pipefail

cd "$(dirname "$0")"

if ! curl -sf --max-time 3 http://localhost:1064/health >/dev/null 2>&1; then
  echo "error: gateway not running. Start it with ./start-gateway.sh" >&2
  exit 1
fi

[[ -f .env ]] && { set -a; . ./.env; set +a; }

MODEL="${FOUNDRY_MODEL:-FW-GLM-5.2}"

# Claude Code doesn't know FW-GLM-5.2's context window and warns about it, then
# assumes a conservative 200k. Set GLM_CONTEXT_TOKENS in .env to the deployment's
# real window to silence the warning and avoid auto-compacting earlier than needed.
[[ -n ${GLM_CONTEXT_TOKENS:-} ]] && export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$GLM_CONTEXT_TOKENS"

# Point Claude Code at the gateway instead of api.anthropic.com.
# The /anthropic suffix is required: aigw registers the Anthropic Messages
# processor at the exact path /anthropic/v1/messages. Claude Code appends
# /v1/messages to the base URL, so the root would 404 "unsupported path".
export ANTHROPIC_BASE_URL="http://localhost:1975/anthropic"

# The real credential is injected upstream by the gateway's BackendSecurityPolicy.
# Claude Code still needs *a* token set, or it falls back to its OAuth login flow.
export ANTHROPIC_AUTH_TOKEN="gateway-injected"

# Map every model slot to the one Foundry deployment. The gateway's bodyMutation
# pins the upstream model anyway; setting these keeps the access log and the
# Claude Code status line honest about what is actually serving.
export ANTHROPIC_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_FABLE_MODEL="$MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL="$MODEL"

# Keep telemetry/auto-update chatter from hitting the gateway.
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

exec claude "$@"
