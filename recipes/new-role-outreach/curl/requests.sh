#!/usr/bin/env bash
# The requests the new-role-outreach recipe makes, for one account.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company and person profiles and the decision answers spend credits; listing decision tools and
# people search are free. Nothing here sends outreach.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"
SINCE="${2:-2026-05-24}"   # evaluation date minus the window, as YYYY-MM-DD

echo "1. List the decision tools (free)"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. Resolve the account domain to a company ID (up to 1 credit for a new profile)"
COMPANY_ID=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')
echo "company_id=$COMPANY_ID"

echo "3. Search people at the company (free). Seniority 4 is VP/Director / Head of. All values are text."
PEOPLE=$(curl -sS -X POST "$API/people/search" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"companyId\": \"$COMPANY_ID\", \"selectedCountry\": \"AU\", \"seniority\": \"4\", \"from\": \"0\", \"size\": \"10\"}")
echo "$PEOPLE" | head -c 800; echo
PERSON_ID=$(echo "$PEOPLE" | python3 -c 'import json,sys; r=(json.load(sys.stdin).get("records") or [{}])[0]; print(r.get("person_id") or r.get("id") or "")')
echo "person_id=$PERSON_ID"

ask() {
  # $1 tool, $2 question JSON. 0.08 credits for a yes or no with reasoning; unknown is free.
  curl -sS -X POST "$API/decisions?withReasoning=true" \
    -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
    -d "{\"tool\": \"$1\", \"question\": $2}"
  echo
}

echo "4. New to role in the last 6 months"
ask is_new_to_role "{\"person_id\": \"$PERSON_ID\", \"within_months\": 6, \"count_internal_moves\": true}"
echo "5. New executive in the last 6 months (this tool uses a decision model inside Firmable's service)"
ask is_new_exec "{\"person_id\": \"$PERSON_ID\", \"within_months\": 6}"
echo "6. Changed jobs since the window start"
ask has_changed_jobs_since "{\"person_id\": \"$PERSON_ID\", \"since\": \"$SINCE\"}"

echo "7. Contact details for a selected person only (up to 1 credit for a new profile)"
curl -sS -G "$API/people" --data-urlencode "id=$PERSON_ID" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo
