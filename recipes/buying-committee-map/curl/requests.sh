#!/usr/bin/env bash
# The requests the buying-committee-map recipe makes for one account and one person.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company profile and decision answers spend credits; people search and the decision tool list are free.
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

echo "3. People search, up to 25 people in AU (free)"
SEARCH=$(curl -sS -X POST "$API/people/search" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"companyId\": \"$COMPANY_ID\", \"selectedCountry\": \"AU\", \"from\": \"0\", \"size\": \"25\"}")
echo "$SEARCH" | head -c 800; echo
PERSON_ID=$(echo "$SEARCH" | json_field '(d.get("records") or [{}])[0].get("person_id", "")')
[ -n "$PERSON_ID" ] || { echo "No people found."; exit 0; }

echo "4. Department of the first person against every role's departments (0.04 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"matches_department\", \"question\": {\"person_id\": \"$PERSON_ID\", \"departments\": [\"Finance\", \"General Management\", \"Engineering & Technical\", \"Operations\"]}}"
echo

echo "5. Seniority of the first person against every role's seniorities (0.04 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"matches_seniority\", \"question\": {\"person_id\": \"$PERSON_ID\", \"seniorities\": [\"C-Suite / Partner\", \"Owner / Founder\", \"VP/Director / Head of\", \"Manager\"]}}"
echo

echo "6. Optional: would this person make the buying call (0.04 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"is_decision_maker\", \"question\": {\"person_id\": \"$PERSON_ID\", \"seller\": \"Accounts payable automation software\"}}"
echo
