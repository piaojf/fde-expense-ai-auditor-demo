CREATE TRIGGER IF NOT EXISTS enterprise_travel_event_no_update
BEFORE UPDATE ON enterprise_events
WHEN json_extract(OLD.body, '$.kind') IN
    ('TRAVEL_POLICY_PUBLISHED','TRAVEL_POLICY_RETIRED','TRAVEL_PROFILE_SET')
BEGIN SELECT RAISE(ABORT, 'immutable travel event'); END;
CREATE TRIGGER IF NOT EXISTS enterprise_travel_event_no_delete
BEFORE DELETE ON enterprise_events
WHEN json_extract(OLD.body, '$.kind') IN
    ('TRAVEL_POLICY_PUBLISHED','TRAVEL_POLICY_RETIRED','TRAVEL_PROFILE_SET')
BEGIN SELECT RAISE(ABORT, 'immutable travel event'); END;
CREATE TRIGGER IF NOT EXISTS enterprise_travel_snapshot_no_update
BEFORE UPDATE ON enterprise_versions
WHEN json_extract(OLD.body, '$.employee_snapshot') IS NOT
     json_extract(NEW.body, '$.employee_snapshot')
  OR json_extract(OLD.body, '$.travel_details') IS NOT
     json_extract(NEW.body, '$.travel_details')
BEGIN SELECT RAISE(ABORT, 'immutable employee and travel snapshot'); END;
