# Deploy notes

## Maintenance timer

Interaction polling, affinity folding, growth settlement and retention used to
run inside the refresh loop, where every second it spent delayed the next
ingest. Run them from a timer instead:

    cp deploy/feedgen-maintenance.{service,timer} ~/.config/systemd/user/
    # edit the three paths, rename to <deployment>-maintenance.*
    systemctl --user daemon-reload
    systemctl --user enable --now feedgen-maintenance.timer

Then set `"maintenance_in_service": false` in config.json so the loop stops
doing the same work. Leaving it true is safe (the pass is idempotent) but
wastes the split.

## Database housekeeping

    python3 db_housekeeping.py stats      # size, free pages, row counts
    python3 db_housekeeping.py rollup     # served_posts -> served_daily
    python3 db_housekeeping.py vacuum     # verified compact copy, manual swap

`served_posts` is rolled up daily by the engine; `engagement_snapshots` is
pruned at `snapshot_keep_days` (default 30). A `VACUUM` reclaims free pages
(~9 % on the leftist database when measured) and is not a substitute for
retention.

## Restore drill

    python3 restore_drill.py --config config.json --db /path/to/feedgen.sqlite

Copies the database with `.backup`, checks integrity and row counts, boots a
second engine on port 8199 against the copy, asks it for did.json, the feed
descriptor and a skeleton, then deletes the copy. Run it after any schema
change, not only when you think something is wrong.
