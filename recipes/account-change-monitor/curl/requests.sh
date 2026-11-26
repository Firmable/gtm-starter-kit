#!/usr/bin/env bash
# The requests the account-change-monitor recipe makes for one account.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company, person, AI Adoption and Tech Intel calls can all spend credits.
# The recipe saves the monitored fields from these responses as a snapshot and compares the next run with it.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"
WORK_EMAIL="${2:-}"

echo "1. Company profile (up to 1 credit for a new profile)"
COMPANY=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY")
echo "$COMPANY" | head -c 600; echo
COMPANY_ID=$(echo "$COMPANY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')

if [ -n "$WORK_EMAIL" ]; then
  echo "2. Person profile (up to 1 credit for a new profile)"
  curl -sS -G "$API/people" --data-urlencode "work_email=$WORK_EMAIL" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo
fi

echo "3. AI Adoption (1 credit per 5 companies found; not found is free)"
curl -sS -X POST "$API/ai-adoption" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"company_id\": \"$COMPANY_ID\"}" | head -c 600; echo

echo "4. Tech Intel, first page (up to 5 credits per company, charged once; 403 if the key cannot use it)"
curl -sS -G "$API/tech-intel" --data-urlencode "id=$COMPANY_ID" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo
