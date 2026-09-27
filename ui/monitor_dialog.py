"""监控窗口编辑对话框：名称、区域/按标题跟随、独立扫描节奏。"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox

from core.capture import cursor_pos, list_windows, window_at, window_process, window_signature, _visible_rect
from core.config import Monitor

# 倒计时秒数：够把鼠标从对话框挪到直播窗口上停稳
IDENTIFY_SECONDS = 5
# 终端 / 控制台宿主进程：识别时鼠标停在这些窗口上一律不算数
# （和 selftest.py --pick 同一套名单，免得把终端身份当成直播窗口）
_TERMINAL_PROCS = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "windowsterminal.exe", "windowsterminalpreview.exe",
    "mintty.exe", "conemu.exe", "conemu64.exe",
}


class MonitorDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, monitor: Monitor | None = None,
                picker_factory=None, known_names: list[str] | None = None) -> None:
        super().__init__(parent)
        self.title("新增监控窗口" if monitor is None else f"编辑窗口：{monitor.name}")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._picker_factory = picker_factory
        self._known = [n for n in (known_names or []) if monitor is None or n != monitor.name]
        self.result: Monitor | None = None
        # 用「英寸」而不是像素定换行宽度：tk 会按当前 DPI 折算，高缩放下提示不会撑成一条长线
        self._wrap = int(round(self.winfo_fpixels("3.4i")))

        src = monitor or Monitor(name="")
        self._region: list[int] | None = list(src.region) if src.region else None

        row = 0
        ttk.Label(self, text="窗口名称").grid(row=row, column=0, sticky="w", padx=8, pady=(10, 2))
        self.name_var = tk.StringVar(value=src.name)
        ttk.Entry(self, textvariable=self.name_var, width=28).grid(row=row, column=1, sticky="we", padx=8)
        row += 1
        row += 1

        ttk.Label(self, text="监控区域").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        box = ttk.Frame(self)
        box.grid(row=row, column=1, sticky="w", padx=8)
        ttk.Button(box, text="框选区域…", command=self._pick).pack(side="left")
        ttk.Button(box, text="清除(全屏)", command=self._clear_region).pack(side="left", padx=4)
        ttk.Button(box, text="自动识别窗口身份…", command=self._identify_window).pack(side="left", padx=4)
        row += 1

        self.region_var = tk.StringVar(value=self._describe_region())
        ttk.Label(self, textvariable=self.region_var, style="Value.TLabel").grid(
            row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="窗口标题包含").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.title_var = tk.StringVar(value=src.window_title)
        ttk.Entry(self, textvariable=self.title_var, width=28).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="填了就跟着这个窗口走（挪位置、改大小都不用重框），忽略上面的框选区域；"
                             "命中多个窗口时取面积最大的那个",
                  style="Hint.TLabel", wraplength=self._wrap).grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="窗口类名包含").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.class_var = tk.StringVar(value=src.window_class)
        ttk.Entry(self, textvariable=self.class_var, width=28).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="进程名包含").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.process_var = tk.StringVar(value=src.window_process)
        ttk.Entry(self, textvariable=self.process_var, width=28).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        self._id_note = ttk.Label(self, text="嫌填类名/进程名麻烦？点上面的「自动识别窗口身份」，"
                                             "倒计时几秒内把鼠标挪到直播窗口上停好别动即可。",
                                  style="Hint.TLabel", wraplength=self._wrap)
        self._id_note.grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="三项身份填几项就要求同时满足几项。直播窗口的标题常是房间号、一场一变，"
                             "所以可以把标题留空，只填类名或进程名。"
                             "`python selftest.py --windows` 会把当前所有窗口的三顷身份列出来。",
                  style="Hint.TLabel", wraplength=self._wrap).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="底部裁切(像素)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.crop_var = tk.StringVar(value="" if not src.crop_bottom else str(src.crop_bottom))
        ttk.Entry(self, textvariable=self.crop_var, width=10).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="右侧裁切(像素)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.crop_right_var = tk.StringVar(
            value="" if not getattr(src, "crop_right", 0) else str(src.crop_right))
        ttk.Entry(self, textvariable=self.crop_right_var, width=10).grid(
            row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="左侧裁切(像素)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.crop_left_var = tk.StringVar(
            value="" if not getattr(src, "crop_left", 0) else str(src.crop_left))
        ttk.Entry(self, textvariable=self.crop_left_var, width=10).grid(
            row=row, column=1, sticky="w", padx=8)
        row += 1
        row += 1
        ttk.Label(self, text="跟随时从窗口底部裁掉这么多像素：直播窗口下方的弹幕里也有「签到/领取」，"
                             "裁掉能避免把弹幕当成按钮。微信视频号直播间的聊天列表在右边一列，"
                             "那种布局要把「右侧裁切」填成聊天列的宽度（先用 "
                             "`python selftest.py --ocr-only` 看聊天文字的 x 坐标）——"
                             "既不会被群名弹幕点错，OCR 还少认一半文本框。",
                  style="Hint.TLabel", wraplength=self._wrap).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        self.bg_var = tk.BooleanVar(value=src.background_capture)
        ttk.Checkbutton(self, text="后台抓窗口(PrintWindow)", variable=self.bg_var,
                        style="Toolbutton").grid(
            row=row, column=1, sticky="w", padx=8, pady=2)
        row += 1
        ttk.Label(self, text="实验性：窗口被别的窗口盖住、或不肯让出焦点时也能抓到画面，"
                             "不会因为切到前台而打断直播。对 GPU/DirectX 渲染的视频可能"
                             "抓到黑屏，那时会自动回退整屏截图；只在按身份跟随窗口时可用。",
                  style="Hint.TLabel", wraplength=self._wrap).grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="扫描间隔(秒)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.interval_var = tk.StringVar(value="" if not src.scan_interval_sec else str(src.scan_interval_sec))
        ttk.Entry(self, textvariable=self.interval_var, width=10).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="留空用全局值。窗口多时可适当调大，减轻整机负担",
                  style="Hint.TLabel").grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="OCR 边长(像素)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.max_side_var = tk.StringVar(value="" if not src.ocr_max_side else str(src.ocr_max_side))
        ttk.Entry(self, textvariable=self.max_side_var, width=10).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="小窗口想看得更清可单独调大；纯模板目标忽略该项",
                  style="Hint.TLabel").grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        self.enabled_var = tk.BooleanVar(value=src.enabled)
        ttk.Checkbutton(self, text="启用该窗口", variable=self.enabled_var,
                        style="Toolbutton").grid(
            row=row, column=1, sticky="w", padx=8, pady=(8, 0))
        row += 1

        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, columnspan=2, pady=14)
        ttk.Button(btns, text="确定", command=self._on_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left")

    def _read_cursor_window(self):
        """读一次鼠标下的窗口；停在桌面/终端/本程序自己身上时返回 None。"""
        point = cursor_pos()
        if point is None:
            return None
        hwnd = window_at(point[0], point[1])
        if not hwnd:
            return None
        proc = (window_process(hwnd) or "").lower()
        if proc in _TERMINAL_PROCS or proc == os.path.basename(sys.executable).lower():
            return None
        box = _visible_rect(hwnd)
        if box is None:
            return None
        return window_signature(hwnd), box

    def _identify_window(self) -> None:
        """倒计时采样鼠标下的窗口，自动填好标题/类名/进程名。"""
        self.grab_release()
        self.withdraw()
        tip = tk.Toplevel(self)
        tip.title("识别窗口")
        tip.transient(self)
        tip.attributes("-topmost", True)
        tip.resizable(False, False)
        label = ttk.Label(tip, style="Heading.TLabel")
        label.pack(padx=24, pady=18)
        tip.update_idletasks()
        tw, th = tip.winfo_width(), tip.winfo_height()
        tip.geometry("+%d+%d" % ((tip.winfo_screenwidth() - tw) // 2,
                                 (tip.winfo_screenheight() - th) // 3))
        tip.grab_set()
        state = {"left": IDENTIFY_SECONDS, "sample": None}

        def tick() -> None:
            state["left"] -= 1
            sample = self._read_cursor_window()
            if sample is not None:
                state["sample"] = sample
            if state["left"] > 0:
                label.config(text="请在 %d 秒内把鼠标移到直播窗口画面上，停好别动…"
                              % state["left"])
                self.after(1000, tick)
                return
            tip.grab_release()
            tip.destroy()
            self.deiconify()
            self.lift()
            self.grab_set()
            self._finish_identify(state["sample"])

        label.config(text="请在 %d 秒内把鼠标移到直播窗口画面上，停好别动…" % state["left"])
        self.after(1000, tick)

    def _finish_identify(self, sample) -> None:
        if sample is None:
            self._id_note.config(text="没锁到窗口：倒计时里鼠标没停在可见的直播窗口上，再试一次。")
            return
        (title, klass, proc), (left, top, width, height) = sample
        if not klass and not proc:
            self._id_note.config(text="这个窗口读不到类名和进程名，没法按身份跟踪，请改用「框选区域」。")
            return
        self.title_var.set(title)
        self.class_var.set(klass)
        self.process_var.set(proc)
        klass_l, proc_l = klass.lower(), proc.lower()
        hits = sum(1 for _t, k, p, _b in list_windows(with_identity=True)
                   if klass_l in (k or "").lower() and proc_l in (p or "").lower())
        note = "已识别 %dx%d @ %d,%d：类名「%s」/ 进程「%s」" % (width, height, left, top, klass, proc)
        if hits > 1:
            note += "（当前有 %d 个可见窗口同时命中这组身份，保存后取面积最大的，"                     "建议补标题收窄）" % hits
        self._id_note.config(text=note)

    def _pick(self) -> None:
        if self._picker_factory is None:
            messagebox.showwarning("提示", "当前环境不支持框选。")
            return
        region = self._picker_factory("框选这个直播窗口（视频画面）所在区域")
        self.grab_set()
        if region:
            self._region = list(region)
            self.region_var.set(self._describe_region())

    def _clear_region(self) -> None:
        self._region = None
        self.region_var.set(self._describe_region())

    def _describe_region(self) -> str:
        if not self._region:
            return "未设置 → 整个屏幕"
        l, t, w, h = self._region
        return f"left={l}, top={t}, {w}x{h}"

    def _on_ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "请填写窗口名称。")
            return
        if name in self._known:
            messagebox.showwarning("提示", f"名称「{name}」已被其他监控窗口占用。")
            return
        interval = None
        raw = self.interval_var.get().strip()
        if raw:
            try:
                interval = max(0.2, float(raw))
            except ValueError:
                messagebox.showwarning("提示", "扫描间隔必须是数字或留空。")
                return
        max_side = None
        raw_side = self.max_side_var.get().strip()
        if raw_side:
            try:
                max_side = max(320, int(float(raw_side)))
            except ValueError:
                messagebox.showwarning("提示", "OCR 边长必须是数字或留空。")
                return
        crop = 0
        raw_crop = self.crop_var.get().strip()
        if raw_crop:
            try:
                crop = max(0, int(float(raw_crop)))
            except ValueError:
                messagebox.showwarning("提示", "底部裁切必须是数字或留空。")
                return
        crop_right = 0
        raw_right = self.crop_right_var.get().strip()
        if raw_right:
            try:
                crop_right = max(0, int(float(raw_right)))
            except ValueError:
                messagebox.showwarning("提示", "右侧裁切必须是数字或留空。")
                return
        crop_left = 0
        raw_left = self.crop_left_var.get().strip()
        if raw_left:
            try:
                crop_left = max(0, int(float(raw_left)))
            except ValueError:
                messagebox.showwarning("提示", "左侧裁切必须是数字或留空。")
                return
        title = self.title_var.get().strip()
        klass = self.class_var.get().strip()
        process = self.process_var.get().strip()
        if (background or crop or crop_right or crop_left) and not (title or klass or process):
            messagebox.showwarning("提示", "三侧裁切、后台抓窗口只在按身份跟随窗口时生效，"
                                    "请先填窗口标题/类名/进程名中的任何一项。")
            return
        self.result = Monitor(name=name, region=self._region, enabled=bool(self.enabled_var.get()),
                              scan_interval_sec=interval, ocr_max_side=max_side,
                              window_title=title, window_class=klass,
                              window_process=process, crop_bottom=crop,
                              crop_right=crop_right, crop_left=crop_left,
                              background_capture=background)
        self.destroy()
