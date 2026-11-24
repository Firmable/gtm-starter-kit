#!/usr/bin/env bash
# The requests the tech-sales-opportunities recipe makes, one company at a time.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# Tech Intel spends credits: up to 5 the first time you get a company, plus 0.2 for every page
# requested with evidence. The company profile (optional) costs up to 1 credit for a new profile.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"

echo "1. Optional: company profile (up to 1 credit for a new profile; used when fetch_company is true)"
curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. First page of Tech Intel with evidence (up to 5 credits once per company, plus 0.2 for the evidence request)"
PAGE=$(curl -sS -G "$API/tech-intel" --data-urlencode "fqdn=$DOMAIN" --data-urlencode "withEvidence=true" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY")
echo "$PAGE" | head -c 1200; echo
CURSOR=$(echo "$PAGE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("next_cursor") or "")')

if [ -n "$CURSOR" ]; then
  echo "3. Next page with the same identifier and the cursor (0.2 credits for the evidence request)"
  curl -sS -G "$API/tech-intel" --data-urlencode "fqdn=$DOMAIN" --data-urlencode "withEvidence=true" \
    --data-urlencode "cursor=$CURSOR" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 1200
  echo
else
  echo "3. No next_cursor: the first page held every technology."
fi
