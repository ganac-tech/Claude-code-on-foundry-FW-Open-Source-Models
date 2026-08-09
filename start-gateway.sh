#!/usr/bin/env bash
# Start the Envoy AI Gateway locally, translating Anthropic /v1/messages
# to the Foundry OpenAI chat-completions endpoint.
#
#   ./start-gateway.sh          # normal
#   ./start-gateway.sh --debug  # verbose Envoy + aigw logs
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "error: .env not found. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${FOUNDRY_HOST:?set FOUNDRY_HOST in .env}"
: "${AZURE_API_KEY:?set AZURE_API_KEY in .env}"

# Normalize whatever the portal gave you down to a bare hostname. The Backend
# resource needs host-only; the portal shows the full project endpoint, e.g.
#   https://my-resource.services.ai.azure.com/api/projects/proj-default
RAW_HOST="$FOUNDRY_HOST"
FOUNDRY_HOST="${FOUNDRY_HOST#*://}"   # strip scheme
FOUNDRY_HOST="${FOUNDRY_HOST%%/*}"    # strip path
FOUNDRY_HOST="${FOUNDRY_HOST%%:*}"    # strip port
export FOUNDRY_HOST

if [[ -z "$FOUNDRY_HOST" || "$FOUNDRY_HOST" != *.* ]]; then
  echo "error: could not read a hostname out of FOUNDRY_HOST=$RAW_HOST" >&2
  exit 1
fi
if [[ "$RAW_HOST" != "$FOUNDRY_HOST" ]]; then
  echo "note: normalized FOUNDRY_HOST -> $FOUNDRY_HOST"
fi

: "${FOUNDRY_MODEL:=FW-GLM-5.2}"
: "${CACHE_KEY:=paypal-claude-code}"
export FOUNDRY_MODEL CACHE_KEY

# Envoy needs a real CA bundle to validate the TLS connection to Foundry.
# BackendTLSPolicy's `wellKnownCACertificates: "System"` points at a Linux path
# that doesn't exist here, so build the ConfigMap the policy references from the
# host's actual bundle and append it to the config we run.
CA_BUNDLE=""
for candidate in /etc/ssl/cert.pem /etc/ssl/certs/ca-certificates.crt \
                 /etc/pki/tls/certs/ca-bundle.crt; do
  [[ -r $candidate ]] && { CA_BUNDLE=$candidate; break; }
done
if [[ -z $CA_BUNDLE ]]; then
  echo "error: no system CA bundle found; cannot validate TLS to Foundry." >&2
  echo "       looked in /etc/ssl/cert.pem, /etc/ssl/certs/ca-certificates.crt," >&2
  echo "       /etc/pki/tls/certs/ca-bundle.crt" >&2
  exit 1
fi

GENERATED=".aigw-foundry.generated.yaml"
{
  cat aigw-foundry.yaml
  printf '\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: system-ca\n  namespace: default\ndata:\n  ca.crt: |\n'
  sed 's/^/    /' "$CA_BUNDLE"
} > "$GENERATED"

echo "messages -> http://localhost:1975/anthropic/v1/messages"
echo "           (set ANTHROPIC_BASE_URL=http://localhost:1975/anthropic)"
echo "admin    -> http://localhost:1064/health , /metrics"
echo "upstream -> https://${FOUNDRY_HOST}/openai/v1/chat/completions"
echo "model    -> ${FOUNDRY_MODEL}   (Foundry deployment name)"
echo "cachekey -> ${CACHE_KEY}   (prompt_cache_key, fleet-wide)"
echo "ca       -> ${CA_BUNDLE}"
echo

exec ./bin/aigw run "$GENERATED" "$@"
