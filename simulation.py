"""Simulation worker thread and manual location control."""

import math
import queue
import random
import threading
import time

from geopy.distance import geodesic

from ldplayer import (
    create_hidden_startupinfo,
    find_ldconsole_path,
    launch_emulator,
    send_ldconsole_action,
    set_emulator_location,
    set_sensor_values,
)
from stride_model import acceleration_sample, build_stride_state


def _motion_state_for_speed(speed_mps: float) -> str:
    """Map current GPS movement speed to a running state.

    The app's step detector is much more sensitive to abnormal cadence/impact spikes than
    a human runner would be, so the state thresholds are intentionally conservative and more
    centered around a realistic, healthy running rhythm instead of aggressive sprint values.
    """
    if speed_mps < 1.2:
        return "warmup"
    if speed_mps < 2.2:
        return "steady"
    if speed_mps < 3.4:
        return "run"
    return "sprint"


def _cadence_for_state(speed_mps: float, state: str) -> float:
    """Return the reference project's running cadence for the current speed state.

    The reference implementation uses approximately 125 spm during warmup, 168 spm
    during steady running, 180 spm while accelerating, 155 spm while slowing down,
    and 120 spm during cooldown.  Keeping the steady value near 168 spm is important:
    it is the cadence at which the reference project reliably triggers step detection.
    Speed still selects the state, so the cadence remains coupled to the GPS movement.
    """
    if state == "warmup":
        return 125.0
    if state == "steady":
        return 168.0
    if state == "run":
        return 180.0
    return 180.0


def _compute_gait_cycle(speed_mps: float, cadence_spm: float, heading_deg: float, time_offset: float, state: str) -> tuple[float, float, float]:
    """Return a more realistic gravity vector for a running gait cycle.

    The model reproduces a running stride as a periodic waveform:
    - vertical force peaks around heel strike and mid-stance
    - forward acceleration oscillates with the step cycle
    - lateral drift is small but non-zero to make the app feel like real human motion
    """
    cycle_hz = cadence_spm / 60.0
    cycle_phase = (time_offset * cycle_hz) % 1.0
    theta = math.radians(heading_deg)

    # 真正的跑步不是单一的正弦波，而是一个具有“落地->支撑->摆动”的周期，
    # 因此使用相位偏移后的多波叠加，模拟前后、左右和垂直方向的更真实冲击。
    vertical_wave = math.sin(2.0 * math.pi * cycle_phase)
    forward_wave = math.sin(2.0 * math.pi * cycle_phase - math.pi / 2.0)
    lateral_wave = math.sin(2.0 * math.pi * cycle_phase + math.pi / 2.0)
    braking_wave = max(0.0, -math.sin(2.0 * math.pi * cycle_phase))

    state_factor = {"warmup": 0.75, "steady": 1.0, "run": 1.3, "sprint": 1.6}[state]
    forward_accel = state_factor * (1.8 * forward_wave + 0.45 * speed_mps + 0.6 * braking_wave)
    lateral_accel = 0.40 * lateral_wave
    vertical_accel = 9.81 + state_factor * (0.95 * vertical_wave + 0.26 * speed_mps)

    x = math.sin(theta) * lateral_accel + math.cos(theta) * forward_accel * 0.18
    y = math.cos(theta) * forward_accel + math.sin(theta) * lateral_accel * 0.35
    z = vertical_accel
    return round(x, 3), round(y, 3), round(z, 3)


def _emit_step_sensor_packet(console_exe_path, emulator_index, startupinfo, heading_deg, speed_mps, cadence_spm, state, phase_index, phase_total):
    """Emit a single burst of sensor samples that match the current GPS motion state.

    Instead of a single static sensor point per GPS update, we render several sub-samples within
    the same running step. This makes the app see more continuous acceleration patterns and better
    approximates a real running gait.
    """
    base_phase = (phase_index + 1) / float(phase_total)
    time_offset = base_phase
    gravity_vector = _compute_gait_cycle(
        speed_mps=speed_mps,
        cadence_spm=cadence_spm,
        heading_deg=heading_deg,
        time_offset=time_offset,
        state=state,
    )
    altitude = 5.0 + 1.8 * math.sin(base_phase * 2.0 * math.pi * 3.2)
    pitch = 2.5 * math.sin(base_phase * 2.0 * math.pi)
    roll = 1.5 * math.cos(base_phase * 2.0 * math.pi * 1.4)

    set_sensor_values(
        console_exe_path,
        emulator_index,
        compass=heading_deg,
        altitude=altitude,
        gravity=gravity_vector,
        orientation=(heading_deg, pitch, roll),
        startupinfo=startupinfo,
    )


