# school-runing-dut

一个基于 Python/Tkinter 和雷电模拟器的轨迹模拟控制器。程序根据四个坐标点生成由直道和圆弧组成的闭合路径，并通过雷电模拟器控制台接口向模拟器发送位置。

> 请仅在获得授权的测试环境中使用本项目，并遵守学校、平台及当地相关规定。项目不保证任何第三方应用对模拟定位数据的接受情况。

## 功能

- 使用四个 WGS-84 坐标点描述一圈路径：
  - `P1`：左下 / 西南
  - `P2`：右下 / 东南
  - `P3`：右上 / 东北
  - `P4`：左上 / 西北
- 自动生成：
  - `P1 -> P2` 直道
  - `P2 -> P3` 圆弧
  - `P3 -> P4` 直道
  - `P4 -> P1` 圆弧
- 支持设置总距离、路径点间距和圆弧角度。
- 支持固定配速或平滑随机配速。
- 支持路径南北、东西方向的整体偏移。
- 支持按几率添加横向随机 GPS 偏移。
- 运行过程中可以暂停，并使用方向键或界面按钮进行人工移动。
- 保存最后位置，下次启动时可从最近路径点继续。
- 自动检测正在运行的雷电模拟器目录。
- 坐标预设从脚本同目录的 `areas.json` 加载，可自行添加区域。
- 使用雷电模拟器 14 的 `ldconsole.exe` 接口启动实例并设置位置。

## 运行环境

- Windows
- Python 3.10 或更高版本
- 雷电模拟器 14
- 雷电模拟器实例已创建，且索引与程序中填写的索引一致

程序使用 Python 自带的 Tkinter 图形界面。Windows 官方 Python 安装包通常已经包含 Tkinter。

## 安装

在项目目录打开 PowerShell：

```powershell
cd D:\path\to\school-runing-dut
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

如果 PowerShell 不允许执行激活脚本，可以不激活环境，直接使用虚拟环境解释器：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

当前依赖见 [requirements.txt](./requirements.txt)：

- `geopy`：计算地理距离、方位和插值点
- `psutil`：自动检测雷电模拟器进程

## 启动

激活虚拟环境后运行：

```powershell
python .\track_simulation_controller.py
```

或直接运行：

```powershell
.\.venv\Scripts\python.exe .\track_simulation_controller.py
```

## 自动发布 Windows x64 版本

仓库包含 `.github/workflows/windows-release.yml`。向 `main` 分支推送后，GitHub Actions 会在 Windows x64 runner 上：

1. 创建 Python 3.12 x64 环境；
2. 安装 `requirements.txt` 和 PyInstaller；
3. 将程序打包为无控制台窗口的单文件 `.exe`；
4. 把 `areas.json` 和 README 一起放入 ZIP；
5. 自动创建 GitHub Release，并上传 `.exe` 和 ZIP。

也可以在 GitHub Actions 页面手动运行 `Windows x64 Release` 工作流。发布包中的 `track_sim_config.json` 会在首次运行时自动生成，个人路径和位置配置不会被打包进公开 Release。

## 雷电模拟器配置

在程序的“模拟器设置”中填写雷电模拟器安装目录，例如：

```text
D:\LDPlayer\LDPlayer14
```

程序会按以下顺序查找控制台程序：

1. `ldconsole.exe`
2. `dnconsole.exe`（兼容旧版本）

自动启动实例时使用：

```text
ldconsole.exe launch --index <模拟器索引>
```

每个轨迹点使用雷电控制台的定位接口：

```text
ldconsole.exe locate --index <模拟器索引> --LLI <经度>,<纬度>
```

注意 `--LLI` 的顺序是**经度在前、纬度在后**，而界面坐标输入显示为纬度和经度。定位命令的退出码、输出和超时都会被检查，失败时会显示错误信息。

## 坐标预设：`areas.json`

`areas.json` 必须和 `track_simulation_controller.py` 位于同一目录。程序启动时会读取它，并校验文件必须是非空 JSON 对象，且每个预设至少包含以下字段：

```json
{
    "区域名称": {
        "p1_lat": "纬度",
        "p1_lon": "经度",
        "p2_lat": "纬度",
        "p2_lon": "经度",
        "p3_lat": "纬度",
        "p3_lon": "经度",
        "p4_lat": "纬度",
        "p4_lon": "经度",
        "offset_ns": "0.0",
        "offset_ew": "0.0"
    }
}
```

其中：

- `p1_*` 至 `p4_*` 是四个路径顶点。
- `offset_ns` 是南北方向偏移，正数向北，负数向南。
- `offset_ew` 是东西方向偏移，正数向东，负数向西。
- 两个偏移字段可以省略，程序会按 `0.0` 处理。

文件缺失、JSON 格式错误或坐标字段缺失时，程序会在启动阶段报告错误。

## 参数说明

### 模拟参数

- **总距离（米）**：要模拟的总距离。
- **路径点间距（米）**：相邻定位点之间的距离。数值越小，定位命令越密集。
- **圆弧角度（度）**：两个弯道使用的圆弧角度，必须大于 `0` 且小于 `360`。

### 配速

- 未启用平滑配速时，使用固定的分钟/公里配速。
- 启用平滑配速后：
  - **基础配速**：平均分钟/公里。
  - **变异率**：配速随机变化范围。
  - **变化平滑度**：配速逐渐变化所使用的时间范围。

### 随机偏移

启用后，部分路径点会按照当前行进方向的横向方向添加随机偏移：

- **偏移几率**：每个点产生随机偏移的概率。
- **左/右最大偏移**：横向偏移的最大米数。

### 暂停和人工控制

点击“暂停”后，程序会停止自动发送路径点。此时可以：

- 点击界面中的上下左右按钮；
- 使用键盘方向键；
- 每次移动默认 `5` 米。

点击“继续”后，程序回到自动路径，并以暂停前的路径点作为恢复基准。

## 配置文件

程序会在脚本同目录读写 `track_sim_config.json`，保存：

- 雷电模拟器目录；
- 最近使用的坐标预设；
- 路径偏移；
- 随机偏移参数；
- 平滑配速参数；
- 最近一次位置。

该文件属于本地运行状态，不建议提交个人机器路径或位置数据。

## 常见问题

### 找不到 `ldconsole.exe`

确认填写的是雷电模拟器安装目录，而不是某个实例目录。也可以点击“浏览...”选择目录。雷电模拟器正在运行时，程序还会尝试通过 `dnplayer.exe` 自动检测安装目录。

### 定位命令执行失败

请检查：

1. 控制台程序是否来自雷电模拟器 14；
2. 模拟器索引是否正确；
3. 目标实例是否可以正常启动；
4. `ldconsole.exe help` 是否包含 `locate` 命令；
5. 是否有其他程序或模拟器设置阻止位置模拟。

### 启动时找不到预设

确认 `areas.json` 与脚本在同一目录，并且 JSON 编码为 UTF-8。预设名称和坐标字段必须符合上面的格式。

### GUI 无法启动

确认使用的是 Windows Python，并检查 Tkinter：

```powershell
.\.venv\Scripts\python.exe -c "import tkinter; print('Tkinter OK')"
```

## 项目文件

```text
track_simulation_controller.py  主程序和 Tkinter 界面
areas.json                     坐标预设
track_sim_config.json          本机运行配置
requirements.txt               Python 依赖
.venv/                         本地虚拟环境，不提交到 Git
```

## 开发检查

修改代码后可以运行：

```powershell
.\.venv\Scripts\python.exe -m py_compile .\track_simulation_controller.py
git diff --check
```
