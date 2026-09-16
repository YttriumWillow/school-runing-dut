"""LDPlayer console discovery and location control."""

import os
import subprocess

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


# Keep the original private helper names available to callers that used them.
_find_ldconsole_path = find_ldconsole_path
_create_hidden_startupinfo = create_hidden_startupinfo
_set_emulator_location = set_emulator_location