def _signed_bearing_delta(from_bearing: float, to_bearing: float) -> float:
    """Return the signed turn delta in degrees, normalized to [-180, 180]."""
    delta = (to_bearing - from_bearing + 540.0) % 360.0 - 180.0
    return delta


def _turn_context(heading_deg: float, previous_heading: float | None, speed_mps: float) -> tuple[float, float]:
    """Return turn severity and lateral bias for the current GPS path state."""
    if previous_heading is None:
        return 0.0, 0.0

    delta = _signed_bearing_delta(previous_heading, heading_deg)
    turn_strength = abs(delta) / 90.0
    lateral_bias = math.copysign(1.0, delta if delta != 0 else 1.0)
    if speed_mps <= 0:
        return 0.0, 0.0
    return min(1.5, turn_strength * (0.8 + speed_mps / 4.0)), lateral_bias


def _emit_gait_phase_packet(console_exe_path, emulator_index, startupinfo, heading_deg, speed_mps, cadence_spm, state, phase_name, phase_fraction, previous_heading=None):
    """Emit one phase of the running cycle: strike, stance, toe-off, swing.

    Each phase changes acceleration amplitude and orientation in a different way so that the app
    sees a plausible body movement rather than a uniform artificially smooth signal.
    """
    # Keep the gait envelope smoother and less aggressive than the previous sports-style model.
    # The step detector in the target app is sensitive to over-sharp impact bursts, so we bias the
    # amplitude toward a normal human running signature rather than a hard-impact training signal.
    phase_map = {
        "strike": {"boost": 0.95, "pitch": 2.5, "roll": 1.2},
        "stance": {"boost": 0.85, "pitch": 1.5, "roll": 0.8},
        "toe_off": {"boost": 1.05, "pitch": -1.2, "roll": -0.7},
        "swing": {"boost": 0.72, "pitch": -2.4, "roll": 0.5},
    }
    config = phase_map[phase_name]
    theta = math.radians(heading_deg)
    state_factor = {"warmup": 0.65, "steady": 0.8, "run": 1.0, "sprint": 1.15}[state]
    cadence_factor = cadence_spm / 180.0
    turn_strength, lateral_bias = _turn_context(heading_deg, previous_heading, speed_mps)

    x = math.sin(theta) * (0.45 * config["roll"] + 0.10 * speed_mps) * config["boost"]
    y = math.cos(theta) * (0.85 * config["pitch"] + 0.35 * speed_mps) * config["boost"]
    z = 9.81 + state_factor * (0.35 * config["boost"] + 0.10 * speed_mps * cadence_factor)

    x += lateral_bias * turn_strength * 0.7
    y += turn_strength * 0.4
    z += turn_strength * 0.25

    phase_offset = phase_fraction * 2.0 * math.pi
    x += 0.18 * math.sin(phase_offset)
    y += 0.12 * math.cos(phase_offset + 1.0)
    z += 0.28 * math.sin(phase_offset * 2.0)

    gravity_vector = (round(max(-2.5, min(2.5, x)), 3), round(max(-2.5, min(2.5, y)), 3), round(max(8.5, min(12.0, z)), 3))
    altitude = 5.0 + 0.9 * math.sin(phase_fraction * 2.0 * math.pi * 3.0)
    pitch = config["pitch"] * (0.7 + 0.15 * math.sin(phase_offset)) + turn_strength * 1.5
    roll = config["roll"] * (0.8 + 0.08 * math.cos(phase_offset)) + turn_strength * 1.0 * lateral_bias

    set_sensor_values(
        console_exe_path,
        emulator_index,
        compass=heading_deg,
        altitude=altitude,
        gravity=gravity_vector,
        orientation=(heading_deg, pitch, roll),
        startupinfo=startupinfo,
    )


