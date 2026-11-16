"""Constant-state track summaries with elapsed-time weighting and timer pauses."""

import math

ELEVATION_THRESHOLD_METERS = 3.0
EARTH_RADIUS_METERS = 6_371_000  # Mean radius; see docs/reference/garmin-tracks.md.


def ground_distance(first: dict, second: dict) -> float | None:
    """Haversine great-circle distance between two complete positions, in meters."""
    if not all(key in point for point in (first, second) for key in ("latitude", "longitude")):
        return None
    lat1, lat2 = (math.radians(point["latitude"]) for point in (first, second))
    delta = math.radians(second["longitude"] - first["longitude"])
    chord = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta / 2) ** 2
    )
    return EARTH_RADIUS_METERS * 2 * math.asin(math.sqrt(min(1, chord)))


class TrackSummary:
    def __init__(self) -> None:
        self.first: int | None = None
        self.clock: int | None = None
        self.active = True
        self.moving = self.last_moving = 0.0
        self.previous: dict = {}
        self.distance = 0.0
        self.maximum_speed: float | None = None
        self.ascent = self.descent = 0.0
        self.elevation: float | None = None
        self.bounds: list[float] | None = None

    def advance(self, moment: int) -> None:
        """Integrate timer-active seconds; a time behind the clock adds nothing."""
        if self.clock is not None and moment >= self.clock:
            if self.active and self.first is not None:
                self.moving += moment - self.clock
        self.clock = max(moment, self.clock) if self.clock is not None else moment

    def event(self, event: dict) -> None:
        # A late (backward) event still changes the timer state from the current clock;
        # ignoring it would count a whole pause, or drop the rest of the activity.
        self.advance(event["time"])
        self.active = event["action"] == "resume"

    def sample(self, point: dict) -> bool:
        moment = point["time"]
        if self.previous and moment <= self.previous["time"]:
            return False  # Duplicate/backward records cannot add samples, time or distance.
        self.advance(moment)
        if self.first is None:
            self.first = moment
        active_seconds = self.moving - self.last_moving
        if self.previous:
            self.distance += self.interval_distance(point, active_seconds)
        self.last_moving = self.moving
        if "speed" in point:
            self.maximum_speed = max(self.maximum_speed or 0.0, point["speed"])
        self.update_elevation(point.get("altitude"))
        self.update_bounds(point)
        self.previous = point
        return True

    def interval_distance(self, point: dict, active_seconds: float) -> float:
        before = self.previous
        if "distance" in before and "distance" in point:
            # The source odometer already excludes timer pauses. A reset starts a new segment.
            return max(0, point["distance"] - before["distance"])
        if not active_seconds:
            return 0
        distance = ground_distance(before, point)
        if distance is not None:
            return distance
        if "speed" in before and "speed" in point:
            return (point["speed"] + before["speed"]) / 2 * active_seconds
        return 0

    def update_elevation(self, altitude: float | None) -> None:
        """Hysteresis: only a change of at least the threshold moves the anchor."""
        if altitude is None:
            return
        if self.elevation is None:
            self.elevation = altitude
        difference = altitude - self.elevation
        if abs(difference) >= ELEVATION_THRESHOLD_METERS:
            self.ascent += max(0, difference)
            self.descent += max(0, -difference)
            self.elevation = altitude

    def update_bounds(self, point: dict) -> None:
        if "latitude" not in point or "longitude" not in point:
            return
        lat, lon = point["latitude"], point["longitude"]
        if self.bounds is None:
            self.bounds = [lat, lon, lat, lon]
        else:
            self.bounds = [
                min(self.bounds[0], lat),
                min(self.bounds[1], lon),
                max(self.bounds[2], lat),
                max(self.bounds[3], lon),
            ]

    def result(self) -> dict:
        """Distance in m, durations in s, speeds in m/s; null where no channel exists."""
        elapsed = self.previous["time"] - self.first if self.first is not None else 0
        altitude = self.elevation is not None
        return {
            "elapsed_seconds": elapsed,
            "moving_seconds": self.last_moving,
            "distance": self.distance,
            "average_speed": self.distance / self.last_moving if self.last_moving else None,
            "maximum_speed": self.maximum_speed,
            "ascent": self.ascent if altitude else None,
            "descent": self.descent if altitude else None,
            "bounds": self.bounds,
        }
