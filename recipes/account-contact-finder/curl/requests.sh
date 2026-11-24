#!/usr/bin/env bash
# The requests the account-contact-finder recipe makes for one account and one candidate.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company profile, decision answers and person profile spend credits; people search and the
# decision tool list are free.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"
json_field() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "1. List the decision tools (free)"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. Company profile (up to 1 credit for a new profile)"
COMPANY_ID=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY" \
  | json_field 'd.get("id", "")')
echo "Company ID: $COMPANY_ID"

echo "3. People search: finance department (14), C-Suite / Partner seniority (3) (free)"
SEARCH=$(curl -sS -X POST "$API/people/search" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"companyId\": \"$COMPANY_ID\", \"selectedCountry\": \"AU\", \"department\": \"14\", \"seniority\": \"3\", \"from\": \"0\", \"size\": \"10\"}")
echo "$SEARCH" | head -c 800; echo
PERSON_ID=$(echo "$SEARCH" | json_field '(d.get("records") or [{}])[0].get("person_id", "")')
[ -n "$PERSON_ID" ] || { echo "No candidates found."; exit 0; }

echo "4. One check on the first candidate (0.04 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"matches_seniority\", \"question\": {\"person_id\": \"$PERSON_ID\", \"seniorities\": [\"C-Suite / Partner\"]}}"
echo

echo "5. Person profile with contact details (up to 1 credit for a new profile)"
curl -sS -G "$API/people" --data-urlencode "id=$PERSON_ID" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 800; echo
