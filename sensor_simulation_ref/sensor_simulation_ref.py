from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class SensorSample:
    """单个离线传感器采样点。"""

    timestamp: float
    state: str
    latitude: float
    longitude: float
    speed_mps: float
    pace_min_per_km: float
    cadence_spm: float
    step_count: int
    acceleration_x: float
    acceleration_y: float
    acceleration_z: float


class RunningSensorSimulator:
    """生成可复现的合成跑步传感器数据。"""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        seed: int = 20260914,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.random = random.Random(seed)
        self.time = 0.0
        self.step_count = 0
        self.distance_m = 0.0
        self.cadence_spm = 80.0
        self.speed_mps = 0.8

    def _target_state(
        self,
        timestamp: float,
    ) -> tuple[str, float, float]:
        """返回当前阶段、目标速度和目标步频。"""
        if timestamp < 20:
            return "warmup", 1.5, 125.0
        if timestamp < 80:
            return "steady", 2.8, 168.0
        if timestamp < 105:
            return "accelerating", 3.6, 180.0
        if timestamp < 135:
            return "slowing", 2.2, 155.0
        return "cooldown", 1.0, 120.0

    @staticmethod
    def _smooth(current: float, target: float, response: float) -> float:
        return current + (target - current) * response

    def _advance_position(self, distance_m: float) -> None:
        """沿正北方向推进，仅用于生成离线轨迹。"""
        meters_per_degree_lat = 111_320.0
        self.latitude += distance_m / meters_per_degree_lat

    def _make_acceleration(
        self,
        timestamp: float,
        cadence_spm: float,
        state: str,
    ) -> tuple[float, float, float]:
        """生成带步态周期和有限噪声的三轴加速度。"""
        step_frequency_hz = cadence_spm / 60.0
        phase = timestamp * step_frequency_hz * 2.0 * math.pi

        vertical = 9.81 + 1.25 * math.sin(phase)
        vertical += 0.35 * math.sin(2.0 * phase + 0.4)
        horizontal_x = 0.18 * math.sin(phase + 0.7)
        horizontal_y = 0.12 * math.cos(phase * 0.5)

        if state == "accelerating":
            horizontal_y += 0.08

        noise = self.random.gauss
        return (
            horizontal_x + noise(0.0, 0.035),
            horizontal_y + noise(0.0, 0.035),
            vertical + noise(0.0, 0.08),
        )

    def generate(
        self,
        duration_s: float,
        sample_rate_hz: float = 10.0,
    ) -> Iterator[SensorSample]:
        """按指定时长和频率生成采样点。"""
        if duration_s <= 0:
            raise ValueError("duration_s must be greater than zero")
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be greater than zero")

        interval = 1.0 / sample_rate_hz
        sample_count = int(duration_s * sample_rate_hz)
        step_phase = 0.0

        for _ in range(sample_count):
            state, target_speed, target_cadence = self._target_state(self.time)
            self.speed_mps = self._smooth(
                self.speed_mps,
                target_speed,
                response=0.08,
            )
            self.cadence_spm = self._smooth(
                self.cadence_spm,
                target_cadence,
                response=0.12,
            )

            distance_delta = self.speed_mps * interval
            self.distance_m += distance_delta
            self._advance_position(distance_delta)

            step_phase += self.cadence_spm / 60.0 * interval
            completed_steps = int(step_phase)
            if completed_steps:
                self.step_count += completed_steps
                step_phase -= completed_steps

            acceleration = self._make_acceleration(
                self.time,
                self.cadence_spm,
                state,
            )
            pace = 1000.0 / (self.speed_mps * 60.0)

            yield SensorSample(
                timestamp=round(self.time, 3),
                state=state,
                latitude=round(self.latitude, 7),
                longitude=round(self.longitude, 7),
                speed_mps=round(self.speed_mps, 4),
                pace_min_per_km=round(pace, 3),
                cadence_spm=round(self.cadence_spm, 2),
                step_count=self.step_count,
                acceleration_x=round(acceleration[0], 4),
                acceleration_y=round(acceleration[1], 4),
                acceleration_z=round(acceleration[2], 4),
            )

            self.time += interval


def write_json(samples: list[SensorSample], output_path: Path) -> None:
    """将采样点写入 JSON 文件。"""
    output_path.write_text(
        json.dumps(
            [asdict(sample) for sample in samples],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_csv(samples: list[SensorSample], output_path: Path) -> None:
    """将采样点写入 CSV 文件。"""
    if not samples:
        return

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=asdict(samples[0]).keys(),
        )
        writer.writeheader()
        writer.writerows(asdict(sample) for sample in samples)


def main() -> None:
    simulator = RunningSensorSimulator(
        latitude=22.687253,
        longitude=114.204035,
    )
    samples = list(simulator.generate(duration_s=150, sample_rate_hz=10))

    write_json(samples, Path("synthetic_running_samples.json"))
    write_csv(samples, Path("synthetic_running_samples.csv"))

    print(f"generated {len(samples)} samples")
    print(f"steps: {samples[-1].step_count}")
    print(f"distance: {simulator.distance_m:.1f} m")


if __name__ == "__main__":
    main()
