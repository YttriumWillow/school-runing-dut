"""Application paths and coordinate preset loading."""

import json
import os
import sys


# Keep data files next to the script, or next to the executable when frozen.
BASE_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "track_sim_config.json")
AREAS_FILE = os.path.join(BASE_DIR, "areas.json")
ICON_FILE = os.path.join(RESOURCE_DIR, "exit-icon.ico")
ICON_PNG_FILE = os.path.join(RESOURCE_DIR, "exit-icon.png")

DEFAULT_PRESETS = {
    "示例跑道 (自动创建)": {
        "p1_lat": "31.230400",
        "p1_lon": "121.473700",
        "p2_lat": "31.231300",
        "p2_lon": "121.473700",
        "p3_lat": "31.231300",
        "p3_lon": "121.474500",
        "p4_lat": "31.230400",
        "p4_lon": "121.474500",
        "offset_ns": "0.0",
        "offset_ew": "0.0",
    }
}


def create_default_presets():
    """Create a standalone example preset file when areas.json is missing."""
    with open(AREAS_FILE, "w", encoding="utf-8") as file:
        json.dump(DEFAULT_PRESETS, file, ensure_ascii=False, indent=4)
        file.write("\n")
    return DEFAULT_PRESETS


def load_presets():
    """Load coordinate presets from areas.json beside the application."""
    try:
        with open(AREAS_FILE, "r", encoding="utf-8") as file:
            presets = json.load(file)
    except FileNotFoundError:
        return create_default_presets()
    except json.JSONDecodeError as exc:
        raise ValueError(f"坐标预设文件格式错误: {AREAS_FILE}: {exc}") from exc

    if not isinstance(presets, dict) or not presets:
        raise ValueError("坐标预设文件必须是非空 JSON 对象。")

    required_fields = {
        "p1_lat", "p1_lon", "p2_lat", "p2_lon",
        "p3_lat", "p3_lon", "p4_lat", "p4_lon"
    }
    for name, preset in presets.items():
        if not isinstance(name, str) or not isinstance(preset, dict):
            raise ValueError("坐标预设格式错误：名称必须是字符串，内容必须是对象。")
        missing_fields = required_fields - preset.keys()
        if missing_fields:
            missing = ", ".join(sorted(missing_fields))
            raise ValueError(f"坐标预设“{name}”缺少字段: {missing}")

    return presets


PRESETS = load_presets()
