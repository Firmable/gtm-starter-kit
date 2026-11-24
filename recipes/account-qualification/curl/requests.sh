#!/usr/bin/env bash
# The requests the account-qualification recipe makes, one company at a time.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company and decision calls spend credits; listing decision tools is free.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"

echo "1. List the decision tools (free)"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. Company profile (up to 1 credit for a new profile)"
COMPANY=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY")
echo "$COMPANY" | head -c 600; echo
COMPANY_ID=$(echo "$COMPANY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')

echo "3. One check with reasoning (0.08 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions?withReasoning=true" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"is_located_in\", \"question\": {\"company_id\": \"$COMPANY_ID\", \"countries\": [\"AU\"], \"where\": \"head_office\"}}"
echo
