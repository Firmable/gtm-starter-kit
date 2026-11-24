#!/usr/bin/env bash
# The requests the duplicate-account-review recipe makes for one candidate pair.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company and decision calls spend credits; listing decision tools is free.
# Nothing is merged or deleted: these calls only read data.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
LEFT="${1:-canva.com}"
RIGHT="${2:-canva.com.au}"

company_id() {
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))'
}

echo "1. List the decision tools (free) and check that is_same_business is offered"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. Left company profile (up to 1 credit for a new profile)"
LEFT_JSON=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$LEFT" -H "Authorization: Bearer $FIRMABLE_API_KEY")
echo "$LEFT_JSON" | head -c 600; echo
LEFT_ID=$(echo "$LEFT_JSON" | company_id)

echo "3. Right company profile (up to 1 credit for a new profile)"
RIGHT_JSON=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$RIGHT" -H "Authorization: Bearer $FIRMABLE_API_KEY")
echo "$RIGHT_JSON" | head -c 600; echo
RIGHT_ID=$(echo "$RIGHT_JSON" | company_id)

echo "4. is_same_business with reasoning (0.08 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions?withReasoning=true" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"is_same_business\", \"question\": {\"company_id\": \"$LEFT_ID\", \"other_company_id\": \"$RIGHT_ID\"}}"
echo
