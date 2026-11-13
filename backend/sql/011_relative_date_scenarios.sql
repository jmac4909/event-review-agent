ALTER TABLE review.events DROP CONSTRAINT events_simulation_scenario_check;
ALTER TABLE review.events ADD CONSTRAINT events_simulation_scenario_check
    CHECK (simulation_scenario IN (
        'repeat', 'same_day', 'two_days_later', 'november_12', 'november_14',
        'distance_3m', 'distance_4_99m', 'distance_5m'
    ));
