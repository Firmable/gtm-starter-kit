#!/usr/bin/env bash
# The requests the inbound-lead-router recipe makes for one lead.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company, ICP fit and person calls spend credits. Personal email domains are filtered by the
# recipe before any call. Routing rules are applied by the recipe code, not by the API.
# Nothing is written to a CRM.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"
WORK_EMAIL="${2:-}"

echo "1. Company profile (up to 1 credit for a new profile). Rules read hq_country, employee_count and industries."
COMPANY=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY")
echo "$COMPANY" | head -c 600; echo
COMPANY_ID=$(echo "$COMPANY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')

echo "2. ICP fit with reasoning (0.08 credits for a yes or no; unknown is free). Only when the configuration has icp."
curl -sS -X POST "$API/decisions?withReasoning=true" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"is_icp_fit\", \"question\": {\"company_id\": \"$COMPANY_ID\", \"icp\": {\"min_staff\": 100, \"staff_in\": \"AU\", \"countries\": [\"AU\", \"NZ\"], \"criteria\": [\"Sells to other businesses\", \"Has a sales team\"]}}}"
echo

if [ -n "$WORK_EMAIL" ]; then
  echo "3. Person by work email (up to 1 credit for a new profile; 404 when Firmable has no profile). Only when fetch_person is true."
  curl -sS -G "$API/people" --data-urlencode "work_email=$WORK_EMAIL" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600
  echo
fi
