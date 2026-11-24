#!/usr/bin/env bash
# The requests the campaign-audience-check recipe makes, for one company and one campaign.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# The decision call spends credits; listing decision tools is free.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"

echo "1. List the decision tools and read the matches_campaign_audience schema (free)"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "2. Optional: company profile for display (up to 1 credit for a new profile; only when fetch_company is true)"
curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "3. One campaign with reasoning (0.08 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions?withReasoning=true" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"matches_campaign_audience\", \"question\": {\"domain\": \"$DOMAIN\", \"campaign\": {\"message\": \"Shared brand templates for marketing and design teams at Australian software companies\", \"countries\": [\"AU\"], \"min_staff\": 200}}}"
echo
