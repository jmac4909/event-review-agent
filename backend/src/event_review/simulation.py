"""Explicit demo inputs. Scenarios never supply a review or a policy."""

from datetime import datetime, timedelta, timezone


class InvalidScenario(ValueError):
    pass


def scenario_evidence(source: dict, scenario: str) -> dict:
    evidence = {
        "metadata": dict(source["metadata"]),
        "description": source["description"],
        "occurred_at": None,  # The database supplies the arrival time by default.
    }
    if scenario == "repeat":
        return evidence
    if scenario in {"same_day", "two_days_later"}:
        if source["event_type"] != "restricted_area_entry":
            raise InvalidScenario("Date scenarios require an access event.")
        days = 0 if scenario == "same_day" else 2
        evidence["occurred_at"] = source["occurred_at"].astimezone(timezone.utc) + timedelta(days=days)
        return evidence
    if scenario in {"november_12", "november_14"}:
        if source["event_type"] != "restricted_area_entry":
            raise InvalidScenario("Date scenarios require an access event.")
        day = 12 if scenario == "november_12" else 14
        evidence["occurred_at"] = datetime(2026, 11, day, 12, tzinfo=timezone.utc)
        return evidence
    distances = {"distance_3m": 3, "distance_4_99m": 4.99, "distance_5m": 5}
    if scenario not in distances:
        raise InvalidScenario("Unknown simulation scenario.")
    if source["event_type"] != "pedestrian_vehicle_proximity":
        raise InvalidScenario("Distance scenarios require a vehicle proximity event.")
    distance = distances[scenario]
    evidence["metadata"]["estimated_distance_m"] = distance
    evidence["description"] = f"The detector reported {distance} m separation between pedestrians and a moving vehicle."
    return evidence
