"""Running stride model shared by GPS and external gravity injection."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class StrideState:
    """Current movement parameters used to render one sensor sample."""

    speed_mps: float
    heading_deg: float
    cadence_spm: float
    motion_state: str


def motion_state_for_speed(speed_mps: float) -> str:
    """Map GPS speed to the same phases used by the reference simulator."""
    if speed_mps < 1.2:
        return "warmup"
    if speed_mps < 2.2:
        return "steady"
    if speed_mps < 3.4:
        return "accelerating"
    return "sprinting"


def cadence_for_state(state: str) -> float:
    """Return the reference simulator's target cadence for a movement phase."""
    return {
        "warmup": 125.0,
        "steady": 168.0,
        "accelerating": 180.0,
        "sprinting": 180.0,
    }.get(state, 168.0)


def step_length_for_state(speed_mps: float, cadence_spm: float) -> float:
    """Return the implied step length in metres for the current GPS speed."""
    if cadence_spm <= 0:
        raise ValueError("cadence_spm must be greater than zero")
    return speed_mps * 60.0 / cadence_spm


def build_stride_state(speed_mps: float, heading_deg: float) -> StrideState:
    """Build a stride state directly from the current GPS motion."""
    motion_state = motion_state_for_speed(speed_mps)
    return StrideState(
        speed_mps=speed_mps,
        heading_deg=heading_deg % 360.0,
        cadence_spm=cadence_for_state(motion_state),
        motion_state=motion_state,
    )


def acceleration_sample(state: StrideState, elapsed_seconds: float) -> tuple[float, float, float]:
    """Render one reference-project-style acceleration sample in m/s².

    The reference project emits this waveform at 50 Hz. It combines a raised-cosine
    landing pulse, a second harmonic, and small horizontal swing components. Horizontal
    acceleration is rotated into the current GPS heading before being returned.
    """
    phase = 2.0 * math.pi * (state.cadence_spm / 60.0) * elapsed_seconds
    contact = 0.5 - 0.5 * math.cos(phase)
    intensity = {
        "warmup": 0.72,
        "steady": 1.0,
        "accelerating": 1.08,
        "sprinting": 1.12,
    }.get(state.motion_state, 1.0)

    vertical = 9.80665 + intensity * (
        11.0 * contact**6 + 3.2 * math.sin(2.0 * phase - 0.6)
    )
    forward = intensity * (
        5.0 * math.sin(phase + math.pi / 2.0)
        + 4.0 * math.sin(2.0 * phase + 0.4)
    )
    lateral = intensity * (
        2.6 * math.sin(phase + 0.4)
        + 1.3 * math.sin(2.0 * phase + 1.1)
    )

    heading = math.radians(state.heading_deg)
    x = math.sin(heading) * forward + math.cos(heading) * lateral
    y = math.cos(heading) * forward - math.sin(heading) * lateral
    return round(x, 3), round(y, 3), round(vertical, 3)
