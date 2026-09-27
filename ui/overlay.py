"""检测高亮层：在命中的按钮上画框，短暂显示后自动消失，且不阻挡鼠标。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
import tkinter as tk

from core.capture import ScreenCapture

TRANSPARENT = "#010203"
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20


class HighlightOverlay:
    """覆盖整个虚拟屏幕的透明层，仅用于视觉反馈。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._win: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._left = 0
        self._top = 0
        self._items: dict[int, float] = {}  # canvas item id -> 过期时间
        self._tick_job: str | None = None

    def prewarm(self) -> None:
        """启动时就把高亮层建好。

        否则第一次命中时才建窗口，那几百毫秒里这层可能正好盖到要点的按钮上面。
        """
        self._ensure_window()

    def show(self, rect: tuple[int, int, int, int], label: str, sec: float) -> None:
        self._ensure_window()
        if self._canvas is None:
            return
        # 每次显示前都重设一遍「鼠标透透」：Tk 在映射窗口、设置 -topmost 时会把扩展样式重写一遍，
        # 把我们加的 WS_EX_TRANSPARENT 推掉。这层一拢住鼠标，命中的按钮就点不到（只收得到 MOUSEMOVE，
        # LBUTTONDOWN 全被它吃了）；WindowFromPoint 也会回“命中点被别的窗口盖着”，反正造成误判。
        if self._win is not None:
            _make_click_through(self._win)
        x, y, w, h = rect
        x1, y1 = x - self._left, y - self._top
        box = self._canvas.create_rectangle(x1, y1, x1 + w, y1 + h,
                                           outline="#00ff88", width=3)
        text = self._canvas.create_text(x1, max(12, y1 - 4), text=label, anchor="sw",
                                       fill="#00ff88", font=("Microsoft YaHei", 10, "bold"))
        deadline = time.monotonic() + max(0.1, sec)
        self._items[box] = deadline
        self._items[text] = deadline
        if self._tick_job is None:
            self._tick_job = self.root.after(120, self._tick)

    def close(self) -> None:
        if self._tick_job is not None:
            try:
                self.root.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
        self._win = None
        self._canvas = None
        self._items = {}

    # ---------- 内部 ----------
    def _ensure_window(self) -> None:
        if self._win is not None:
            return
        capture = ScreenCapture()
        try:
            self._left, self._top, width, height = capture.full_region
        finally:
            capture.close()

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.geometry(f"{width}x{height}+{self._left}+{self._top}")
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", TRANSPARENT)
        canvas = tk.Canvas(win, bg=TRANSPARENT, highlightthickness=0,
                           width=width, height=height)
        canvas.pack(fill="both", expand=True)
        self._win, self._canvas = win, canvas
        # 高亮层要在第一次显示前就建好、并让它对鼠标透透：命中的第一点和建窗口只差
        # 几百毫秒，建窗那一刻会把这层盖到直播窗上，点击就被它吞了。
        _make_click_through(win)
        win.after_idle(lambda w=win: _make_click_through(w))

    def _tick(self) -> None:
        self._tick_job = None
        if self._canvas is None:
            return
        now = time.monotonic()
        for item in [i for i, deadline in self._items.items() if deadline <= now]:
            try:
                self._canvas.delete(item)
            except tk.TclError:
                pass
            self._items.pop(item, None)
        if self._items:
            self._tick_job = self.root.after(120, self._tick)


def _make_click_through(win: tk.Toplevel) -> None:
    """让高亮层对鼠标点击透明，避免挡住真实按钮、吞掉整台机器的鼠标。

    只靠 -transparentcolor 色键不够：色键只管「画出来看不见」，鼠标命中仍归这层。
    winfo_id() 拿到的是 Tk 的内容子窗口，真正占满全屏、接收鼠标的是它的根窗口；
    SetWindowLongW 设在子窗口上等于没设。2026-09-26 真机实测：软件一跑起来
    WindowFromPoint 全屏回这层、鼠标点不动任何窗口（用户原话：“每次运行后鼠标无法操作”），
    直播窗的点击也全被它吞掉。所以先用 GetAncestor(GA_ROOT) 找到真正的顶层窗口再补位。
    """
    try:
        hwnd = int(win.winfo_id())
    except Exception:
        return
    if not hwnd:
        return
    try:
        user32 = ctypes.WinDLL("user32")      # 私用引用，别咋 core.capture 设的 argtypes
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        root = int(user32.GetAncestor(wintypes.HWND(hwnd), 2) or 0)   # 2 = GA_ROOT
        # 根窗口（占满全屏接鼠标的那个）和内容子窗口（画高亮框的那个）都要透明：
        # 只设根窗口，子窗口上画出来的框仍然会吞掉等一下点；只设子窗口，整屏还是点不动。
        for target in {root, hwnd}:
            style = user32.GetWindowLongW(wintypes.HWND(target), GWL_EXSTYLE)
            user32.SetWindowLongW(wintypes.HWND(target), GWL_EXSTYLE,
                                  style | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
    except Exception:
        # 设不上也不自己崩：色键还在，最多是点击被吞，不能把整个界面带崩
        pass
