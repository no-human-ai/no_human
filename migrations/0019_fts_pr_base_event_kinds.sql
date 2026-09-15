-- 0019 (task 22c4ddf6 finding #3, "a stale-but-mergeable PR is never
-- re-measured or woken"): `blockers.wake._check_base_stale` emits two new
-- event kinds, `pr_base_remeasured` and `pr_base_undetermined`, neither of
-- which existed when migration 0006 first keyed the FTS trigger. Same trap
-- migration 0009's own comment names: `CREATE TRIGGER IF NOT EXISTS` will NOT
-- update the trigger on a database that already has it, so a pre-existing
-- install would silently never FTS-index either new kind. Drop and recreate
-- with both added. Idempotent: this whole script runs on every connect
-- (executescript). The one-time backfill of already-stored events of these
-- two kinds is handled below, mirroring 0006's own idempotent INSERT..SELECT.
DROP TRIGGER IF EXISTS task_events_fts_insert;
CREATE TRIGGER IF NOT EXISTS task_events_fts_insert
AFTER INSERT ON task_events
WHEN json_extract(NEW.data, '$.kind') IN (
  'attempt_failed', 'escalated', 'pr_ci_red', 'ci_gate_fail', 'review',
  'blocked', 'tamper', 'pr_base_remeasured', 'pr_base_undetermined'
)
BEGIN
  INSERT INTO events_fts(rowid, text)
  VALUES (NEW.id, COALESCE(json_extract(NEW.data, '$.text'), ''));
END;

INSERT INTO events_fts(rowid, text)
SELECT e.id, COALESCE(json_extract(e.data, '$.text'), '')
FROM task_events e
WHERE json_extract(e.data, '$.kind') IN ('pr_base_remeasured', 'pr_base_undetermined')
AND e.id NOT IN (SELECT rowid FROM events_fts);
