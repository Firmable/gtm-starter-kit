#!/usr/bin/env bash
# The requests the pre-call-account-pack recipe makes for one account.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# Listing decision tools and people search are free. The company profile, person profile,
# decisions, Tech Intel and AI Adoption calls spend credits.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"
AUTH=(-H "Authorization: Bearer $FIRMABLE_API_KEY")
JSON=(-H "Content-Type: application/json")

echo "1. List the decision tools (free)"
curl -sS "$API/decisions" "${AUTH[@]}" | head -c 600; echo

echo "2. Company profile (up to 1 credit for a new profile)"
COMPANY=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" "${AUTH[@]}")
echo "$COMPANY" | head -c 600; echo
COMPANY_ID=$(echo "$COMPANY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')

echo "3. One company check with reasoning (0.08 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions?withReasoning=true" "${AUTH[@]}" "${JSON[@]}" \
  -d "{\"tool\": \"is_b2b\", \"question\": {\"company_id\": \"$COMPANY_ID\"}}"
echo

echo "4. People search: sales leaders in Australia (free). Seniority 4 is VP/Director/Head of; department 2 is Sales."
SEARCH=$(curl -sS -X POST "$API/people/search" "${AUTH[@]}" "${JSON[@]}" \
  -d "{\"companyId\": \"$COMPANY_ID\", \"selectedCountry\": \"AU\", \"seniority\": \"4\", \"department\": \"2\", \"from\": \"0\", \"size\": \"3\"}")
echo "$SEARCH" | head -c 600; echo
PERSON_ID=$(echo "$SEARCH" | python3 -c 'import json,sys; r=json.load(sys.stdin).get("records") or [{}]; print(r[0].get("person_id",""))')

if [ -n "$PERSON_ID" ]; then
  echo "5. Person profile for the first match (up to 1 credit for a new profile)"
  curl -sS -G "$API/people" --data-urlencode "id=$PERSON_ID" "${AUTH[@]}" | head -c 600; echo

  echo "6. One contact check with reasoning (0.08 credits for a yes or no; unknown is free)"
  curl -sS -X POST "$API/decisions?withReasoning=true" "${AUTH[@]}" "${JSON[@]}" \
    -d "{\"tool\": \"works_at_company\", \"question\": {\"person_id\": \"$PERSON_ID\", \"company_id\": \"$COMPANY_ID\"}}"
  echo
fi

echo "7. Tech Intel, first page (up to 5 credits per company, charged once)"
curl -sS -G "$API/tech-intel" --data-urlencode "id=$COMPANY_ID" "${AUTH[@]}" | head -c 800; echo

echo "8. AI Adoption, optional (0.2 credits per company found; not found is free)"
curl -sS -X POST "$API/ai-adoption" "${AUTH[@]}" "${JSON[@]}" -d "{\"company_id\": \"$COMPANY_ID\"}" | head -c 800
echo
