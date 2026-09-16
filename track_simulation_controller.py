import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from geopy.distance import geodesic
from geopy.point import Point

from geo_utils import interpolate_arc, interpolate_straight
from ldplayer import find_leidian_path
from settings import CONFIG_FILE, PRESETS
from simulation import (
    pause_event,
    run_simulation_thread,
    send_manual_location,
    skip_wait_event,
    stop_simulation_event,
)


# -----------------------------------------------------------------
# 主 GUI (使用 Tkinter)
# -----------------------------------------------------------------

class TrackSimulatorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("操场实时模拟控制器 (Tkinter版)")
        self.root.geometry("580x800") # (新) 调整窗口大小
        
        self.simulation_thread = None
        self.status_queue = queue.Queue() # 线程通信队列
        
        # (新) 手动控制状态
        self.manual_step_m = 5.0 # 每次手动点击移动 5 米
        self.last_sent_point = None # 存储最后发送的点
        self.last_known_location = None # (新) 用于保存
        
        # (新) 路径微调 (用于线程)
        self.initial_offset_ns = 0.0
        self.initial_offset_ew = 0.0
        
        # --- (新) 创建滚动条框架 ---
        main_canvas = tk.Canvas(root)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=main_canvas.yview)
        main_canvas.configure(yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side="right", fill="y")
        main_canvas.pack(side="left", fill="both", expand=True)
        
        # 这个 frame 包含所有内容
        self.scrollable_frame = ttk.Frame(main_canvas, padding="10")
        
        # (新) 修复: 存储 canvas window item ID
        self.canvas_window_item = main_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        
        # (新) 修复: 分离 canvas 和 frame 的 configure 事件
        def on_frame_configure(event):
            # 当 frame 内部大小改变时, 更新 scrollregion
            main_canvas.configure(scrollregion=main_canvas.bbox("all"))
        
        def on_canvas_configure(event):
            # 当 canvas 窗口大小改变时, 改变 frame 的宽度
            main_canvas.itemconfig(self.canvas_window_item, width=event.width)

        self.scrollable_frame.bind("<Configure>", on_frame_configure)
        main_canvas.bind("<Configure>", on_canvas_configure)
        
        # (新) 绑定鼠标滚轮
        def _on_mouse_wheel(event):
            # (新) 跨平台滚轮支持
            delta = 0
            if event.num == 4: # Linux scroll up
                delta = -1
            elif event.num == 5: # Linux scroll down
                delta = 1
            elif event.delta > 0: # Windows/macOS scroll up
                delta = -1
            elif event.delta < 0: # Windows/macOS scroll down
                delta = 1
            
            main_canvas.yview_scroll(delta, "units")
        
        # 绑定到根窗口，使其随处可用
        self.mouse_wheel_binding_id_1 = self.root.bind_all("<MouseWheel>", _on_mouse_wheel)
        self.mouse_wheel_binding_id_2 = self.root.bind_all("<Button-4>", _on_mouse_wheel)
        self.mouse_wheel_binding_id_3 = self.root.bind_all("<Button-5>", _on_mouse_wheel)

        # --- --- --- --- --- --- ---
        
        
        # --- 创建控件 ---
        # (新) 所有控件的父级改为 self.scrollable_frame
        
        # 1. 模拟器设置
        path_frame = ttk.Labelframe(self.scrollable_frame, text="模拟器设置", padding="5")
        path_frame.pack(fill="x", expand=True)
        
        # (新) 修复布局
        ttk.Label(path_frame, text="雷电模拟器安装目录:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.ld_folder_path = tk.StringVar(value="D:/Program Files/leidian/LDPlayer14")
        self.ld_folder_entry = ttk.Entry(path_frame, textvariable=self.ld_folder_path, width=40) # (新) 固定宽度
        self.ld_folder_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=5, pady=5) # (新) 合并单元格
        ttk.Button(path_frame, text="浏览...", command=self.browse_ld_folder).grid(row=0, column=3, padx=5, pady=5)
        
        ttk.Label(path_frame, text="模拟器索引:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.emulator_index = tk.StringVar(value="1")
        ttk.Entry(path_frame, textvariable=self.emulator_index, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=5)
        
        self.skip_wait_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(path_frame, text="跳过启动等待，直接开始移动", variable=self.skip_wait_var).grid(row=2, column=2, sticky="w", padx=5, pady=5)
        
        # (新) 从上次位置开始
        self.start_from_last_pos_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(path_frame, text="从上次位置开始 (防漂移)", variable=self.start_from_last_pos_var).grid(row=3, column=1, columnspan=2, sticky="w", padx=5, pady=5)
        
        path_frame.grid_columnconfigure(1, weight=1) # (新) 让 Entry 扩展

        # 2. (新) 预设
        preset_frame = ttk.Labelframe(self.scrollable_frame, text="坐标预设", padding="5")
        preset_frame.pack(fill="x", expand=True, pady=5)
        
        ttk.Label(preset_frame, text="选择预设:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.preset_var = tk.StringVar()
        preset_options = list(PRESETS.keys())
        self.preset_menu = ttk.Combobox(preset_frame, textvariable=self.preset_var, values=preset_options, state="readonly", width=30)
        self.preset_menu.grid(row=0, column=1, padx=5, pady=5)
        self.preset_menu.bind("<<ComboboxSelected>>", self.on_preset_select)
        
        # 3. 坐标输入
        self.coords_frame = ttk.Labelframe(self.scrollable_frame, text="坐标 (WGS-84)", padding="5")
        self.coords_frame.pack(fill="x", expand=True, pady=5)
        
        self.coord_entries = {}
        coord_labels = {
            'p1': 'P1 (左下/SW):',
            'p2': 'P2 (右下/SE):',
            'p3': 'P3 (右上/NE):',
            'p4': 'P4 (左上/NW):'
        }
        
        row = 0
        for key, text in coord_labels.items():
            ttk.Label(self.coords_frame, text=text).grid(row=row, column=0, sticky="w", padx=5, pady=2)
            
            ttk.Label(self.coords_frame, text="纬:").grid(row=row, column=1, sticky="w", padx=5)
            lat_var = tk.StringVar()
            ttk.Entry(self.coords_frame, textvariable=lat_var, width=12).grid(row=row, column=2, sticky="w")
            self.coord_entries[f'{key}_lat'] = lat_var
            
            ttk.Label(self.coords_frame, text="经:").grid(row=row, column=3, sticky="w", padx=5)
            lon_var = tk.StringVar()
            ttk.Entry(self.coords_frame, textvariable=lon_var, width=12).grid(row=row, column=4, sticky="w")
            self.coord_entries[f'{key}_lon'] = lon_var
            row += 1
            
        self.coords_frame.grid_columnconfigure(2, weight=1)
        self.coords_frame.grid_columnconfigure(4, weight=1)

        # 4. 模拟参数
        params_frame = ttk.Labelframe(self.scrollable_frame, text="模拟参数", padding="5")
        params_frame.pack(fill="x", expand=True)
        
        ttk.Label(params_frame, text="总距离 (米):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.total_dist_m = tk.StringVar(value="3000")
        ttk.Entry(params_frame, textvariable=self.total_dist_m, width=10).grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(params_frame, text="路径点间距 (米):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.step_m = tk.StringVar(value="0.5")
        ttk.Entry(params_frame, textvariable=self.step_m, width=10).grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(params_frame, text="圆弧角度 (度):").grid(row=1, column=2, sticky="w", padx=5, pady=5)
        self.arc_degrees = tk.StringVar(value="170.0") 
        ttk.Entry(params_frame, textvariable=self.arc_degrees, width=10).grid(row=1, column=3, padx=5, pady=5)
        
        # 5. 平滑配速
        pace_smooth_frame = ttk.Labelframe(self.scrollable_frame, text="平滑配速 (反作弊)", padding="5")
        pace_smooth_frame.pack(fill="x", expand=True, pady=5)
        
        self.random_pace_var = tk.BooleanVar(value=True)
        self.random_pace_check = ttk.Checkbutton(pace_smooth_frame, text="启用平滑配速", variable=self.random_pace_var, command=self.toggle_pace_entries)
        self.random_pace_check.grid(row=0, column=0, sticky="w", padx=10, pady=5)
        
        ttk.Label(pace_smooth_frame, text="基础配速 (分钟/公里):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.pace_minkm = tk.StringVar(value="6.4")
        self.pace_entry = ttk.Entry(pace_smooth_frame, textvariable=self.pace_minkm, width=10)
        self.pace_entry.grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(pace_smooth_frame, text="变异率 (± min/km):").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.variability_var = tk.StringVar(value="0.5")
        self.variability_entry = ttk.Entry(pace_smooth_frame, textvariable=self.variability_var, width=10)
        self.variability_entry.grid(row=2, column=1, padx=5, pady=5)
        
        ttk.Label(pace_smooth_frame, text="变化平滑度 (秒):").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        self.smoothness_var = tk.StringVar(value="27")
        self.smoothness_entry = ttk.Entry(pace_smooth_frame, textvariable=self.smoothness_var, width=10)
        self.smoothness_entry.grid(row=3, column=1, padx=5, pady=5)

        # 6. 路径微调
        offset_frame = ttk.Labelframe(self.scrollable_frame, text="路径微调 (偏移)", padding="5")
        offset_frame.pack(fill="x", expand=True, pady=(0, 5))

        ttk.Label(offset_frame, text="北/南 偏移 (米):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.offset_ns = tk.StringVar(value="0.0")
        self.offset_ns_entry = ttk.Entry(offset_frame, textvariable=self.offset_ns, width=10)
        self.offset_ns_entry.grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(offset_frame, text="(正数向北, 负数向南)").grid(row=0, column=2, sticky="w", padx=5, pady=5)

        ttk.Label(offset_frame, text="东/西 偏移 (米):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.offset_ew = tk.StringVar(value="0.0")
        self.offset_ew_entry = ttk.Entry(offset_frame, textvariable=self.offset_ew, width=10)
        self.offset_ew_entry.grid(row=1, column=1, padx=5, pady=5)
        ttk.Label(offset_frame, text="(正数向东, 负数向西)").grid(row=1, column=2, sticky="w", padx=5, pady=5)

        # 7. 随机偏移 (GPS 噪声)
        random_frame = ttk.Labelframe(self.scrollable_frame, text="随机偏移 (GPS 噪声)", padding="5")
        random_frame.pack(fill="x", expand=True, pady=(0, 10))
        
        self.random_offset_var = tk.BooleanVar(value=True)
        self.random_offset_check = ttk.Checkbutton(random_frame, text="启用随机偏移", variable=self.random_offset_var, command=self.toggle_random_offset_entries)
        self.random_offset_check.grid(row=0, column=0, sticky="w", padx=5, pady=5)

        ttk.Label(random_frame, text="偏移几率 (%):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.random_offset_chance = tk.StringVar(value="13")
        self.random_offset_chance_entry = ttk.Entry(random_frame, textvariable=self.random_offset_chance, width=10)
        self.random_offset_chance_entry.grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(random_frame, text="左/右最大偏移 (米):").grid(row=1, column=2, sticky="w", padx=5, pady=5)
        self.random_offset_range = tk.StringVar(value="0.8")
        self.random_offset_range_entry = ttk.Entry(random_frame, textvariable=self.random_offset_range, width=10)
        self.random_offset_range_entry.grid(row=1, column=3, padx=5, pady=5)

        # 8. (新) 手动控制
        manual_frame = ttk.Labelframe(self.scrollable_frame, text="手动控制 (可在模拟时使用方向键)", padding="5")
        manual_frame.pack(fill="x", expand=True, pady=(0, 10))
        
        self.manual_buttons = {}
        self.manual_buttons["N"] = ttk.Button(manual_frame, text="↑ (北)", command=self.manual_move_north, state=tk.DISABLED)
        self.manual_buttons["N"].grid(row=0, column=1, padx=5, pady=2)
        
        self.manual_buttons["W"] = ttk.Button(manual_frame, text="← (西)", command=self.manual_move_west, state=tk.DISABLED)
        self.manual_buttons["W"].grid(row=1, column=0, padx=5, pady=2)
        
        self.manual_buttons["S"] = ttk.Button(manual_frame, text="↓ (南)", command=self.manual_move_south, state=tk.DISABLED)
        self.manual_buttons["S"].grid(row=1, column=1, padx=5, pady=2)
        
        self.manual_buttons["E"] = ttk.Button(manual_frame, text="→ (东)", command=self.manual_move_east, state=tk.DISABLED)
        self.manual_buttons["E"].grid(row=1, column=2, padx=5, pady=2)
        
        manual_frame.grid_columnconfigure(0, weight=1)
        manual_frame.grid_columnconfigure(1, weight=1)
        manual_frame.grid_columnconfigure(2, weight=1)

        # 9. 控制按钮
        button_frame = ttk.Frame(self.scrollable_frame, padding="5")
        button_frame.pack(fill="x", expand=True)
        
        self.start_button = ttk.Button(button_frame, text="开始模拟", command=self.start_simulation)
        self.start_button.pack(side=tk.LEFT, fill="x", expand=True, padx=5)
        
        self.pause_button = ttk.Button(button_frame, text="暂停", command=self.toggle_pause, state=tk.DISABLED)
        self.pause_button.pack(side=tk.LEFT, fill="x", expand=True, padx=5)
        
        self.stop_button = ttk.Button(button_frame, text="停止模拟", command=self.stop_simulation, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, fill="x", expand=True, padx=5)
        
        self.skip_wait_button = ttk.Button(button_frame, text="跳过等待", command=self.skip_wait_now, state=tk.DISABLED)
        self.skip_wait_button.pack(side=tk.LEFT, fill="x", expand=True, padx=5)
        
        # 10. 状态显示
        status_frame = ttk.Labelframe(self.scrollable_frame, text="状态", padding="5")
        status_frame.pack(fill="both", expand=True, pady=10)
        
        self.status_label = ttk.Label(status_frame, text="状态: 空闲", foreground="green")
        self.status_label.pack(anchor="w", padx=5, pady=2)
        
        self.coords_label = ttk.Label(status_frame, text="", foreground="darkblue")
        self.coords_label.pack(anchor="w", padx=5, pady=2)
        
        # 加载设置
        self.load_settings()
        
        # (新) 初始化预设
        if not self.preset_var.get():
            self.preset_menu.current(0) # 默认选择第一个
        self.load_preset(self.preset_var.get())
        
        # (新) 初始化UI状态
        self.toggle_pace_entries()
        self.toggle_random_offset_entries()
        
        # (新) 绑定快捷键
        self.bind_keys()
        
        # 退出时停止线程
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # 启动队列检查循环
        self.check_queue()

    # (新) 手动控制函数 (使用锁)
    def manual_move_north(self, event=None):
        if self.start_button.cget('state') == tk.DISABLED: # 只在模拟运行时生效
            # (新) 只在暂停时生效
            if not pause_event.is_set():
                self.send_manual_command(bearing=0)

    def manual_move_south(self, event=None):
        if self.start_button.cget('state') == tk.DISABLED:
            if not pause_event.is_set():
                self.send_manual_command(bearing=180)

    def manual_move_east(self, event=None):
        if self.start_button.cget('state') == tk.DISABLED:
            if not pause_event.is_set():
                self.send_manual_command(bearing=90)

    def manual_move_west(self, event=None):
        if self.start_button.cget('state') == tk.DISABLED:
            if not pause_event.is_set():
                self.send_manual_command(bearing=270)
    
    # (新) 发送手动命令的辅助函数
    def send_manual_command(self, bearing):
        if self.last_sent_point is None:
            self.status_label.config(text="错误: 未知当前位置", foreground="red")
            return
            
        # 1. 计算新点
        new_point = geodesic(meters=self.manual_step_m).destination(self.last_sent_point, bearing=bearing)
        
        # 2. 更新 last_sent_point (重要!)
        self.last_sent_point = new_point 
        
        # 3. 启动一个临时线程来发送命令
        threading.Thread(
            target=send_manual_location,
            args=(self, new_point.latitude, new_point.longitude),
            daemon=True
        ).start()
            
    def bind_keys(self):
        self.root.bind("<Up>", self.manual_move_north)
        self.root.bind("<Down>", self.manual_move_south)
        self.root.bind("<Right>", self.manual_move_east)
        self.root.bind("<Left>", self.manual_move_west)
        
    def unbind_keys(self):
        self.root.unbind("<Up>")
        self.root.unbind("<Down>")
        self.root.unbind("<Right>")
        self.root.unbind("<Left>")

    # (新) 切换配速输入框
    def toggle_pace_entries(self):
        if self.random_pace_var.get():
            # 启用随机, 禁用固定
            self.pace_entry.config(state=tk.NORMAL) # 基础配速保持可用
            self.variability_entry.config(state=tk.NORMAL)
            self.smoothness_entry.config(state=tk.NORMAL)
        else:
            # 禁用随机, 启用固定
            self.pace_entry.config(state=tk.NORMAL)
            self.variability_entry.config(state=tk.DISABLED)
            self.smoothness_entry.config(state=tk.DISABLED)

    # (新) 切换随机偏移输入框
    def toggle_random_offset_entries(self):
        if self.random_offset_var.get():
            self.random_offset_chance_entry.config(state=tk.NORMAL)
            self.random_offset_range_entry.config(state=tk.NORMAL)
        else:
            self.random_offset_chance_entry.config(state=tk.DISABLED)
            self.random_offset_range_entry.config(state=tk.DISABLED)

    # (新) 选择预设时调用
    def on_preset_select(self, event):
        preset_name = self.preset_var.get()
        self.load_preset(preset_name)

    # (新) 加载预设数据到 GUI
    def load_preset(self, preset_name):
        data = PRESETS.get(preset_name)
        if not data:
            return
            
        self.coord_entries['p1_lat'].set(data['p1_lat'])
        self.coord_entries['p1_lon'].set(data['p1_lon'])
        self.coord_entries['p2_lat'].set(data['p2_lat'])
        self.coord_entries['p2_lon'].set(data['p2_lon'])
        self.coord_entries['p3_lat'].set(data['p3_lat'])
        self.coord_entries['p3_lon'].set(data['p3_lon'])
        self.coord_entries['p4_lat'].set(data['p4_lat'])
        self.coord_entries['p4_lon'].set(data['p4_lon'])
        
        # 只有在配置文件没加载的情况下，才设置偏移
        if not hasattr(self, 'settings_loaded') or not self.settings_loaded:
             self.offset_ns.set(data.get('offset_ns', '0.0'))
             self.offset_ew.set(data.get('offset_ew', '0.0'))

    def browse_ld_folder(self):
        directory = filedialog.askdirectory()
        if directory:
            self.ld_folder_path.set(directory)

    def start_simulation(self):
        try:
            # 1. 清除停止/暂停标记
            stop_simulation_event.clear()
            pause_event.set() # .set() 意味着 "未暂停"
            skip_wait_event.clear() # (新) 清除跳过事件
            
            # (新) 重置手动偏移
            self.last_sent_point = None
            
            # 2. 解析和验证输入
            ld_folder = self.ld_folder_path.get()
            
            if not ld_folder: 
                raise Exception("请提供雷电模拟器安装目录。")
            
            emu_index = int(self.emulator_index.get())
            p1 = Point(latitude=float(self.coord_entries['p1_lat'].get()), longitude=float(self.coord_entries['p1_lon'].get()))
            p2 = Point(latitude=float(self.coord_entries['p2_lat'].get()), longitude=float(self.coord_entries['p2_lon'].get()))
            p3 = Point(latitude=float(self.coord_entries['p3_lat'].get()), longitude=float(self.coord_entries['p3_lon'].get()))
            p4 = Point(latitude=float(self.coord_entries['p4_lat'].get()), longitude=float(self.coord_entries['p4_lon'].get()))
            
            self.last_sent_point = p1 # (新) 初始化最后点位
            
            total_dist_m = float(self.total_dist_m.get())
            step_m = float(self.step_m.get())
            
            # (新) 路径微调现在由线程处理
            self.initial_offset_ns = float(self.offset_ns.get())
            self.initial_offset_ew = float(self.offset_ew.get())
            
            skip_wait = self.skip_wait_var.get()
            
            if skip_wait:
                skip_wait_event.set()
            
            # (新) 动态计算 pace_info
            pace_info = None
            if self.random_pace_var.get():
                base_pace = float(self.pace_minkm.get()) 
                variability = float(self.variability_var.get()) 
                smoothness = float(self.smoothness_var.get()) 
                if base_pace <= 0 or variability < 0 or smoothness <= 0:
                    raise Exception("平滑配速参数无效。")
                pace_info = ("smooth", base_pace, variability, smoothness)
            else:
                pace_minkm = float(self.pace_minkm.get())
                if pace_minkm <= 0:
                    raise Exception("配速必须 > 0")
                speed_ms = 1000.0 / (pace_minkm * 60.0)
                if speed_ms <= 0 or step_m <= 0:
                    raise Exception("速度或间距必须 > 0")
                pace_info = step_m / speed_ms # 恒定延迟
            
            arc_degrees = float(self.arc_degrees.get())
            if arc_degrees <= 0 or arc_degrees >= 360:
                raise Exception("圆弧角度必须在 0 和 360 度之间。")
            
            random_offset_info = None
            if self.random_offset_var.get():
                chance = float(self.random_offset_chance.get())
                range_m = float(self.random_offset_range.get())
                if chance <= 0 or chance > 100 or range_m <= 0:
                    raise Exception("随机偏移参数无效")
                random_offset_info = (chance / 100.0, range_m) # 转换

            if total_dist_m <= 0:
                raise Exception("总距离必须 > 0")
            
            self.status_label.config(text=f"正在计算单圈路径...", foreground="blue")

            # 4. **恢复为正确的几何**
            # (P1 -> P2 直道), (P2 -> P3 弯道), (P3 -> P4 直道), (P4 -> P1 弯道)
            
            s1_points, s1_len = interpolate_straight(p1, p2, step_m)
            a1_points, a1_len = interpolate_arc(p2, p3, step_m, arc_degrees)
            s2_points, s2_len = interpolate_straight(p3, p4, step_m)
            a2_points, a2_len = interpolate_arc(p4, p1, step_m, arc_degrees)

            single_lap_points = s1_points + a1_points + s2_points + a2_points
            
            if not single_lap_points: raise Exception("计算出的路径点为0，请检查坐标。")
            
            # 5. 计算完整路径
            total_points_needed = int(total_dist_m / step_m)
            full_path_points = []
            lap_point_count = len(single_lap_points)

            for i in range(total_points_needed):
                full_path_points.append(single_lap_points[i % lap_point_count])
            
            # (新) 6. 查找最近点 (防漂移)
            if self.start_from_last_pos_var.get() and self.last_known_location:
                try:
                    last_point_obj = Point(latitude=self.last_known_location[0], longitude=self.last_known_location[1])
                    start_index = self.find_closest_start_index(full_path_points, last_point_obj)
                    
                    # 重排列表
                    full_path_points = full_path_points[start_index:] + full_path_points[:start_index]
                    self.status_label.config(text="从上次位置恢复...", foreground="blue")
                except Exception as e:
                    self.status_label.config(text=f"无法从上次位置恢复: {e}", foreground="orange")
                    # 即使失败，也继续正常运行
            
            # 7. 启动线程
            self.start_button.config(state=tk.DISABLED)
            self.pause_button.config(text="暂停", state=tk.NORMAL) 
            self.stop_button.config(state=tk.NORMAL) 
            # (新) 启用手动按钮
            for btn in self.manual_buttons.values():
                btn.config(state=tk.NORMAL)
            
            self.simulation_thread = threading.Thread(
                target=run_simulation_thread,
                args=(self.status_queue, self, ld_folder, emu_index, full_path_points, pace_info, step_m, random_offset_info), # (新) 传入 self (app)
                daemon=True
            )
            self.simulation_thread.start()

        except Exception as e:
            messagebox.showerror("启动失败", f"启动模拟失败:\n{e}")
            self.status_label.config(text=f"失败: {e}", foreground="red")
            self.reset_gui_state() 

    # (新) 查找最近点的辅助函数
    def find_closest_start_index(self, path_points, last_point):
        min_dist = float('inf')
        start_index = 0
        
        # (新) 优化：每隔 10 个点检查一次，加快查找速度
        for i in range(0, len(path_points), 10):
            point, bearing = path_points[i]
            dist = geodesic(point, last_point).meters
            if dist < min_dist:
                min_dist = dist
                start_index = i
        
        # (新) 粗略找到后，在附近 [-10, +10] 范围内进行精细查找
        fine_start = max(0, start_index - 10)
        fine_end = min(len(path_points), start_index + 10)
        
        for i in range(fine_start, fine_end):
            point, bearing = path_points[i]
            dist = geodesic(point, last_point).meters
            if dist < min_dist:
                min_dist = dist
                start_index = i
                
        return start_index

    def toggle_pause(self):
        """ 切换暂停/继续状态 """
        if pause_event.is_set():
            # 正在运行 -> 暂停
            pause_event.clear() 
            self.pause_button.config(text="继续")
            # (新) 状态更新由线程负责
            # (新) 暂停时保持手动按钮可用
            for btn in self.manual_buttons.values():
                btn.config(state=tk.NORMAL)
        else:
            # 已暂停 -> 继续
            pause_event.set() 
            self.pause_button.config(text="暂停")
            # (新) 状态更新由线程负责
            # (新) 恢复时保持手动按钮可用
            for btn in self.manual_buttons.values():
                btn.config(state=tk.NORMAL)

    def stop_simulation(self):
        """ 停止模拟 """
        self.status_label.config(text="正在发送停止信号...", foreground="orange")
        stop_simulation_event.set() 
        pause_event.set() # 确保线程没有卡在 pause_event.wait()
    
    # (新) 立即跳过等待
    def skip_wait_now(self):
        skip_wait_event.set()
        self.skip_wait_button.config(state=tk.DISABLED)
        
    def check_queue(self):
        """
        主 GUI 线程调用的函数，用于检查来自模拟线程的消息。
        """
        try:
            msg_type, msg, color_or_coords = self.status_queue.get_nowait()
            
            if msg_type == "ERROR":
                messagebox.showerror("线程错误", msg)
                self.status_label.config(text=f"错误: {msg}", foreground="red")
                self.reset_gui_state()
            elif msg_type == "UPDATE":
                progress = msg
                coords = color_or_coords
                self.status_label.config(text=progress, foreground="blue")
                self.coords_label.config(text=coords)
            elif msg_type == "STATUS":
                self.status_label.config(text=msg, foreground=color_or_coords)
                if msg == "模拟已手动停止。":
                    self.reset_gui_state()
            elif msg_type == "DONE":
                self.status_label.config(text=msg, foreground=color_or_coords)
                self.coords_label.config(text="")
                self.reset_gui_state()
            # (新) 处理跳过按钮
            elif msg_type == "ENABLE_SKIP":
                new_state = tk.NORMAL if color_or_coords else tk.DISABLED
                self.skip_wait_button.config(state=new_state)
                # (新) 增加 update_idletasks 确保按钮立即刷新
                self.root.update_idletasks() 

        except queue.Empty:
            pass # 队列为空
        
        self.root.after(100, self.check_queue) # 100ms 轮询一次

    def reset_gui_state(self):
        """ 重置GUI按钮状态 """
        self.start_button.config(state=tk.NORMAL)
        self.pause_button.config(text="暂停", state=tk.DISABLED)
        self.stop_button.config(state=tk.DISABLED)
        self.skip_wait_button.config(state=tk.DISABLED) 
        # (新) 禁用手动按钮
        for btn in self.manual_buttons.values():
            btn.config(state=tk.DISABLED)
            
        # (新) 保存最后位置
        if self.last_sent_point:
            self.last_known_location = (self.last_sent_point.latitude, self.last_sent_point.longitude)
        
        self.simulation_thread = None

    # (新) 自动检测
    def find_leidian_path(self):
        return find_leidian_path()

    def load_settings(self):
        """ (新) 加载设置 """
        self.settings_loaded = False # 默认为 False
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                self.ld_folder_path.set(
                    data.get("ld_folder_path", "D:/Program Files/leidian/LDPlayer14")
                )
                self.offset_ns.set(data.get("offset_ns", "0.0"))
                self.offset_ew.set(data.get("offset_ew", "0.0"))
                self.preset_var.set(data.get("last_preset", "CUHKSZ(Lower Campus)"))
                
                # (新) 加载最后位置
                self.last_known_location = data.get(
                    "last_known_location", [22.687253, 114.204035]
                )
                if self.last_known_location:
                    # (新) 默认勾选
                    self.start_from_last_pos_var.set(True)
                
                # (新) 加载随机设置
                self.random_offset_var.set(data.get("use_random_offset", True))
                self.random_offset_chance.set(data.get("random_offset_chance", "13"))
                self.random_offset_range.set(data.get("random_offset_range", "0.8"))
                
                # (新) 加载配速设置
                self.random_pace_var.set(data.get("use_random_pace", True))
                self.pace_minkm.set(data.get("base_pace", "5.5"))
                self.variability_var.set(data.get("pace_variability", "0.2"))
                self.smoothness_var.set(data.get("pace_smoothness", "27"))

                self.settings_loaded = True # 标记已加载
        except FileNotFoundError:
            self.preset_menu.current(0) # 配置文件不存在，默认选择第一个
        except Exception as e:
            print(f"加载配置文件失败: {e}")
            self.preset_menu.current(0)
            
        # (新) 自动检测
        if not self.ld_folder_path.get():
             try:
                path = self.find_leidian_path()
                if path:
                    self.ld_folder_path.set(path)
                    # (新) 延迟更新状态，避免覆盖"空闲"
                    self.root.after(1000, lambda: self.status_label.config(text="已自动检测到雷电模拟器目录。", foreground="blue"))
             except Exception as e:
                print(f"自动检测失败: {e}") # 非关键错误

    def save_settings(self):
        """ (新) 保存设置 """
        try:
            data = {
                "ld_folder_path": self.ld_folder_path.get(),
                "offset_ns": self.offset_ns.get(),
                "offset_ew": self.offset_ew.get(),
                "last_preset": self.preset_var.get(),
                
                # (新) 保存最后位置
                "last_known_location": self.last_known_location,
                
                "use_random_offset": self.random_offset_var.get(),
                "random_offset_chance": self.random_offset_chance.get(),
                "random_offset_range": self.random_offset_range.get(),
                
                # (新) 保存配速设置
                "use_random_pace": self.random_pace_var.get(),
                "base_pace": self.pace_minkm.get(),
                "pace_variability": self.variability_var.get(),
                "pace_smoothness": self.smoothness_var.get()
            }
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"保存配置文件失败: {e}")

    def on_closing(self):
        """ (新) 在关闭时保存设置 """
        self.save_settings() 
        if self.simulation_thread and self.simulation_thread.is_alive():
            stop_simulation_event.set() 
            pause_event.set() 
        
        # (新) 解绑
        self.unbind_keys()
        if hasattr(self, 'mouse_wheel_binding_id_1'):
            self.root.unbind_all("<MouseWheel>")
            self.root.unbind_all("<Button-4>")
            self.root.unbind_all("<Button-5>")
            
        self.root.destroy()

# -----------------------------------------------------------------
# 启动应用
# -----------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    app = TrackSimulatorApp(root)
    root.mainloop()