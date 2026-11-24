#!/usr/bin/env bash
# The requests the ai-account-brief recipe makes, one company at a time.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# Every call here can spend credits: the company profile up to 1 credit for a new profile,
# AI Adoption with evidence 3 credits when the company is found (free when not found),
# and Tech Intel up to 5 credits once per company.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"

echo "1. Company profile (up to 1 credit for a new profile)"
COMPANY=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY")
echo "$COMPANY" | head -c 600; echo
COMPANY_ID=$(echo "$COMPANY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')

echo "2. AI Adoption with evidence (3 credits when the company is found; not found is free)"
curl -sS -X POST "$API/ai-adoption?withEvidence=true" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"company_id\": \"$COMPANY_ID\"}" | head -c 1500
echo

echo "3. First page of Tech Intel (up to 5 credits, paid once per company)"
curl -sS -G "$API/tech-intel" --data-urlencode "id=$COMPANY_ID" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 1200
echo
