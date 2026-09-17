"""LDPlayer console discovery and location control."""

import math
import os
import subprocess
import threading

try:
    import psutil
except ImportError:
    print("警告: 未找到 'psutil' 库。")
    print("请运行 'pip install psutil' 来启用自动检测模拟器目录功能。")
    psutil = None


def find_leidian_path():
    """Return the directory containing the running LDPlayer process."""
    if psutil is None or os.name != "nt":
        return None

    for proc in psutil.process_iter(
        attrs=["pid", "name", "exe"],
        ad_value=None,
    ):
        try:
            process_name = proc.info.get("name")
            exe_path = proc.info.get("exe")
            if process_name == "dnplayer.exe":
                if exe_path:
                    return os.path.dirname(exe_path)
        except psutil.Error:
            pass
    return None


def find_ldconsole_path(ld_folder_path):
    """Find the LDPlayer console executable in the installation directory."""
    for executable_name in ("ldconsole.exe", "dnconsole.exe"):
        executable_path = os.path.join(ld_folder_path, executable_name)
        if os.path.isfile(executable_path):
            return executable_path
    raise FileNotFoundError(
        f"在目录中未找到 ldconsole.exe（雷电模拟器 14 控制台程序）: {ld_folder_path}"
    )


def create_hidden_startupinfo():
    """Create Windows startup settings that hide the console window."""
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def launch_emulator(console_exe_path, emulator_index, startupinfo):
    """Launch an emulator instance without blocking the simulation thread."""
    launch_command = [console_exe_path, "launch", "--index", str(emulator_index)]
    launch_process = subprocess.Popen(
        launch_command,
        startupinfo=startupinfo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if launch_process.poll() is not None and launch_process.returncode != 0:
        raise RuntimeError(
            f"无法启动雷电模拟器实例（退出码 {launch_process.returncode}）"
        )
    return launch_process


def set_emulator_location(
    console_exe_path, emulator_index, lat, lon, startupinfo
):
    """Set the emulator GPS location (ldconsole expects longitude first)."""
    # 雷电控制台的 locate 命令使用的是 --LLI 经度,纬度 的顺序，
    # 这和常见的 lat, lon 表示顺序相反；如果顺序写反，模拟器会把位置偏移到错误的地方。
    command = [
        console_exe_path,
        "locate",
        "--index", str(emulator_index),
        "--LLI", f"{lon},{lat}",
    ]
    result = subprocess.run(
        command,
        startupinfo=startupinfo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"locate API 执行失败（退出码 {result.returncode}）"
            + (f": {details}" if details else "")
        )


def send_ldconsole_action(console_exe_path, emulator_index, action_name, value, startupinfo=None, timeout=4):
    """Send a generic LDPlayer action command like gravity or compass injection.

    Typical example:
        ldconsole.exe action --index 0 --key call.gravity --value x,y,z
    """
    if startupinfo is None:
        startupinfo = create_hidden_startupinfo()

    action_names = [action_name]
    if isinstance(action_name, str):
        if action_name.startswith("call."):
            action_names.append(action_name.replace("call.", "call_", 1))
        elif action_name.startswith("call_"):
            action_names.append(action_name.replace("call_", "call.", 1))

    last_error = None
    for candidate_name in action_names:
        command = [
            console_exe_path,
            "action",
            "--index", str(emulator_index),
            "--key", candidate_name,
            "--value", str(value),
        ]
        result = subprocess.run(
            command,
            startupinfo=startupinfo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode == 0:
            return result
        last_error = result

    details = (last_error.stderr or last_error.stdout).strip() if last_error else ""
    raise RuntimeError(
        f"action API 执行失败（{action_name}，退出码 {getattr(last_error, 'returncode', 'unknown')}）"
        + (f": {details}" if details else "")
    )


def _resolve_adb_path():
    """Locate adb.exe in the environment or Android SDK path."""
    candidates = []
    adb_on_path = __import__("shutil").which("adb")
    if adb_on_path:
        candidates.append(adb_on_path)

    android_sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_sdk:
        candidates.extend([
            os.path.join(android_sdk, "platform-tools", "adb.exe"),
            os.path.join(android_sdk, "platform-tools", "adb"),
        ])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _run_adb_command(args, timeout=20):
    """Run an adb command with the host environment's SDK/adb installation."""
    adb_path = _resolve_adb_path()
    if adb_path is None:
        raise FileNotFoundError("未在 PATH 或 ANDROID_HOME 中找到 adb 可执行文件")

    command = [adb_path] + args
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _adb_root_and_check(device_serial=None):
    """Check whether adb root access is available on the target device.

    This is intentionally a best-effort compatibility check. If root access is not available,
    the project keeps using the LDPlayer external-control path and does not attempt app-level
    injection.
    """
    base_args = ["-s", device_serial] if device_serial else []

    try:
        result = _run_adb_command(base_args + ["root"], timeout=15)
        if result.returncode == 0:
            return True
    except Exception:
        pass

    try:
        shell_result = _run_adb_command(base_args + ["shell", "su -c id"], timeout=15)
        if shell_result.returncode == 0 and "uid=0" in shell_result.stdout.lower():
            return True
    except Exception:
        pass

    return False


def _frida_sensor_patch_script():
    """Build the reference project's SensorEventQueue hook with mutable values.

    The reference implementation hooks only the explicitly selected sensor-monitoring
    application. It does not attach to system_server or enumerate arbitrary application
    processes.
    """
    return """
    var cfg = {
      compass: 0.0, pitch: 0.0, roll: 0.0,
      pressure: 1013.25
    };
    Java.perform(function () {
      var Queue = Java.use('android.hardware.SystemSensorManager$SensorEventQueue');
      Queue.dispatchSensorEvent.overload('int', '[F', 'int', 'long').implementation = function(handle, values, accuracy, timestamp) {
        try {
          var ev = this.mSensorsEvents.value.get(handle);
          if (ev !== null && ev.sensor !== null) {
            var type = ev.sensor.value.getType();
            if (type === 3) {
              values[0] = cfg.compass;
              values[1] = cfg.pitch;
              values[2] = cfg.roll;
            }
            if (type === 6) {
              values[0] = cfg.pressure;
            }
          }
        } catch (e) {}
        return this.dispatchSensorEvent(handle, values, accuracy, timestamp);
      };
    });

    rpc.exports = {
      setvalues: function(compass, pressure, pitch, roll) {
        cfg.compass = Number(compass);
        cfg.pressure = Number(pressure);
        cfg.pitch = Number(pitch);
        cfg.roll = Number(roll);
        return JSON.stringify(cfg);
      },
      status: function() { return JSON.stringify(cfg); }
    };
    """


class _FridaSensorBridge:
    """Maintain one Frida session for the explicitly configured sensor app."""

    def __init__(self, device_serial, package_name):
        import frida

        self.device = (
            frida.get_device(device_serial, timeout=5)
            if device_serial
            else frida.get_usb_device(timeout=5)
        )
        process = next(
            (
                item
                for item in self.device.enumerate_processes()
                if item.name == package_name or item.name.startswith(package_name + ":")
            ),
            None,
        )
        if process is None:
            raise RuntimeError(
                f"未找到正在运行的传感器检测 App 进程: {package_name}"
            )
        self.session = self.device.attach(process.pid)
        self.script = self.session.create_script(_frida_sensor_patch_script())
        self.script.load()
        self.lock = threading.Lock()

    def set_values(self, compass, pressure, orientation):
        pitch = orientation[1] if orientation is not None else 0.0
        roll = orientation[2] if orientation is not None else 0.0
        with self.lock:
            return self.script.exports_sync.setvalues(
                float(compass or 0.0),
                float(pressure),
                float(pitch),
                float(roll),
            )


_frida_bridge = None
_frida_bridge_key = None


def _try_adb_sensor_fallback(device_serial=None, *, compass=None, altitude=None, gravity=None, orientation=None, target_package=None):
    """Update the selected sensor app through the reference project's Frida hook."""
    try:
        import frida
    except Exception:
        return False, "adb fallback unavailable: frida not installed"

    package_name = target_package or os.environ.get("SENSOR_TARGET_PACKAGE")
    if not package_name:
        return False, "未配置 SENSOR_TARGET_PACKAGE，未启用 App 传感器 hook"

    if not _adb_root_and_check(device_serial):
        return False, "adb fallback unavailable: no root access"

    if altitude is None:
        altitude = 0.0
    pressure = 1013.25 * math.pow(max(0.01, 1.0 - float(altitude) / 44330.0), 5.255)

    try:
        global _frida_bridge, _frida_bridge_key
        bridge_key = (device_serial, package_name)
        if _frida_bridge is None or _frida_bridge_key != bridge_key:
            _frida_bridge = _FridaSensorBridge(device_serial, package_name)
            _frida_bridge_key = bridge_key
        # Gravity is intentionally excluded: acceleration must use ldconsole only.
        _frida_bridge.set_values(compass, pressure, orientation)
        return True, "adb fallback active"
    except Exception as exc:
        _frida_bridge = None
        _frida_bridge_key = None
        return False, f"adb fallback failed: {exc}"


def set_sensor_values(console_exe_path, emulator_index, *, compass=None, altitude=None, gravity=None, orientation=None, startupinfo=None, adb_device_serial=None, sensor_target_package=None):
    """Inject sensor values for the emulator environment.

    Direction and altitude are optionally mirrored into the explicitly configured sensor
    monitoring app through the reference project's SensorEventQueue hook. Gravity remains
    available through the external LDPlayer action API.

    The old external LDPlayer action path remains as a fallback for gravity or when the rooted
    platform or Frida bridge is unavailable.
    """
    if startupinfo is None:
        startupinfo = create_hidden_startupinfo()

    # Prefer system-level sensor event replacement for compass / altitude / orientation because it
    # is the only way to make sensor-reading apps observe the new values reliably when LDPlayer
    # action keys are not supported by that specific emulator build.
    system_fallback_ok, system_fallback_msg = _try_adb_sensor_fallback(
        adb_device_serial,
        compass=compass,
        altitude=altitude,
        gravity=gravity,
        orientation=orientation,
        target_package=sensor_target_package,
    )
    if system_fallback_ok:
        return True

    last_error = None
    try:
        if compass is not None:
            send_ldconsole_action(
                console_exe_path,
                emulator_index,
                "call.compass",
                _normalize_scalar_value(compass),
                startupinfo=startupinfo,
            )

        if altitude is not None:
            send_ldconsole_action(
                console_exe_path,
                emulator_index,
                "call.altitude",
                _normalize_scalar_value(altitude),
                startupinfo=startupinfo,
            )

        if gravity is not None:
            send_ldconsole_action(
                console_exe_path,
                emulator_index,
                "call.gravity",
                _normalize_vector_value(gravity),
                startupinfo=startupinfo,
            )

        if orientation is not None:
            send_ldconsole_action(
                console_exe_path,
                emulator_index,
                "call.orientation",
                _normalize_vector_value(orientation),
                startupinfo=startupinfo,
            )

        return True
    except Exception as exc:
        last_error = exc

    if last_error is not None:
        raise RuntimeError(
            "LDPlayer sensor injection failed and the system-level compatibility fallback is unavailable: "
            f"{system_fallback_msg}"
        ) from last_error
    raise RuntimeError(
        "LDPlayer sensor injection failed and the system-level compatibility fallback is unavailable: "
        f"{system_fallback_msg}"
    )


def _normalize_scalar_value(value):
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            raise ValueError("scalar sensor value cannot be empty")
        value = value[0]
    return str(float(value))


def _normalize_vector_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ValueError("vector sensor value must contain exactly 3 numbers")
        return ",".join(str(float(v)) for v in value)
    raise TypeError("vector sensor value must be a sequence like (x, y, z)")


# Keep the original private helper names available to callers that used them.
_find_ldconsole_path = find_ldconsole_path
_create_hidden_startupinfo = create_hidden_startupinfo
_set_emulator_location = set_emulator_location
_set_sensor_values = set_sensor_values
_send_ldconsole_action = send_ldconsole_action
