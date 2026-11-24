#!/usr/bin/env bash
# The requests the contact-health-check recipe makes, for one contact.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The company and person profiles and the decision answers spend credits; listing decision tools is free.
# These are Firmable-held observations. Nothing here sends an email or SMS or makes a call.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
WORK_EMAIL="${1:-jane.citizen@example.com}"
DOMAIN="${2:-canva.com}"
TITLE="${3:-Head of Marketing}"

echo "1. List the decision tools (free)"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. Resolve the expected employer to a company ID (up to 1 credit for a new profile)"
COMPANY_ID=$(curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')
echo "company_id=$COMPANY_ID"

echo "3. Optional: person profile (up to 1 credit for a new profile; only when fetch_person is true)"
curl -sS -G "$API/people" --data-urlencode "work_email=$WORK_EMAIL" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

ask() {
  # $1 tool, $2 question JSON. 0.08 credits for a yes or no with reasoning; unknown is free.
  curl -sS -X POST "$API/decisions?withReasoning=true" \
    -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
    -d "{\"tool\": \"$1\", \"question\": $2}"
  echo
}

echo "4. Works at the expected employer"
ask works_at_company "{\"work_email\": \"$WORK_EMAIL\", \"company_id\": \"$COMPANY_ID\"}"
echo "5. Still in the expected role (this tool uses a decision model inside Firmable's service)"
ask is_contact_current "{\"work_email\": \"$WORK_EMAIL\", \"company_id\": \"$COMPANY_ID\", \"title\": \"$TITLE\"}"
echo "6. Work email likely to deliver (Firmable's deliverability grade, not a live mailbox check)"
ask is_email_valid "{\"work_email\": \"$WORK_EMAIL\"}"
echo "7. Mobile held by Firmable (a yes means a mobile is held, not that it is live)"
ask is_mobile_valid "{\"work_email\": \"$WORK_EMAIL\"}"
echo "8. Mobile on the Australian Do Not Call register, as at Firmable's last refresh (not a legal check)"
ask is_mobile_on_dnc "{\"work_email\": \"$WORK_EMAIL\"}"
