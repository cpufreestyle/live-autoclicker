"""程序入口：设置 DPI 感知并启动主界面。"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox

from core.capture import set_process_dpi_awareness
from core.config import ConfigError
from ui.main_window import MainWindow


def main() -> int:
    set_process_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法创建图形界面: {exc}", file=sys.stderr)
        return 1
    try:
        MainWindow(root)
    except ConfigError as exc:
        # 打包成 exe 后没有控制台，配置读坏了不弹窗就等于「双击没反应」
        _report(root, "配置文件有问题", str(exc))
        root.destroy()
        return 1
    except Exception as exc:
        _report(root, "启动失败", f"{type(exc).__name__}: {exc}")
        root.destroy()
        return 1
    root.mainloop()
    return 0


def _report(root: tk.Tk, title: str, message: str) -> None:
    print(f"{title}: {message}", file=sys.stderr)
    try:
        root.withdraw()
        messagebox.showerror(title, message)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
