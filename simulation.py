"""Simulation worker thread and manual location control."""

import random
import threading
import time

from geopy.distance import geodesic

from ldplayer import (
    create_hidden_startupinfo,
    find_ldconsole_path,
    launch_emulator,
    set_emulator_location,
)


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
    last_path_point = None

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


_send_manual_location = send_manual_location
