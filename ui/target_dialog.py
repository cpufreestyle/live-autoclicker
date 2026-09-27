"""按钮目标编辑对话框：名称、模板、OCR 关键词、偏移、冷却。"""
from __future__ import annotations

import os
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from core.config import Target
from core.templates import preview_size, save_region_as_template, suggest_relative_path, templates_dir


class TargetDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, target: Target | None = None,
                 picker_factory=None, monitor_names: list[str] | None = None) -> None:
        super().__init__(parent)
        self.title("新增按钮目标" if target is None else f"编辑目标：{target.name}")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._picker_factory = picker_factory
        self._monitor_names = list(monitor_names or [])
        self.result: Target | None = None

        src = target or Target(name="")
        row = 0
        ttk.Label(self, text="按钮名称").grid(row=row, column=0, sticky="w", padx=8, pady=(10, 2))
        self.name_var = tk.StringVar(value=src.name)
        ttk.Entry(self, textvariable=self.name_var, width=38).grid(row=row, column=1, sticky="we", padx=8)
        row += 1

        ttk.Label(self, text="模板图片").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        box = ttk.Frame(self)
        box.grid(row=row, column=1, sticky="we", padx=8)
        self.template_var = tk.StringVar(value=src.template)
        ttk.Entry(box, textvariable=self.template_var, width=26).pack(side="left")
        ttk.Button(box, text="框选截图…", command=self._capture_template).pack(side="left", padx=4)
        ttk.Button(box, text="选文件…", command=self._choose_file).pack(side="left")
        row += 1

        self.size_var = tk.StringVar(value=self._describe_template(src.template))
        ttk.Label(self, textvariable=self.size_var, style="Hint.TLabel").grid(
            row=row, column=1, sticky="w", padx=8)
        self.template_var.trace_add("write", lambda *_: self.size_var.set(
            self._describe_template(self.template_var.get())))
        row += 1

        ttk.Label(self, text="OCR 关键词").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.keywords_var = tk.StringVar(value=", ".join(src.ocr_keywords))
        ttk.Entry(self, textvariable=self.keywords_var, width=38).grid(row=row, column=1, sticky="we", padx=8)
        row += 1
        ttk.Label(self, text="多个词用逗号分隔，如：签到, 立即签到",
                  style="Hint.TLabel").grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="点击偏移 (dx, dy)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.offset_var = tk.StringVar(value=f"{src.click_offset[0]}, {src.click_offset[1]}")
        ttk.Entry(self, textvariable=self.offset_var, width=14).grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="冷却时间 (秒)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        # 用 StringVar：Spinbox 里留半截输入时 DoubleVar.get() 会抛 TclError
        self.cooldown_var = tk.StringVar(value=f"{src.cooldown_sec:g}")
        ttk.Spinbox(self, from_=0, to=3600, increment=5, textvariable=self.cooldown_var,
                    width=10).grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="生效窗口").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.monitors_var = tk.StringVar(value=", ".join(src.monitors))
        ttk.Entry(self, textvariable=self.monitors_var, width=38).grid(row=row, column=1, sticky="we", padx=8)
        row += 1
        hint = "留空=所有监控窗口都扫；多窗口用同一按钮时留空最省事"
        if self._monitor_names:
            hint += f"。现有窗口：{', '.join(self._monitor_names)}"
        ttk.Label(self, text=hint, style="Hint.TLabel", wraplength=330,
                  justify="left").grid(row=row, column=1, sticky="w", padx=8)
        row += 1

        ttk.Label(self, text="匹配阈值 (0-1)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        self.threshold_var = tk.StringVar(value="" if src.match_threshold is None
                                          else str(src.match_threshold))
        ttk.Entry(self, textvariable=self.threshold_var, width=10).grid(row=row, column=1, sticky="w", padx=8)
        row += 1
        ttk.Label(self, text="留空则使用全局阈值（0.3–1.0）", style="Hint.TLabel").grid(
            row=row, column=1, sticky="w", padx=8)
        row += 1

        self.enabled_var = tk.BooleanVar(value=src.enabled)
        ttk.Checkbutton(self, text="启用该目标", variable=self.enabled_var,
                        style="Toolbutton").grid(
            row=row, column=1, sticky="w", padx=8, pady=(8, 0))
        row += 1

        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, columnspan=2, pady=14)
        ttk.Button(btns, text="确定", command=self._on_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left")

    # ---------- 交互 ----------
    def _capture_template(self) -> None:
        if self._picker_factory is None:
            messagebox.showwarning("提示", "当前环境不支持框选。")
            return
        region = self._picker_factory("框选按钮图案本身（尽量只框住按钮，不含文字边缘）")
        self.grab_set()  # 框选层的模态会抢占，取回后恢复本对话框模态
        if not region:
            return
        name = self.name_var.get().strip() or "button"
        rel = suggest_relative_path(name)
        try:
            save_region_as_template(region, rel)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.template_var.set(rel)

    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择模板图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")])
        if not path:
            return
        rel = suggest_relative_path(self.name_var.get().strip() or "button")
        dest = os.path.join(templates_dir(), os.path.basename(rel))
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            messagebox.showerror("复制失败", f"无法把 {os.path.basename(path)} 复制到模板目录：\n{exc}")
            return
        self.template_var.set(rel)

    def _describe_template(self, rel: str) -> str:
        if not rel:
            return "未设置模板时，仅用 OCR 关键词识别"
        size = preview_size(rel)
        return f"模板尺寸: {size[0]}x{size[1]}" if size else f"模板文件不存在: {rel}"

    def _on_ok(self) -> None:
        name = self.name_var.get().strip()
        keywords = [k.strip() for k in self.keywords_var.get().replace("，", ",").split(",") if k.strip()]
        if not name:
            messagebox.showwarning("提示", "请填写按钮名称。")
            return
        if not self.template_var.get().strip() and not keywords:
            messagebox.showwarning("提示", "模板图片与 OCR 关键词至少填一项。")
            return
        try:
            dx, dy = _parse_pair(self.offset_var.get())
        except ValueError:
            messagebox.showwarning("提示", "偏移格式应为「dx, dy」，例如 0, 0。")
            return
        try:
            cooldown = _parse_cooldown(self.cooldown_var.get())
        except ValueError:
            messagebox.showwarning("提示", "冷却时间应是数字（秒），例如 30。")
            return
        threshold_raw = self.threshold_var.get().strip()
        threshold = None
        if threshold_raw:
            try:
                threshold = max(0.3, min(1.0, float(threshold_raw)))
            except ValueError:
                messagebox.showwarning("提示", "匹配阈值必须是 0.3 到 1.0 之间的数字。")
                return

        monitors = [m.strip() for m in self.monitors_var.get().replace("，", ",").split(",") if m.strip()]
        unknown = [m for m in monitors if self._monitor_names and m not in self._monitor_names]
        if unknown:
            messagebox.showwarning(
                "提示",
                "以下生效窗口不存在，将匹配不到任何画面：" + ", ".join(unknown) +
                "\n留空表示对所有监控窗口生效。")
            return

        self.result = Target(
            name=name,
            template=self.template_var.get().strip(),
            ocr_keywords=keywords,
            click_offset=[dx, dy],
            cooldown_sec=cooldown,
            enabled=bool(self.enabled_var.get()),
            match_threshold=threshold,
            monitors=monitors,
        )
        self.destroy()


def _parse_pair(text: str) -> tuple[int, int]:
    parts = [p for p in text.replace("，", ",").split(",") if p.strip() != ""]
    if len(parts) != 2:
        raise ValueError("需要两个数字")
    return int(float(parts[0])), int(float(parts[1]))


def _parse_cooldown(text: str) -> float:
    """留空当 30 秒：清空输入框比填个默认值常见。"""
    raw = text.strip()
    return 30.0 if not raw else max(0.0, float(raw))
