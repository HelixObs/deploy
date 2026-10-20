#!/usr/bin/env bash
# One-off: carry the CHIMEFRB "duplicate replica" silence over to datatrail's
# new update-state error message.
#
# Run right AFTER CHIMEFRB/datatrail#808 is deployed. Until then, this is not
# needed; after the deploy and before this runs, that failure alerts in Slack
# and opens a GitHub issue.
#
# Why: #808 changes update-state errors from the raw exception text to a
# written message. The unique-constraint (file_storage_pair) violation now
# arrives as "New replica is already registered at this storage element.",
# whose fingerprint no active silence matches. This copies silence #24 (same
# expiry, author) to that fingerprint. Scope is unchanged: only that failure.
#
# Fingerprint: herald fingerprint.Compute("CHIMEFRB", "helix.error",
#   "New replica is already registered at this storage element.",
#   "update-state", "") = cb6073e6da7c78e9
#
# Usage:
#   ./2026-10-resilence-update-state.sh            # dry run: shows what would change
#   ./2026-10-resilence-update-state.sh --apply    # writes the silence
#
# Safe to re-run: it does nothing if the new silence already exists.

set -euo pipefail

NAMESPACE=helixobs
EXPECTED_CONTEXT=Arbutus-Admin
SOURCE_ID=24
SOURCE_FP=dc60da749b86c741
NEW_FP=cb6073e6da7c78e9

context=$(kubectl config current-context)
if [[ "$context" != "$EXPECTED_CONTEXT" ]]; then
  echo "error: kubectl context is '$context', expected '$EXPECTED_CONTEXT'" >&2
  exit 1
fi

psql() {
  kubectl exec -i -n "$NAMESPACE" timescaledb-0 -- \
    psql -U helix -d helixobs -v ON_ERROR_STOP=1 "$@"
}

echo "Current state:"
psql -c "SELECT id, fingerprint, silenced_by, expires_at, reason
         FROM notification_silences
         WHERE id = $SOURCE_ID OR fingerprint = '$NEW_FP'
         ORDER BY id;"

if [[ "${1:-}" != --apply ]]; then
  echo "Dry run. Would copy silence #$SOURCE_ID ($SOURCE_FP) to $NEW_FP,"
  echo "unless a silence with $NEW_FP already exists. Re-run with --apply."
  exit 0
fi

psql <<SQL
BEGIN;

DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM notification_silences
                   WHERE id = $SOURCE_ID AND fingerprint = '$SOURCE_FP' AND expires_at > now()) THEN
        RAISE EXCEPTION 'silence #$SOURCE_ID ($SOURCE_FP) is missing, expired or changed; re-check before copying';
    END IF;
END
\$\$;

INSERT INTO notification_silences (instrument_id, event_type, fingerprint, silenced_by, silenced_at, expires_at, reason)
SELECT instrument_id, event_type, '$NEW_FP', silenced_by, now(), expires_at,
       '(migrated from silence $SOURCE_ID: update-state now reports a written message)'
FROM notification_silences
WHERE id = $SOURCE_ID
  AND NOT EXISTS (SELECT 1 FROM notification_silences
                  WHERE fingerprint = '$NEW_FP' AND expires_at > now());

SELECT id, fingerprint, expires_at, reason FROM notification_silences
WHERE id = $SOURCE_ID OR fingerprint = '$NEW_FP' ORDER BY id;

COMMIT;
SQL
