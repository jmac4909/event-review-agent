const labels = {
  same_day: "Date test · Same day",
  two_days_later: "Date test · Two days later",
  november_12: "Date test · November 12, 2026",
  november_14: "Date test · November 14, 2026",
  distance_3m: "Distance test · 3 m",
  distance_4_99m: "Distance test · 4.99 m",
  distance_5m: "Distance test · 5 m",
};

export default function SimulationLabel({ event }) {
  let label = labels[event.simulation_scenario];
  if (Date.parse(event.occurred_at) > Date.now()) label = "Future-date test";
  return label ? <span className="simulation-label">{label}</span> : null;
}
