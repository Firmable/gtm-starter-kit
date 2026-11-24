#!/usr/bin/env bash
# The requests the crm-enrichment recipe makes for one row.
# Reads the key from FIRMABLE_API_KEY. Nothing here runs on install.
# Both calls spend credits: up to 1 credit for each profile new to your organisation.
set -euo pipefail
: "${FIRMABLE_API_KEY:?Set FIRMABLE_API_KEY to your developer API key}"
API="https://api.firmable.com"
DOMAIN="${1:-canva.com}"
WORK_EMAIL="${2:-}"

echo "1. Company profile by domain (up to 1 credit for a new profile)"
curl -sS -G "$API/company" --data-urlencode "fqdn=$DOMAIN" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 800; echo

if [ -n "$WORK_EMAIL" ]; then
  echo "2. Person profile by work email (up to 1 credit for a new profile)"
  curl -sS -G "$API/people" --data-urlencode "work_email=$WORK_EMAIL" -H "Authorization: Bearer $FIRMABLE_API_KEY" | head -c 800; echo
else
  echo "2. Skipped: pass a work email as the second argument to look up a person."
fi
