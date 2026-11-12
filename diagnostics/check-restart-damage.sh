#!/usr/bin/env bash
# Run the restart-damage check and page when it finds depth that is still missing.
#
# Why a wrapper: systemd cannot run something "on failure" without a second unit,
# and the notifier belongs to the estate (scripts/notify.py + alerts.toml), not to
# the feeds engine. This keeps the engine repo free of estate paths and gives the
# timer one thing to call.
#
# notify.py fails open and says so when no sink is configured
# (ESTATE_NTFY_URL / ESTATE_ALERT_EMAIL). That is deliberate: a missing sink must
# not make a real failure look like a green check, so this prints the reason to
# the journal and still exits with the check's own code.
#
# Usage: check-restart-damage.sh <db> <config> <state-file> <hostname>
set -uo pipefail

DB="${1:?db path}"
CONFIG="${2:?config path}"
STATE="${3:?state path}"
HOST="${4:-$(hostname)}"
ENGINE=/mnt/disk-important/personal/documents/code/python/bluesky-feed-generator
NOTIFY=/mnt/disk-important/personal/documents/code/estate/scripts/notify.py

python3 "$ENGINE/diagnostics/restart_damage.py" \
    --db "$DB" --config "$CONFIG" --hours 2 --state "$STATE"
rc=$?

if [ "$rc" -ne 0 ]; then
    if [ -x "$NOTIFY" ] || [ -f "$NOTIFY" ]; then
        python3 "$NOTIFY" --rule feeds_corpus_damage --target "$HOST" \
            --because "a supply-bound board is below 90% of its cap after a step change in in_window; leftist-3d fell 330 -> 170 this way and took hours to refill" \
            --command "python3 $ENGINE/diagnostics/restart_damage.py --db $DB --config $CONFIG --hours 2" \
            || echo "notify failed; the state file still carries the finding"
    else
        echo "no notifier at $NOTIFY; state file only: $STATE"
    fi
fi
exit "$rc"
