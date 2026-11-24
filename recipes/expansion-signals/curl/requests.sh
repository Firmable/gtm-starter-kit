#!/usr/bin/env bash
# The requests the expansion-signals recipe makes, one company at a time.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# Listing decision tools is free. Each yes or no answer costs 0.04 credits (0.08 with reasoning);
# unknown answers are free. The optional company profile costs up to 1 credit for a new profile.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"

ask() {
  curl -sS -X POST "$API/decisions" \
    -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
    -d "{\"tool\": \"$1\", \"question\": $2}"
  echo
}

echo "1. List the decision tools (free)"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. Recorded in two or more countries (0.04 credits for a yes or no)"
ask is_multinational "{\"domain\": \"$DOMAIN\"}"

echo "3. At least 3 sites recorded (0.04 credits for a yes or no)"
ask has_multiple_sites "{\"domain\": \"$DOMAIN\", \"min_sites\": 3}"

echo "4. At least 10 staff in Australia (0.04 credits for a yes or no; answers only for AU and NZ)"
ask has_local_team "{\"domain\": \"$DOMAIN\", \"country\": \"AU\", \"min_staff\": 10}"

echo "5. Any office or staff recorded in Singapore (0.04 credits for a yes or no)"
ask is_located_in "{\"domain\": \"$DOMAIN\", \"countries\": [\"SG\"], \"where\": \"any_office\"}"

echo "Optional: company profile, used when fetch_company is true (up to 1 credit for a new profile)"
echo "curl -sS -G \"$API/company\" --data-urlencode \"fqdn=$DOMAIN\" -H \"Authorization: Bearer \$FIRMABLE_API_KEY\""