def _emit_gait_cycle_stream(console_exe_path, emulator_index, startupinfo, heading_deg, speed_mps, cadence_spm, state, previous_heading=None):
    """Emit a short, continuous sensor stream within one step cycle.

    Real running sensors are not static snapshots. A single GPS update is better simulated as
    a mini burst of sensor readings that sweep through a complete gait cycle with a realistic
    turn-aware acceleration pattern.
    """
    gait_phases = [
        ("strike", 0.00),
        ("stance", 0.25),
        ("toe_off", 0.55),
        ("swing", 0.85),
    ]
    for phase_name, phase_fraction in gait_phases:
        try:
            _emit_gait_phase_packet(
                console_exe_path,
                emulator_index,
                startupinfo,
                heading_deg,
                speed_mps,
                cadence_spm,
                state,
                phase_name,
                phase_fraction,
                previous_heading=previous_heading,
            )
        except Exception:
            pass

    # Add a short interpolated burst between the phase envelope points to make the signal less
    # step-like and closer to a continuous real-world accelerometer trace.
    for sub_phase in [0.12, 0.38, 0.68, 0.92]:
        try:
            _emit_gait_phase_packet(
                console_exe_path,
                emulator_index,
                startupinfo,
                heading_deg,
                speed_mps,
                cadence_spm,
                state,
                "stance" if sub_phase < 0.5 else "swing",
                sub_phase,
                previous_heading=previous_heading,
            )
        except Exception:
            pass


def _build_sensor_payload(
    heading_deg,
    speed_mps,
    cadence_spm,
    state,
    previous_heading=None,
    elapsed_seconds=None,
):
    """Create a single realistic sensor payload for the current motion state.

    This intentionally keeps a single GPS update to a single sensor burst so the worker thread
    remains lightweight. The payload still reflects heading, speed, cadence and turn bias, but
    avoids the expensive repeated step-wave emission that slowed the whole simulation loop.
    """
    state_factor = {"warmup": 0.65, "steady": 0.8, "run": 1.0, "sprint": 1.15}[state]
    turn_strength, lateral_bias = _turn_context(heading_deg, previous_heading, speed_mps)
    theta = math.radians(heading_deg)

    # The reference project drives the accelerometer with a periodic running waveform:
    # a dominant step-frequency component, a second harmonic, and small bounded noise.
    # Use elapsed simulation time rather than the GPS point index so changing GPS
    # intervals does not change the apparent cadence.
    if elapsed_seconds is None:
        elapsed_seconds = time.monotonic()
    phase = 2.0 * math.pi * (cadence_spm / 60.0) * elapsed_seconds
    vertical_wave = math.sin(phase) + 0.28 * math.sin(2.0 * phase + 0.4)
    forward_wave = math.sin(phase - math.pi / 2.0)
    lateral_wave = math.cos(phase + 0.7)
    cadence_phase = 0.5 + 0.5 * math.sin(phase)

    forward_accel = state_factor * (0.95 * forward_wave + 0.20 * speed_mps)
    lateral_accel = state_factor * (0.25 * lateral_wave)
    vertical_accel = 9.81 + state_factor * (1.25 * vertical_wave)

    x = math.sin(theta) * lateral_accel + math.cos(theta) * forward_accel
    y = math.cos(theta) * forward_accel + math.sin(theta) * lateral_accel
    z = vertical_accel

    # Turning adds a small lateral bias without changing the step frequency.
    x += math.sin(theta) * turn_strength * lateral_bias * 0.35
    y += math.cos(theta) * turn_strength * 0.20
    z += 0.10 * math.sin(phase * 0.5)

    gravity = (round(max(-2.2, min(2.2, x)), 3), round(max(-2.2, min(2.2, y)), 3), round(max(8.8, min(11.2, z)), 3))
    altitude = 5.0 + 0.9 * math.sin(phase * 0.12)
    pitch = 1.4 * math.sin(phase) + turn_strength * 1.3
    roll = 1.0 * math.cos(phase * 0.7) - lateral_bias * turn_strength * 0.8

    return {
        "compass": heading_deg,
        "altitude": altitude,
        "gravity": gravity,
        "orientation": (heading_deg, pitch, roll),
    }


def _sensor_worker_loop(
    console_exe_path,
    emulator_index,
    startupinfo,
    sensor_state,
    state_lock,
    stop_event,
):
    """Emit acceleration through ldconsole at the reference project's 50 Hz rate."""
    next_sample = time.monotonic()
    gait_start = next_sample
    while not stop_event.is_set():
        now = time.monotonic()
        if now < next_sample:
            stop_event.wait(next_sample - now)
            continue
        with state_lock:
            stride_state = sensor_state["stride"]
        try:
            gravity = acceleration_sample(
                stride_state,
                elapsed_seconds=now - gait_start,
            )
            # Acceleration is deliberately injected through this external API only.
            send_ldconsole_action(
                console_exe_path,
                emulator_index,
                "call.gravity",
                ",".join(str(value) for value in gravity),
                startupinfo=startupinfo,
            )
        except Exception:
            pass
        next_sample += 0.02
        if next_sample < time.monotonic() - 0.1:
            next_sample = time.monotonic()


