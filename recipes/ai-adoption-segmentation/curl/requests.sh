#!/usr/bin/env bash
# The requests the ai-adoption-segmentation recipe makes for one company.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# AI Adoption and Tech Intel spend credits. Listing decision tools is free; a decision answer spends credits.
# The recipe asks Tech Intel and Decisions only when a segment being tried has a condition that needs them.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"

echo "1. AI Adoption (1 credit per 5 companies found; not found is free)"
AI=$(curl -sS -X POST "$API/ai-adoption" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"domain\": \"$DOMAIN\"}")
echo "$AI" | head -c 800; echo
COMPANY_ID=$(echo "$AI" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("company") or {}).get("firmable_id",""))')

echo "2. AI Adoption with evidence (3 credits per company found with evidence), only when with_evidence is true"
echo "   curl -sS -X POST \"$API/ai-adoption?withEvidence=true\" -H \"Authorization: Bearer \$FIRMABLE_API_KEY\" -H \"Content-Type: application/json\" -d '{\"domain\": \"$DOMAIN\"}'"

echo "3. Tech Intel, first page (up to 5 credits per company, charged once; 403 if the key cannot use it)"
curl -sS -G "$API/tech-intel" --data-urlencode "id=$COMPANY_ID" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 600; echo

echo "4. List the decision tools (free)"
curl -sS "$API/decisions" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 400; echo

echo "5. One decision check with reasoning (0.08 credits for a yes or no; unknown is free)"
curl -sS -X POST "$API/decisions?withReasoning=true" \
  -H "Authorization: Bearer $FIRMABLE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"tool\": \"is_b2b\", \"question\": {\"company_id\": \"$COMPANY_ID\"}}"
echo
