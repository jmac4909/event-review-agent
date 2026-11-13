-- Store the chosen input, so a changed retry cannot reuse another scenario.
ALTER TABLE review.events ADD COLUMN simulation_scenario text
    CHECK (simulation_scenario IN (
        'repeat', 'november_12', 'november_14',
        'distance_3m', 'distance_4_99m', 'distance_5m'
    ));
GRANT INSERT (simulation_scenario) ON review.events TO review_app;
