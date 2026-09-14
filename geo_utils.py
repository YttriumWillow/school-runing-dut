"""Geographic calculations used to build the simulation path."""

import math

from geopy.distance import geodesic
from geopy.point import Point


def calculate_initial_bearing(point_a: Point, point_b: Point) -> float:
    """Calculate the initial bearing from point_a to point_b."""
    lat1 = math.radians(point_a.latitude)
    lon1 = math.radians(point_a.longitude)
    lat2 = math.radians(point_b.latitude)
    lon2 = math.radians(point_b.longitude)
    dLon = lon2 - lon1
    y = math.sin(dLon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - \
        math.sin(lat1) * math.cos(lat2) * math.cos(dLon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def calculate_midpoint(point_a: Point, point_b: Point) -> Point:
    """Calculate the geographic midpoint between two GPS points."""
    distance = geodesic(point_a, point_b).meters
    bearing = calculate_initial_bearing(point_a, point_b)
    return geodesic(meters=distance / 2).destination(
        point=point_a, bearing=bearing
    )


def interpolate_straight(
    p1: Point, p2: Point, step_meters: float
) -> tuple[list[tuple[Point, float]], float]:
    """Generate points along a straight path and return its length."""
    points = []
    total_distance = geodesic(p1, p2).meters
    if total_distance == 0:
        return [], 0.0
    bearing = calculate_initial_bearing(p1, p2)
    num_steps = int(total_distance / step_meters)
    for i in range(num_steps):
        dist = i * step_meters
        new_point = geodesic(meters=dist).destination(point=p1, bearing=bearing)
        points.append((new_point, bearing))
    points.append((p2, bearing))
    return points, total_distance


def interpolate_arc(
    p_start: Point,
    p_end: Point,
    step_meters: float,
    arc_degrees_total: float
) -> tuple[list[tuple[Point, float]], float]:
    """Generate points along an arc and return its length."""
    points = []
    chord_len = geodesic(p_start, p_end).meters
    if chord_len == 0:
        return [], 0.0

    half_chord = chord_len / 2.0
    half_angle_rad = math.radians(arc_degrees_total / 2.0)
    if abs(math.sin(half_angle_rad)) < 1e-6:
        return [], 0.0

    radius = half_chord / math.sin(half_angle_rad)
    arc_length = radius * math.radians(arc_degrees_total)
    num_steps = int(arc_length / step_meters)
    if num_steps == 0:
        return [], 0.0

    chord_midpoint = calculate_midpoint(p_start, p_end)
    dist_to_center_sq = radius**2 - half_chord**2
    dist_to_center = math.sqrt(abs(dist_to_center_sq))
    bearing_chord = calculate_initial_bearing(p_start, p_end)
    bearing_to_center = (bearing_chord - 90 + 360) % 360
    true_center = geodesic(meters=dist_to_center).destination(
        point=chord_midpoint, bearing=bearing_to_center
    )
    start_bearing = calculate_initial_bearing(true_center, p_start)
    angle_step = arc_degrees_total / num_steps

    last_travel_bearing = 0
    for i in range(num_steps):
        current_bearing = (start_bearing - i * angle_step + 360) % 360
        new_point = geodesic(meters=radius).destination(
            point=true_center, bearing=current_bearing
        )
        travel_bearing = (current_bearing - 90 + 360) % 360
        points.append((new_point, travel_bearing))
        last_travel_bearing = travel_bearing

    points.append((p_end, last_travel_bearing))
    return points, arc_length