stop_simulation_event = threading.Event()
pause_event = threading.Event()
skip_wait_event = threading.Event()


def send_manual_location(app, lat, lon):
    """Send a single location update without blocking the GUI."""
    try:
        startupinfo = create_hidden_startupinfo()
        ld_folder_path = app.ld_folder_path.get()
        emulator_index = app.emulator_index.get()
        console_exe_path = find_ldconsole_path(ld_folder_path)
        set_emulator_location(
            console_exe_path, emulator_index, lat, lon, startupinfo
        )
        app.status_label.config(text=f"手动移动到: {lat:.6f}, {lon:.6f}")
        app.coords_label.config(text=f"当前坐标: {lat:.6f}, {lon:.6f}")
    except Exception as e:
        app.status_label.config(text=f"手动移动失败: {e}", foreground="red")


def run_simulation_thread(
    status_queue,
    app,
    ld_folder_path,
    emulator_index,
    points_list,
    pace_info,
    step_m,
    random_offset_info,
):
    """Run the simulation in a worker thread using the LDPlayer API."""
    # 这个函数在后台线程中执行，避免阻塞 Tkinter 的主事件循环。
    # 通过 status_queue 与 GUI 线程通信，确保状态文本、进度更新和错误提示都在主线程中刷新。
    last_path_point = None
    sensor_worker = None
    sensor_stop_event = None

    try:
        total_points = len(points_list)
        startupinfo = create_hidden_startupinfo()

        if not ld_folder_path:
            raise Exception("请提供雷电模拟器安装目录。")

        console_exe_path = find_ldconsole_path(ld_folder_path)
        initial_skip = skip_wait_event.is_set()

        if not initial_skip:
            status_queue.put(
                ("STATUS", f"正在启动模拟器 (索引 {emulator_index})...", "blue")
            )
            # 真正的模拟器启动动作在这里发生：
            # 对于雷电控制台，launch 仅负责启动某个实例，不负责维护 GUI 线程。
            launch_emulator(console_exe_path, emulator_index, startupinfo)

            status_queue.put(("ENABLE_SKIP", True, None))
            for i in range(23):
                if skip_wait_event.is_set():
                    status_queue.put(("STATUS", "已跳过等待...", "blue"))
                    break
                status_queue.put(
                    ("STATUS", f"等待模拟器启动 ({23-i}秒)...", "blue")
                )
                time.sleep(1)
            status_queue.put(("ENABLE_SKIP", False, None))
        else:
            status_queue.put(("STATUS", "已跳过启动等待...", "blue"))
            time.sleep(1)

        skip_wait_event.clear()
        status_queue.put(("STATUS", "连接成功，即将开始模拟...", "blue"))

        # 配速策略分为两种：
        # 1. 固定时速：每个点之间的发送间隔是稳定的；
        # 2. 平滑随机配速：每隔一段时间随机改变目标配速，然后通过指数平滑逐步逼近，
        #    避免“突变式”速度跳变，呈现更像真实跑步节奏。
        sensor_stop_event = threading.Event()
        sensor_state = {
            "stride": build_stride_state(speed_mps=0.0, heading_deg=0.0)
        }
        sensor_state_lock = threading.Lock()
        sensor_worker = threading.Thread(
            target=_sensor_worker_loop,
            args=(
                console_exe_path,
                emulator_index,
                startupinfo,
                sensor_state,
                sensor_state_lock,
                sensor_stop_event,
            ),
            daemon=True,
        )
        sensor_worker.start()

        current_pace = 6.0
        target_pace = 6.0
        min_pace = 5.5
        max_pace = 6.5
        smooth_steps = 30
        is_smooth = False

        if isinstance(pace_info, tuple) and pace_info[0] == "smooth":
            is_smooth = True
            _, base_pace, variability, smoothness = pace_info
            current_pace = base_pace
            target_pace = base_pace
            min_pace = base_pace - variability
            max_pace = base_pace + variability
            avg_delay = step_m / (1000.0 / (base_pace * 60.0))
            smooth_steps = max(1, int(smoothness / avg_delay))
        else:
            const_delay = pace_info

        # 传感器注入的控制逻辑：
        # - compass: 相机/地图的方向角，影响应用层对“跑步朝向”的判断；
        # - gravity: 以 (x, y, z) 形式注入重力/加速度，模拟步态；
        # - altitude: 模拟高程，可用于检测处理海拔变化。
        # 这里按每个定位点更新一次方向和重力，能够让步数/计时器更容易被程序识别为真实跑步。
        loop_start_time = time.time()
        for i, (point, travel_bearing) in enumerate(points_list):
            if stop_simulation_event.is_set():
                status_queue.put(("STATUS", "模拟已手动停止。", "orange"))
                break

            current_offset_ns = app.initial_offset_ns
            current_offset_ew = app.initial_offset_ew
            final_point = point
            if current_offset_ns != 0.0 or current_offset_ew != 0.0:
                final_point = geodesic(meters=current_offset_ns).destination(
                    final_point, bearing=0
                )
                final_point = geodesic(meters=current_offset_ew).destination(
                    final_point, bearing=90
                )

            if random_offset_info:
                chance_pct, max_range_m = random_offset_info
                if random.random() < chance_pct:
                    # 为了让“GPS 抖动”更像真实轨迹，随机偏移不是沿正北/正东，而是沿着
                    # 当前运动方向的横向方向偏移，模拟横向漂移和抖动。
                    offset_dist = random.uniform(-max_range_m, max_range_m)
                    lateral_bearing = (travel_bearing + 90 + 360) % 360
                    final_point = geodesic(meters=offset_dist).destination(
                        final_point, bearing=lateral_bearing
                    )

            last_path_point = final_point
            if not pause_event.is_set():
                app.last_sent_point = final_point
                status_queue.put(
                    ("STATUS", "模拟已暂停。请使用方向键手动控制。", "orange")
                )
                # 线程在这里等待 GUI 恢复，整个模拟会暂时冻结，
                # 这样用户能在 paused 的状态下手动微调当前位置。
                pause_event.wait()
                status_queue.put(("STATUS", "模拟已恢复。", "blue"))
                final_point = last_path_point
                app.last_sent_point = final_point

            if is_smooth:
                if i % smooth_steps == 0:
                    target_pace = random.uniform(min_pace, max_pace)
                current_pace = current_pace * 0.98 + target_pace * 0.02
                current_speed_ms = 1000.0 / (current_pace * 60.0)
                delay = step_m / current_speed_ms
            else:
                delay = const_delay

            app.last_sent_point = final_point
            lon = final_point.longitude
            lat = final_point.latitude
            set_emulator_location(
                console_exe_path, emulator_index, lat, lon, startupinfo
            )

            # 在定位到新坐标后，按当前 GPS 运动状态注入更真实的“朝向 + 步态 + 海拔”组合。
            # 真实跑步时，方向和步态并不是与 GPS 独立的，而是要跟随当前速度和路线方向一起变化；
            # 因此这里把步态的状态机和 GPS 的速度做了绑定，确保传感器输出和运动状态一致。
            heading_deg = float(travel_bearing)
            speed_mps = 1000.0 / (current_pace * 60.0)
            # 更新独立步幅模型；50Hz 加速度线程读取这个快照。
            try:
                with sensor_state_lock:
                    sensor_state["stride"] = build_stride_state(
                        speed_mps=speed_mps,
                        heading_deg=heading_deg,
                    )

                # Direction and altitude remain a low-rate GPS-synchronised update.
                # Acceleration is not sent here; the dedicated worker owns call.gravity.
                set_sensor_values(
                    console_exe_path,
                    emulator_index,
                    compass=heading_deg,
                    altitude=5.0,
                    orientation=(heading_deg, 0.0, 0.0),
                    startupinfo=startupinfo,
                )
            except Exception:
                # 传感器注入可能在某些 LDPlayer 版本上不支持全部 key；
                # 这里不阻断 GPS 区域移动，只记录错误并继续模拟。
                pass
            if i % 10 == 0:
                progress = f"正在模拟: {i+1} / {total_points} 个点"
                coords = f"当前坐标: {lat:.6f}, {lon:.6f}"
                status_queue.put(("UPDATE", progress, coords))

            loop_end_time = loop_start_time + delay
            sleep_time = loop_end_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            loop_start_time = time.time()

        if not stop_simulation_event.is_set():
            status_queue.put(("DONE", "模拟完成！", "green"))
    except Exception as e:
        status_queue.put(("ERROR", f"线程发生未知错误: {e}", None))
    finally:
        if sensor_stop_event is not None:
            sensor_stop_event.set()
        if sensor_worker is not None and sensor_worker.is_alive():
            sensor_worker.join(timeout=1.0)


_send_manual_location = send_manual_location
