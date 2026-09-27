"""主窗口：监控窗口管理、按钮目标、运行参数、启停与实时日志。"""
from __future__ import annotations

import dataclasses
import os
import queue
import subprocess
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable

from core.capture import ScreenCapture, root_hwnd, set_process_dpi_awareness
from core.config import (BASE_DIR, ConfigError, Monitor, Settings, Target, abs_path, changed_config_files, config_files,
                         load_monitors, load_settings, load_targets, save_monitors,
                         save_settings, save_targets, unique_name)
from core.detector import CooldownTracker, Detector, resolve_region
from core.engine import Engine, Event
from core.hotkey import HotkeyManager
from core.runlog import RunLogger
from ui.monitor_dialog import MonitorDialog
from ui.overlay import HighlightOverlay
from ui.region_picker import RegionPicker
from ui.target_dialog import TargetDialog
from ui.theme import apply_theme

# 自动启动前的缓冲：让界面先画完，也留出关窗反悔的时间
AUTO_START_DELAY = 3.0
# 品牌区图标的目标边长（按 96 DPI 设计，实际用 _px 放大）
BRAND_ICON_PX = 30
# 列表为空时的占位提示行前缀：选中占位行按「空列表」处理，不能当成真实行的下标用
EMPTY_HINT = "（空）"


def _ui_scale(root: tk.Tk) -> float:
    """Tk 的字体随系统 DPI 放大，而 geometry/列宽是写死的像素，两者必须同步缩放。"""
    try:
        return max(1.0, float(root.tk.call("tk", "scaling")) / (96.0 / 72.0))
    except (tk.TclError, ValueError):
        return 1.0


class MainWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        # 手改 yaml 改坏是常事：先收着校正说明，等日志框建好再回放，别抛 traceback
        self._config_warnings: list[str] = []
        report = self._config_warnings.append
        self.settings: Settings = load_settings(report)
        self.monitors: list[Monitor] = load_monitors(report)
        self.targets: list[Target] = load_targets(report)
        self.events: queue.Queue = queue.Queue()
        # 运行日志落盘到 logs\run.log：扫描线程的命中/点击/异常都写进去，
        # 界面文本框只留最近 400 行，出问题时 reload 前的记录只在文件里
        self.run_logger = RunLogger(BASE_DIR)
        self.engine = Engine(self.settings, self.monitors, self.targets, self.events,
                              run_logger=self.run_logger)
        self.overlay = HighlightOverlay(root)
        self.hotkeys = HotkeyManager()
        self._monitor_rows: dict[str, str] = {}
        self._max_threads = max(1, os.cpu_count() or 8)
        self._closing = False
        self._auto_job: str | None = None
        self._config_files = config_files()
        # 记录基线 mtime：开机已存在的文件不算“外部改动”。
        _, self._config_mtimes = changed_config_files(self._config_files, {})

        root.title("直播按钮自动点击器 · 多窗口")
        self.scale = _ui_scale(root)
        self.theme = apply_theme(root, self._px)
        self._brand_icon = self._load_icons()
        # 缩放后可能超出屏幕（4K 200% 时 1180x760 会变成 2360x1520），所以要夹到屏幕内
        width = min(self._px(1180), root.winfo_screenwidth() - 40)
        height = min(self._px(820), root.winfo_screenheight() - 60)
        root.geometry(f"{width}x{height}")
        root.minsize(min(self._px(980), width), min(self._px(620), height))
        self._apply_job: str | None = None
        self._build_widgets()
        self._load_settings_to_vars()
        self._refresh_monitors()
        self._refresh_targets()
        self._log("info", "就绪：①「监控窗口」新增并框选各个直播窗口 ②「按钮目标」添加要点的按钮 ③「测试扫描一次」 ④「启动」。")
        self._log("info", "按钮目标默认对所有监控窗口生效；只想在某个窗口点它时，在目标的「生效窗口」里填窗口名。")
        for message in self._config_warnings:
            self._log("warn", f"配置已自动修正：{message}")
        self._config_warnings.clear()
        self.run_logger.start()
        self._log("info", f"运行日志：{self.run_logger.path}")
        self._register_hotkey()
        root.after(150, self._pump_events)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_auto_start()

    # ---------- UI 构建 ----------
    def _px(self, value: int) -> int:
        """把按 96 DPI 设计的像素值换算成当前屏幕的实际像素。"""
        return max(1, int(round(value * self.scale)))

    def _load_icons(self) -> tk.PhotoImage | None:
        """标题栏/任务栏用 ico，品牌区用 PNG —— Tk 的 PhotoImage 读不了 ico。

        资源在 BASE_DIR/assets：源码跑是仓库，打包后是 exe 同级，用户想换图直接替换文件。
        default= 会作用到所有 Toplevel，对话框不用再单独设。
        """
        assets = os.path.join(BASE_DIR, "assets")
        brand = None
        png = os.path.join(assets, "app_icon.png")
        if os.path.isfile(png):
            brand = tk.PhotoImage(file=png)
            step = max(1, round(brand.width() / self._px(BRAND_ICON_PX)))
            if step > 1:
                brand.subsample(step)
        ico = os.path.join(assets, "app_icon.ico")
        if os.path.isfile(ico):
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass        # 只有 Windows 支持 ico，别的平台忽略
        return brand

    def _build_widgets(self) -> None:
        self._build_header()

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=self._px(12), pady=self._px(12))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=3)
        body.rowconfigure(1, weight=2)
        body.rowconfigure(2, weight=2)

        # 左上：监控窗口
        mon_frame = ttk.LabelFrame(body, text="监控窗口 · 每个直播窗口一条扫描线程",
                                   style="Card.TLabelframe")
        mon_frame.grid(row=0, column=0, sticky="nsew")
        mcols = ("enabled", "region", "interval", "scans", "clicks", "note")
        self.mon_tree = self._make_tree(mon_frame, mcols, {
            "#0": ("窗口", 110), "enabled": ("启用", 44), "region": ("区域", 178),
            "interval": ("间隔s", 58), "scans": ("扫描", 56), "clicks": ("点击", 56),
            "note": ("状态", 170),
        }, height=4)
        self.mon_tree.bind("<Double-1>", lambda e: self._edit_monitor())

        # 五个按钮一排比左栏还宽，挤不下时 ttk 会把多余的按钮压成 1px「隐形」，所以分两行
        mbtns = ttk.Frame(mon_frame)
        mbtns.pack(fill="x", pady=(self._px(8), 0))
        row1 = ttk.Frame(mbtns)
        row1.pack(side="top", fill="x")
        row2 = ttk.Frame(mbtns)
        row2.pack(side="top", fill="x", pady=(self._px(4), 0))
        ttk.Button(row1, text="新增窗口", command=self._add_monitor).pack(side="left")
        ttk.Button(row1, text="编辑", command=self._edit_monitor).pack(side="left", padx=self._px(5))
        ttk.Button(row1, text="重新框选", command=self._repick_monitor).pack(side="left")
        ttk.Button(row2, text="启用/停用", command=self._toggle_monitor).pack(side="left")
        ttk.Button(row2, text="删除", command=self._delete_monitor).pack(side="left", padx=self._px(5))

        # 左下：按钮目标
        tgt_frame = ttk.LabelFrame(body, text="按钮目标", style="Card.TLabelframe")
        tgt_frame.grid(row=1, column=0, sticky="nsew", pady=self._px(12))
        self.tree = self._make_tree(tgt_frame, ("enabled", "scope", "template", "keywords",
                                               "offset", "cooldown"), {
            "#0": ("名称", 110), "enabled": ("启用", 44), "scope": ("生效窗口", 120),
            "template": ("模板", 140), "keywords": ("OCR关键词", 140),
            "offset": ("偏移", 66), "cooldown": ("冷却s", 54),
        }, height=4)
        self.tree.bind("<Double-1>", lambda e: self._edit_target())

        tbtns = ttk.Frame(tgt_frame)
        tbtns.pack(fill="x", pady=(self._px(8), 0))
        trow1 = ttk.Frame(tbtns)
        trow1.pack(side="top", fill="x")
        trow2 = ttk.Frame(tbtns)
        trow2.pack(side="top", fill="x", pady=(self._px(4), 0))
        ttk.Button(trow1, text="新增", command=self._add_target).pack(side="left")
        ttk.Button(trow1, text="编辑", command=self._edit_target).pack(side="left", padx=self._px(5))
        ttk.Button(trow1, text="复制到所有窗口",
                   command=self._copy_target_to_monitors).pack(side="left")
        ttk.Button(trow2, text="删除", command=self._delete_target).pack(side="left")
        ttk.Button(trow2, text="启用/停用",
                   command=self._toggle_enabled).pack(side="left", padx=self._px(5))

        # 右：参数（吃掉整个右列高度，日志才有地方横铺）
        self._build_params(body).grid(row=0, column=1, rowspan=2, sticky="nsew",
                                      padx=(self._px(12), 0))

        # 底：日志
        logbox = ttk.LabelFrame(body, text="运行日志", style="Card.TLabelframe")
        logbox.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(self._px(12), 0))
        pal = self.theme.palette
        # width 按字符算，默认 80 字符会把整行撑出屏幕
        self.log_text = tk.Text(logbox, width=40, height=6, wrap="word", relief="flat",
                                background=pal.surface, foreground=pal.text,
                                insertbackground=pal.text, highlightthickness=1,
                                highlightbackground=pal.border_strong, highlightcolor=pal.accent,
                                padx=self._px(8), pady=self._px(6),
                                font=self.theme.fonts["mono"])
        for tag, color in (("info", pal.text), ("warn", pal.warn), ("error", pal.danger)):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(logbox, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")

    def _build_header(self) -> None:
        """白底应用栏：品牌区 + 状态胶囊在上，动作按钮在下。"""
        head = ttk.Frame(self.root, style="Header.TFrame")
        head.pack(fill="x")
        ttk.Separator(head, orient="horizontal",
                      style="Header.TSeparator").pack(side="bottom", fill="x")

        brand = ttk.Frame(head, style="Header.TFrame")
        brand.pack(fill="x", padx=self._px(16), pady=(self._px(12), self._px(4)))
        icon = self._brand_icon
        if icon is not None:
            ttk.Label(brand, image=icon, style="Header.TLabel").pack(side="left",
                                                                     padx=(0, self._px(12)))
        titles = ttk.Frame(brand, style="Header.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="直播按钮自动点击器",
                  style="Header.Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="多窗口 · 模板匹配 + OCR 双通道",
                  style="Header.Subtitle.TLabel").pack(anchor="w")

        side = ttk.Frame(brand, style="Header.TFrame")
        side.pack(side="right")
        self.status_var = tk.StringVar(value="● 已停止")
        self.status_lbl = ttk.Label(side, textvariable=self.status_var, style="StatusIdle.TLabel")
        self.status_lbl.pack(side="right")
        self.stats_var = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.stats_var,
                  style="Header.Subtitle.TLabel").pack(side="right", padx=(0, self._px(12)))

        tools = ttk.Frame(head, style="Header.TFrame")
        tools.pack(fill="x", padx=self._px(16), pady=(self._px(4), self._px(12)))
        self.run_btn = ttk.Button(tools, text="▶ 启动", style="Primary.TButton",
                                  command=self.toggle_run)
        self.run_btn.pack(side="left")
        self.test_btn = ttk.Button(tools, text="测试扫描一次", command=self._test_scan)
        self.test_btn.pack(side="left", padx=self._px(6))
        ttk.Button(tools, text="重置冷却", command=self._reset_cooldowns).pack(side="left")
        ttk.Button(tools, text="打开配置文件",
                   command=self._open_config_dir).pack(side="left", padx=self._px(6))
        ttk.Button(tools, text="打开运行日志",
                   command=self._open_log_dir).pack(side="left")
        ttk.Label(tools, text=f"全局热键 {self.settings.hotkey_toggle.upper()}：启动 / 停止",
                  style="Header.Subtitle.TLabel").pack(side="right")

    def _make_tree(self, master: ttk.Frame, columns: tuple[str, ...],
                   spec: dict[str, tuple[str, int]], height: int) -> ttk.Treeview:
        tree = ttk.Treeview(master, columns=columns, show="tree headings", height=height)
        for key, (text, width) in spec.items():
            tree.heading(key, text=text)
            tree.column(key, width=self._px(width), anchor="w")
        pal = self.theme.palette
        tree.tag_configure("alt", background=pal.row_alt)
        tree.tag_configure("off", foreground=pal.faint)
        tree.tag_configure("hint", foreground=pal.faint)
        tree.tag_configure("run", foreground=pal.ok)
        tree.tag_configure("err", foreground=pal.danger)
        tree.pack(fill="both", expand=True)
        return tree

    def _build_params(self, master: ttk.Frame) -> ttk.Frame:
        # 参数卡的内容比矮窗口的右栏高，直接 grid 会把行高压得比文字还矮、文字全叠在一起。
        # 这里套一层 canvas 滚动：超高时能滚，空间够时观感和原来完全一致。
        wrap = ttk.Frame(master)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0,
                           background=self.theme.palette.bg)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        box = ttk.LabelFrame(canvas, text="运行参数（全局）", style="Card.TLabelframe")
        win_item = canvas.create_window((0, 0), window=box, anchor="nw")
        box.columnconfigure(1, weight=1)
        self.vars: dict[str, tk.Variable] = {}
        self._hint_labels: list[ttk.Label] = []
        self._scale_labels: dict[str, tuple[ttk.Label, Callable[[float], str]]] = {}
        self._decimals: dict[str, int] = {}
        self._row = -1

        self._section(box, "扫描节奏")
        self._scale_row(box, "scan_interval_sec", "扫描间隔", 0.2, 30.0, 1, lambda v: f"{v:g}s")
        self._scale_row(box, "jitter_sec", "随机抖动", 0.0, 5.0, 1, lambda v: f"{v:g}s")

        self._section(box, "识别")
        self._scale_row(box, "match_threshold", "匹配阈值", 0.5, 1.0, 2, lambda v: f"{v:.2f}")
        self._scale_row(box, "ocr_min_score", "OCR 置信度", 0.1, 1.0, 2, lambda v: f"{v:.2f}")
        self._scale_row(box, "highlight_sec", "高亮时长", 0.1, 5.0, 1, lambda v: f"{v:g}s")
        self._row += 1
        ttk.Label(box, text="OCR 线程").grid(row=self._row, column=0, sticky="w")
        self.vars["ocr_threads"] = tk.IntVar()
        ttk.Spinbox(box, from_=1, to=self._max_threads, textvariable=self.vars["ocr_threads"],
                    width=5, command=self._apply_settings).grid(row=self._row, column=1,
                                                                sticky="w",
                                                                padx=(self._px(10), 0))
        ttk.Label(box, text="OCR 并发").grid(row=self._row, column=2, sticky="e")
        self.vars["ocr_concurrency"] = tk.IntVar()
        ttk.Spinbox(box, from_=1, to=8, textvariable=self.vars["ocr_concurrency"], width=5,
                    command=self._apply_settings).grid(row=self._row, column=3, sticky="e")
        self._row += 1
        ttk.Label(box, text="放大边").grid(row=self._row, column=0, sticky="w")
        self.vars["ocr_min_side"] = tk.IntVar()
        ttk.Spinbox(box, from_=0, to=2560, increment=128, textvariable=self.vars["ocr_min_side"],
                    width=5, command=self._apply_settings).grid(row=self._row, column=1,
                                                                sticky="w",
                                                                padx=(self._px(10), 0))
        ttk.Label(box, text="OCR 每几轮").grid(row=self._row, column=2, sticky="e")
        self.vars["ocr_scan_every"] = tk.IntVar()
        ttk.Spinbox(box, from_=1, to=10, textvariable=self.vars["ocr_scan_every"], width=5,
                    command=self._apply_settings).grid(row=self._row, column=3, sticky="e")
        self._row += 1
        ttk.Label(box, text="空闲降频").grid(row=self._row, column=0, sticky="w")
        self.vars["ocr_idle_every"] = tk.IntVar()
        ttk.Spinbox(box, from_=1, to=10, textvariable=self.vars["ocr_idle_every"], width=5,
                    command=self._apply_settings).grid(row=self._row, column=1, sticky="w",
                                                        padx=(self._px(10), 0))
        ttk.Label(box, text="轮").grid(row=self._row, column=2, sticky="e")
        self._hint(box, "很卡就把 OCR 线程降到 1~2、并发保持 1；小窗口字太小就调大放大边。\n"
                        "「OCR 每几轮」设 2 省一半 OCR，代价是最坏多等一个间隔。\n"
                        "「空闲降频」：连续这么多轮什么都没扫到时，OCR 改成每这么多轮才跑一次，"
                        "一有命中立刻恢复；直播没弹窗的白等时段能省一半以上 CPU。")

        self._section(box, "点击与启动")
        self._row += 1
        ttk.Label(box, text="同弹窗防重复").grid(row=self._row, column=0, sticky="w")
        self.vars["click_guard_sec"] = tk.DoubleVar()
        ttk.Spinbox(box, from_=0.0, to=10.0, increment=0.5, format="%.1f",
                    textvariable=self.vars["click_guard_sec"], width=5,
                    command=self._apply_settings).grid(row=self._row, column=1, sticky="w",
                                                       padx=(self._px(10), 0))
        ttk.Label(box, text="秒").grid(row=self._row, column=2, sticky="e")
        self._hint(box, "一张弹窗里常同时写着「立即领券」和「奖券」，会被连着点两下。"
                        "这里填「点过一发后，附近多久不再点」；填 0 就是每见每点。")
        for key, label in (("use_pydirectinput", "使用 pydirectinput（收不到点击时开）"),
                           ("humanize_click", "人性化移动与延迟（降低风控概率）"),
                           ("raise_before_click",
"点击前自动把直播窗口切到前台（被别的窗口挡住时才动）"),
                           ("bring_window_on_screen",
                            "直播窗口在屏幕外时先挪进屏幕再点（点完还原）"),
                           ("auto_start", "打开软件后自动开始运行")):
            self._row += 1
            self.vars[key] = tk.BooleanVar()
            ttk.Checkbutton(box, text=label, variable=self.vars[key],
                            style="Toolbutton",
                            command=self._apply_settings).grid(row=self._row, column=0,
                                                              columnspan=4, sticky="w",
                                                              pady=self._px(1))
        # 右列比左列矮，多出来的高度平摊给每一行，卡片就不会在底部堆出一块空白
        for r in range(self._row + 1):
            box.rowconfigure(r, weight=1)

        def _sync(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            if box.winfo_reqheight() > canvas.winfo_height():
                bar.grid()
            else:
                bar.grid_remove()
            # 提示语跟着卡片宽度换行：卡片宽就少占几行，要滚的内容也短一些
            for hint in self._hint_labels:
                hint.configure(wraplength=max(self._px(220),
                                              box.winfo_width() - self._px(40)))

        box.bind("<Configure>", _sync)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_item, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        def _each(widget):
            yield widget
            for child in widget.winfo_children():
                yield from _each(child)

        for widget in _each(box):
            if widget.winfo_class() not in ("TSpinbox", "TEntry", "TCombobox"):
                widget.bind("<MouseWheel>", _wheel)
        return wrap

    def _section(self, master: ttk.Frame, text: str) -> None:
        self._row += 1
        top = 0 if self._row == 0 else self._px(10)
        ttk.Label(master, text=text, style="Heading.TLabel").grid(
            row=self._row, column=0, columnspan=4, sticky="w", pady=(top, self._px(2)))

    def _hint(self, master: ttk.Frame, text: str) -> None:
        self._row += 1
        hint = ttk.Label(master, text=text, style="Hint.TLabel", wraplength=self._px(250))
        hint.grid(row=self._row, column=0, columnspan=4, sticky="w")
        self._hint_labels.append(hint)

    def _scale_row(self, master: ttk.Frame, key: str, label: str, lo: float, hi: float,
                   decimals: int, fmt: Callable[[float], str]) -> None:
        self._row += 1
        ttk.Label(master, text=label).grid(row=self._row, column=0, sticky="w")
        var = tk.DoubleVar()
        self.vars[key] = var
        self._decimals[key] = decimals
        value = ttk.Label(master, style="Value.TLabel", width=5)
        ttk.Scale(master, from_=lo, to=hi, orient="horizontal", variable=var,
                  command=lambda _s, k=key: self._on_scale(k)).grid(
            row=self._row, column=1, columnspan=2, sticky="ew",
            padx=(self._px(10), self._px(6)))
        value.grid(row=self._row, column=3, sticky="e")
        self._scale_labels[key] = (value, fmt)

    def _on_scale(self, key: str) -> None:
        """滑杆拖动时只刷数字并推迟落盘，松手后统一应用。"""
        value, fmt = self._scale_labels[key]
        value.configure(text=fmt(self._snap(key)))
        self._schedule_apply()

    def _snap(self, key: str) -> float:
        """ttk.Scale 是连续值，按显示精度取整，免得 yaml 里出现 0.8499999。"""
        return round(float(self.vars[key].get()), self._decimals[key])

    def _sync_scales(self) -> None:
        for key, (label, fmt) in self._scale_labels.items():
            label.configure(text=fmt(self._snap(key)))

    def _load_settings_to_vars(self) -> None:
        self.vars["scan_interval_sec"].set(self.settings.scan_interval_sec)
        self.vars["jitter_sec"].set(self.settings.jitter_sec)
        self.vars["match_threshold"].set(self.settings.match_threshold)
        self.vars["ocr_min_score"].set(self.settings.ocr_min_score)
        self.vars["highlight_sec"].set(self.settings.highlight_sec)
        self.vars["ocr_concurrency"].set(self.settings.ocr_concurrency)
        self.vars["ocr_threads"].set(self.settings.ocr_threads)
        self.vars["ocr_min_side"].set(self.settings.ocr_min_side)
        self.vars["ocr_scan_every"].set(self.settings.ocr_scan_every)
        self.vars["ocr_idle_every"].set(self.settings.ocr_idle_every)
        self.vars["click_guard_sec"].set(self.settings.click_guard_sec)
        self.vars["use_pydirectinput"].set(self.settings.use_pydirectinput)
        self.vars["humanize_click"].set(self.settings.humanize_click)
        self.vars["raise_before_click"].set(self.settings.raise_before_click)
        self.vars["bring_window_on_screen"].set(self.settings.bring_window_on_screen)
        self.vars["auto_start"].set(self.settings.auto_start)
        self._sync_scales()

    # ---------- 监控窗口 ----------
    def _refresh_monitors(self) -> None:
        keep = self._selected_name(self.mon_tree)
        self.mon_tree.delete(*self.mon_tree.get_children())
        self._monitor_rows = {}
        states = self.engine.monitor_states()
        for i, m in enumerate(self.monitors):
            state = states.get(m.name)
            item = self.mon_tree.insert("", "end", text=m.name,
                                        values=self._monitor_values(m, state),
                                        tags=self._state_tags(i, m, state))
            self._monitor_rows[m.name] = item
        if not self.monitors:
            self.mon_tree.insert("", "end",
                                 text=f"{EMPTY_HINT}还没有监控窗口 —— 点下方「新增窗口」添加直播间",
                                 tags=("hint",))
        if keep in self._monitor_rows:
            self.mon_tree.selection_set(self._monitor_rows[keep])
        self._apply_settings(persist=False)

    @staticmethod
    def _state_tags(index: int, row: Monitor | Target,
                    state: dict | None) -> tuple[str, ...]:
        """斑马纹叠加行状态：停用最弱、扫描中绿色、出错红色。"""
        tags = ["alt"] if index % 2 else []
        if not row.enabled:
            tags.append("off")
        elif state is not None:
            if state.get("last_error"):
                tags.append("err")
            elif state.get("busy"):
                tags.append("run")
        return tuple(tags)

    @staticmethod
    def _selected_name(tree: ttk.Treeview) -> str | None:
        """重建表格会丢掉选中项，先记下再恢复，连续操作同一行不用重新点选。"""
        sel = tree.selection()
        return tree.item(sel[0], "text") if sel else None

    @staticmethod
    def _identity(m: Monitor) -> str:
        """表格里显示的窗口身份：标题 / 类名 / 进程名，用 + 连起来。"""
        parts = []
        if m.window_title.strip():
            parts.append(f"标题「{m.window_title}」")
        if m.window_class.strip():
            parts.append(f"类名「{m.window_class}」")
        if m.window_process.strip():
            parts.append(f"进程「{m.window_process}」")
        return "+".join(parts) or "未设置"

    def _monitor_values(self, m: Monitor, state: dict | None = None) -> tuple:
        if m.tracked:
            crop = []
            if m.crop_bottom:
                crop.append(f"底-{m.crop_bottom}px")
            if getattr(m, "crop_right", 0):
                crop.append(f"右-{m.crop_right}px")
            if getattr(m, "crop_left", 0):
                crop.append(f"左-{m.crop_left}px")
            region = "跟随 " + self._identity(m) + ("。" + "，".join(crop) if crop else "")
        elif not m.region:
            region = "全屏"
        else:
            region = f"{m.region[2]}x{m.region[3]} @ {m.region[0]},{m.region[1]}"
        interval = "全局" if not m.scan_interval_sec else f"{m.scan_interval_sec:g}"
        if state is None:
            scans = clicks = "-"
            note = "" if m.enabled else "已停用"
        else:
            scans, clicks = state["scans"], state["clicks"]
            note = state.get("last_error") or ("正在扫描" if state.get("busy") else "空闲")
        return ("✓" if m.enabled else "", region, interval, scans, clicks, note[:26])

    def _add_monitor(self) -> None:
        default = unique_name(f"窗口{len(self.monitors) + 1}", [m.name for m in self.monitors])
        dlg = MonitorDialog(self.root, Monitor(name=default), picker_factory=self._pick_region,
                            known_names=[m.name for m in self.monitors])
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        result = dlg.result
        result.name = unique_name(result.name, [m.name for m in self.monitors])
        if not result.region and not result.tracked:
            self._log("warn", f"「{result.name}」既没框选区域也没填窗口标题，将按整屏扫描"
                              f"（多窗口时很慢，建议框选或填标题）。")
        self.monitors.append(result)
        self._persist_monitors()

    def _edit_monitor(self) -> None:
        idx = self._monitor_index()
        if idx is None:
            return
        dlg = MonitorDialog(self.root, self.monitors[idx], picker_factory=self._pick_region,
                            known_names=[m.name for m in self.monitors])
        self.root.wait_window(dlg)
        if dlg.result:
            self.monitors[idx] = dlg.result
            self._persist_monitors()

    def _repick_monitor(self) -> None:
        idx = self._monitor_index()
        if idx is None:
            return
        region = self._pick_region(f"重新框选「{self.monitors[idx].name}」的区域")
        if region:
            self.monitors[idx].region = list(region)
            self._persist_monitors()
            self._log("info", f"「{self.monitors[idx].name}」区域已更新为 {tuple(region)}")

    def _delete_monitor(self) -> None:
        idx = self._monitor_index()
        if idx is None:
            return
        name = self.monitors[idx].name
        if not messagebox.askyesno("确认删除", f"删除监控窗口「{name}」？"):
            return
        self.monitors.pop(idx)
        self._persist_monitors()
        self._log("info", f"已删除窗口「{name}」。生效窗口里写了该名字的目标需要重新指定。")

    def _toggle_monitor(self) -> None:
        idx = self._monitor_index()
        if idx is None:
            return
        self.monitors[idx].enabled = not self.monitors[idx].enabled
        self._persist_monitors()

    def _monitor_index(self) -> int | None:
        sel = self.mon_tree.selection()
        if not sel or self.mon_tree.item(sel[0], "text").startswith(EMPTY_HINT):
            messagebox.showinfo("提示", "请先在监控窗口列表里选择一项；列表还是空的，先点「新增窗口」。")
            return None
        return self.mon_tree.index(sel[0])

    def _persist_monitors(self) -> None:
        saved = self._save(save_monitors, self.monitors)
        self._refresh_monitors()
        if saved:
            self._log("info", "监控窗口配置已保存。")

    def _save(self, fn, payload) -> bool:
        """写配置文件，失败一定弹窗。

        按钮点了却没落盘、下次启动配置又变回去，是最难自查的一种坑。
        """
        try:
            fn(payload)
            # 自己写盘后刷新基线 mtime，别让监听把这次保存当成“外部改动”又触发一遍
            _, self._config_mtimes = changed_config_files(self._config_files, self._config_mtimes)
            return True
        except ConfigError as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

    def _pick_region(self, instruction: str | None = None) -> tuple[int, int, int, int] | None:
        return RegionPicker(self.root).pick(instruction or "拖拽框选一个直播窗口区域")

    # ---------- 按钮目标 ----------
    def _refresh_targets(self) -> None:
        keep = self._selected_name(self.tree)
        self.tree.delete(*self.tree.get_children())
        rows: dict[str, str] = {}
        for i, t in enumerate(self.targets):
            rows[t.name] = self.tree.insert("", "end", text=t.name, tags=self._state_tags(
                i, t, None), values=(
                "✓" if t.enabled else "",
                "全部" if not t.monitors else ", ".join(t.monitors),
                t.template or "-",
                ", ".join(t.ocr_keywords) or "-",
                f"{t.click_offset[0]},{t.click_offset[1]}",
                f"{t.cooldown_sec:g}",
            ))
        if not self.targets:
            self.tree.insert("", "end",
                             text=f"{EMPTY_HINT}还没有按钮目标 —— 点下方「新增」添加要点的按钮",
                             tags=("hint",))
        if keep in rows:
            self.tree.selection_set(rows[keep])
        self._apply_settings(persist=False)

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel or self.tree.item(sel[0], "text").startswith(EMPTY_HINT):
            messagebox.showinfo("提示", "请先在目标列表里选择一项；列表还是空的，先点「新增」。")
            return None
        return self.tree.index(sel[0])

    def _add_target(self) -> None:
        dlg = TargetDialog(self.root, None, picker_factory=self._pick_region,
                           monitor_names=[m.name for m in self.monitors])
        self.root.wait_window(dlg)
        if dlg.result:
            self.targets.append(dlg.result)
            self._persist_targets()

    def _edit_target(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        dlg = TargetDialog(self.root, self.targets[idx], picker_factory=self._pick_region,
                           monitor_names=[m.name for m in self.monitors])
        self.root.wait_window(dlg)
        if dlg.result:
            self.targets[idx] = dlg.result
            self._persist_targets()

    def _copy_target_to_monitors(self) -> None:
        """把选中目标按窗口拆成多份，方便给每个窗口设不同偏移/冷却。"""
        idx = self._selected_index()
        if idx is None:
            return
        base = self.targets[idx]
        names = [m.name for m in self.monitors if m.enabled] or [m.name for m in self.monitors]
        if not names:
            messagebox.showinfo("提示", "请先添加监控窗口。")
            return
        if not messagebox.askyesno("确认", f"为 {len(names)} 个窗口各生成一份「{base.name}」？"):
            return
        for name in names:
            clone = dataclasses.replace(base, name=f"{base.name}·{name}", monitors=[name])
            self.targets.append(clone)
        # 原目标通常对所有窗口生效，留着会和副本重复点同一个按钮
        base.enabled = False
        self._persist_targets()
        self._log("info", f"已为每个窗口复制「{base.name}」，并停用原目标以免重复点击；"
                          f"可分别调整偏移与冷却。")

    def _delete_target(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        if messagebox.askyesno("确认删除", f"删除目标「{self.targets[idx].name}」？"):
            self.targets.pop(idx)
            self._persist_targets()

    def _toggle_enabled(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self.targets[idx].enabled = not self.targets[idx].enabled
        self._persist_targets()

    def _persist_targets(self) -> None:
        saved = self._save(save_targets, self.targets)
        self._refresh_targets()
        if saved:
            self._log("info", "目标配置已保存。")

    # ---------- 参数 ----------
    def _schedule_apply(self) -> None:
        """拖动滑杆时合并保存，避免每个像素都写盘。"""
        if self._apply_job is not None:
            try:
                self.root.after_cancel(self._apply_job)
            except Exception:
                pass
        self._apply_job = self.root.after(250, self._apply_settings)

    def _apply_settings(self, persist: bool = True) -> None:
        self._apply_job = None
        self.settings.scan_interval_sec = max(0.2, self._snap("scan_interval_sec"))
        self.settings.jitter_sec = max(0.0, self._snap("jitter_sec"))
        self.settings.match_threshold = min(1.0, max(0.3, self._snap("match_threshold")))
        self.settings.ocr_min_score = min(1.0, max(0.1, self._snap("ocr_min_score")))
        self.settings.highlight_sec = self._snap("highlight_sec")
        self.settings.ocr_concurrency = self._int_var("ocr_concurrency", self.settings.ocr_concurrency, 1, 8)
        self.settings.ocr_threads = self._int_var("ocr_threads", self.settings.ocr_threads, 1, self._max_threads)
        self.settings.ocr_min_side = self._int_var("ocr_min_side", self.settings.ocr_min_side, 0, 4096)
        self.settings.ocr_scan_every = self._int_var("ocr_scan_every", self.settings.ocr_scan_every, 1, 10)
        self.settings.ocr_idle_every = self._int_var("ocr_idle_every", self.settings.ocr_idle_every, 1, 10)
        self.settings.click_guard_sec = self._float_var("click_guard_sec",
                                                     self.settings.click_guard_sec, 0.0, 60.0)
        self.settings.use_pydirectinput = bool(self.vars["use_pydirectinput"].get())
        self.settings.humanize_click = bool(self.vars["humanize_click"].get())
        self.settings.raise_before_click = bool(self.vars["raise_before_click"].get())
        self.settings.bring_window_on_screen = bool(self.vars["bring_window_on_screen"].get())
        self.settings.auto_start = bool(self.vars["auto_start"].get())
        self.engine.update_config(self.settings, self.monitors, self.targets)
        if persist:
            self._save(save_settings, self.settings)

    def _float_var(self, key: str, default: float, lo: float, hi: float) -> float:
        """Spinbox 里可能残留半截输入，取不到合法值就沿用上一次的。"""
        try:
            value = float(self.vars[key].get())
        except (TypeError, ValueError):
            return default
        return round(min(hi, max(lo, value)), 2)

    def _int_var(self, key: str, default: int, lo: int, hi: int) -> int:
        """Spinbox 里可能残留半截输入，取不到合法值就沿用上一次的。"""
        try:
            value = int(float(self.vars[key].get()))
        except (TypeError, ValueError):
            return default
        return min(hi, max(lo, value))

    # ---------- 运行控制 ----------
    def toggle_run(self, silent: bool = False) -> None:
        if not self.engine.running and not self._validate_for_start(silent):
            return
        self.engine.toggle()

    def _validate_for_start(self, silent: bool = False) -> bool:
        """启动前的配置检查。自动启动时只写日志，不弹模态框。"""
        enabled_monitors = [m for m in self.monitors if m.enabled]
        enabled_targets = [t for t in self.targets if t.enabled and t.has_detector()]
        if not enabled_monitors:
            self._start_blocked(silent, "请先在「监控窗口」里新增至少一个窗口。")
            return False
        if not enabled_targets:
            self._start_blocked(silent, "请先在「按钮目标」里新增并启用至少一个按钮。")
            return False
        if all(not m.region and not m.tracked for m in enabled_monitors):
            self._log("warn", "没有框选任何区域，正按整屏扫描：又慢又容易点到无关文字，"
                              "建议在「监控窗口」里逐个框选直播画面。")
        self._warn_offscreen_regions(enabled_monitors)
        unbound = [t.name for t in enabled_targets
                   if t.monitors and not any(n in {m.name for m in enabled_monitors} for n in t.monitors)]
        if unbound:
            self._log("warn", f"这些目标的生效窗口当前未启用，不会扫描：{', '.join(unbound)}")
        missing = [t.name for t in enabled_targets
                   if t.template and not os.path.exists(abs_path(t.template))]
        if missing:
            self._log("warn", f"这些目标引用的模板文件不存在，只靠关键词匹配："
                              f"{', '.join(missing)}")
        if not self.engine.ocr.available and any(t.ocr_keywords for t in enabled_targets):
            self._log("warn", "OCR 依赖未安装，仅靠关键词的目标不会生效。")
        return True

    def _warn_offscreen_regions(self, monitors: list[Monitor]) -> None:
        """区域已经完全不在当前屏幕内时提前说明。

        拔掉一副显示器或换了分辨率之后，旧区域会让扫描线程每帧报一次错，
        日志里看起来像「软件坏了」，其实只需要重新框选。
        """
        try:
            capture = ScreenCapture()
            try:
                left, top, width, height = capture.full_region
            finally:
                capture.close()
        except Exception as exc:
            self._log("error", f"读取屏幕尺寸失败，跳过区域校验：{exc}")
            return
        for monitor in monitors:
            if monitor.tracked or not monitor.region:
                continue    # 跟随窗口的每轮现取矩形，旧屏幕上的坐标不算问题
            l, t, w, h = monitor.region_tuple
            if l >= left + width or t >= top + height or l + w <= left or t + h <= top:
                self._log("warn", f"「{monitor.name}」的区域 ({l},{t} {w}x{h}) 已不在当前屏幕 "
                                  f"({width}x{height} @ {left},{top}) 范围内，请重新框选。")

    def _start_blocked(self, silent: bool, message: str) -> None:
        if silent:
            self._log("warn", f"无法自动启动：{message}")
        else:
            messagebox.showinfo("提示", message)

    # ---------- 打开后自动运行 ----------
    def _schedule_auto_start(self) -> None:
        if not self.settings.auto_start:
            return
        self._log("info", f"已勾选「打开软件后自动开始运行」，{AUTO_START_DELAY:g} 秒后启动。")
        self._auto_job = self.root.after(int(AUTO_START_DELAY * 1000), self._auto_start)

    def _auto_start(self) -> None:
        self._auto_job = None
        if self._closing or self.engine.running:
            return
        if not self._validate_for_start(silent=True):
            return
        self.engine.start()
        # 高亮层提前建好：第一次命中时才建窗口的话，那层可能正好盖在要点的按钮上
        self.overlay.prewarm()

    def _register_hotkey(self) -> None:
        ok = self.hotkeys.register({self.settings.hotkey_toggle: self._on_hotkey})
        if not ok:
            self.root.bind(f"<{self.settings.hotkey_toggle.upper()}>", lambda e: self.toggle_run())
            self._log("warn", "未安装 pynput，热键仅在窗口获得焦点时有效。")

    def _on_hotkey(self) -> None:
        """pynput 的回调跑在它的监听线程里，只能塞事件，不能直接动界面。

        tkinter 不是线程安全的，跨线程调用偶尔会让整个进程直接消失。
        """
        self.events.put(Event("hotkey"))

    def _reset_cooldowns(self) -> None:
        self.engine.reset_cooldowns()
        self._log("info", "已重置所有窗口的冷却计时。")

    def _open_log_dir(self) -> None:
        path = os.path.dirname(self.run_logger.path) if self.run_logger else os.path.join(BASE_DIR, "logs")
        try:
            os.makedirs(path, exist_ok=True)
            subprocess.Popen(["explorer", path])
        except Exception as exc:
            self._log("error", f"无法打开日志目录: {exc}")

    def _open_config_dir(self) -> None:
        path = os.path.join(BASE_DIR, "config")
        try:
            subprocess.Popen(["explorer", path])
        except Exception as exc:
            self._log("error", f"无法打开配置目录: {exc}")

    def _test_scan(self) -> None:
        """逐窗口截一帧跑检测但不点击，用来验证阈值和模板。

        整段跑在主线程上，4K 一帧可能要几秒，所以先禁用按钮并写清进度，
        免得看起来像软件卡死了。
        """
        set_process_dpi_awareness()
        enabled = [m for m in self.monitors if m.enabled]
        if not enabled:
            messagebox.showinfo("提示", "没有启用的监控窗口。")
            return
        self.test_btn.configure(state="disabled")
        capture = None
        try:
            capture = ScreenCapture()
            self._refresh_self_windows()   # 空闲时不刷新，测试前补一次，免得用上次运行的旧句柄
            for monitor in enabled:
                self._log("info", f"[{monitor.name}] 测试扫描中（首次识别要加载 OCR 模型，可能要几秒）…")
                detector = Detector(self.settings, monitor, self.engine.ocr, CooldownTracker(),
                                    self.engine.guard)
                resolved = resolve_region(monitor)
                if not resolved.ok:
                    self._log("warn", f"[{monitor.name}] {resolved.error}")
                    continue
                try:
                    frame = capture.grab(resolved.box)
                except Exception as exc:
                    self._log("error", f"[{monitor.name}] 截图失败: {exc}")
                    continue
                hits = Detector.dedupe_same_location(detector.scan(frame, self.targets,
                                                                    region=resolved.box))
                for err in detector.pop_errors():
                    self._log("warn", err)
                if not hits:
                    self._log("warn", f"[{monitor.name}] 未命中任何目标。可降低匹配阈值，"
                                      f"或用 selftest.py --ocr-only 看识别到的文字。")
                for det in hits:
                    self._log("info", f"[{monitor.name}] 测试命中「{det.target_name}」"
                                      f"({det.source}, {det.confidence:.2f}) → 将点击 "
                                      f"({det.abs_x}, {det.abs_y})")
                    ox, oy = (resolved.box[0], resolved.box[1]) if resolved.box else (0, 0)
                    x, y, w, h = det.box
                    self.overlay.show((ox + x, oy + y, w, h), f"{monitor.name}·{det.target_name}", 1.5)
        except Exception as exc:
            self._log("error", f"测试扫描中断: {exc}")
        finally:
            if capture is not None:
                capture.close()
            self.test_btn.configure(state="normal")

    # ---------- 事件与日志 ----------
    def _refresh_self_windows(self) -> None:
        """登记本程序自己的顶层窗口，扫描线程据此丢弃「点到自己」的命中。"""
        hwnds = []
        for win in (self.root, *self.root.winfo_children()):
            if win is not self.root and not isinstance(win, tk.Toplevel):
                continue        # root 的直接子控件是普通 frame，不是独立窗口
            try:
                if not win.winfo_viewable() or win.overrideredirect():
                    continue    # 高亮层/框选层无边框且点击穿透，不算「自己」
                hwnds.append(root_hwnd(win.winfo_id()))
            except tk.TclError:
                continue        # 对话框正在销毁
        self.engine.guard.update(hwnds)

    def _watch_config_files(self) -> None:
        """外部进程（如 selftest.py --pick --write）改了 config/*.yaml 就自动应用，免重启。

        只在确有新 mtime 且能正常解析时才热加载；解析失败保留原状只提示，避免误触。
        自己经 _save() 写盘后会刷新基线 mtime，所以不会被自己的保存反复触发。"""
        changed, new_mtimes = changed_config_files(self._config_files, self._config_mtimes)
        if not changed:
            return
        self._config_mtimes = new_mtimes
        report = self._config_warnings.append
        try:
            settings = load_settings(report)
            monitors = load_monitors(report)
            targets = load_targets(report)
        except ConfigError as exc:
            self._log("warn", "配置文件刚被改但有错误，仍用当前配置：" + str(exc))
            return
        self.settings, self.monitors, self.targets = settings, monitors, targets
        self.engine.update_config(self.settings, self.monitors, self.targets)
        self._refresh_monitors()
        self._refresh_targets()
        self._log("info", "检测到配置改动（" + "、".join(changed) + "），已自动应用")

    def _pump_events(self) -> None:
        # 只在跑着的时候刷新：空闲时没有扫描线程会读这份句柄
        if self.engine.running:
            self._refresh_self_windows()
        summary = None
        try:
            while True:
                ev = self.events.get_nowait()
                if ev.kind == "log":
                    # 引擎已自己写盘，这里只负责显示
                    self._log(ev.payload["level"], ev.payload["message"], to_file=False)
                elif ev.kind == "highlight":
                    self.overlay.show(ev.payload["box"], ev.payload["label"], ev.payload["sec"])
                elif ev.kind == "state":
                    self._set_running(ev.payload["running"])
                elif ev.kind == "monitor":
                    summary = self._update_monitor_row(ev.payload) or summary
                elif ev.kind == "hotkey":
                    # 热键线程只会往队列里塞这一条，动作在主线程完成
                    self.toggle_run()
        except queue.Empty:
            pass
        if summary is not None:
            self.stats_var.set(summary)
        self._watch_config_files()
        self.root.after(150, self._pump_events)

    def _set_running(self, running: bool) -> None:
        """按钮和状态胶囊成对切换，别出现「■ 停止」配灰色空闲胶囊。"""
        self.run_btn.configure(text="■ 停止" if running else "▶ 启动",
                               style="Stop.TButton" if running else "Primary.TButton")
        self.status_var.set("● 扫描中" if running else "● 已停止")
        self.status_lbl.configure(style="StatusRun.TLabel" if running else "StatusIdle.TLabel")

    def _update_monitor_row(self, state: dict) -> str | None:
        item = self._monitor_rows.get(state["name"])
        monitor = next((m for m in self.monitors if m.name == state["name"]), None)
        if item and monitor:
            self.mon_tree.item(item, values=self._monitor_values(monitor, state),
                               tags=self._state_tags(self.mon_tree.index(item), monitor, state))
        stats = self.engine.stats
        return (f"{stats['monitors']} 窗口 · 扫描 {stats['scans']} · "
                f"命中 {stats['hits']} · 点击 {stats['clicks']}")

    def _log(self, level: str, message: str, to_file: bool = True) -> None:
        stamp = time.strftime("%H:%M:%S")
        # 界面自己的日志落盘；引擎事件已经在 Engine.log 里写过了，to_file=False 跳过，
        # 否则同一条会写两遍
        if to_file and self.run_logger is not None:
            self.run_logger.write(level, message)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {message}\n", (level,))
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_text.delete("1.0", f"{lines - 400}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        self._closing = True
        if self._auto_job is not None:
            self.root.after_cancel(self._auto_job)
            self._auto_job = None
        if self.engine.running:
            self.engine.stop()
        self.hotkeys.unregister()
        self.overlay.close()
        failed = []
        for fn, payload, label in ((save_settings, self.settings, "settings.yaml"),
                                   (save_monitors, self.monitors, "monitors.yaml"),
                                   (save_targets, self.targets, "targets.yaml")):
            try:
                fn(payload)
            except ConfigError as exc:
                failed.append(f"{label}：{exc}")
        if failed:
            # 界面马上要拆，日志框里写了也看不见；退出前弹窗说明哪些改动没落盘
            messagebox.showwarning("部分配置未保存", "\n".join(failed))
        # 日志在独立写线程里，退出前把队列尾巴冲完，否则最后几行会丢
        if self.run_logger is not None:
            self.run_logger.stop()
        self.root.destroy()
