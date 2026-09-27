"""区域框选：全屏半透明遮罩，拖拽选出一个矩形区域。"""
from __future__ import annotations

import tkinter as tk

from core.capture import ScreenCapture, rect_to_region


class RegionPicker:
    """模态框选工具。

    返回 (left, top, width, height) 绝对屏幕坐标；按 Esc 或选区过小时返回 None。
    """

    MIN_SIZE = 8

    def __init__(self, parent: tk.Misc) -> None:
        self.parent = parent
        self.result: tuple[int, int, int, int] | None = None
        self._origin: tuple[int, int] | None = None

    def pick(self, instruction: str = "拖拽鼠标框选区域，松开即确认；按 Esc 取消") -> tuple[int, int, int, int] | None:
        self.result = None
        self._origin = None
        capture = ScreenCapture()
        try:
            left, top_, width, height = capture.full_region
        finally:
            # mss 实例握着屏幕 DC，不 close 就是每框选一次漏一个
            capture.close()

        win = tk.Toplevel(self.parent)
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.30)
        win.configure(bg="black", cursor="cross")
        win.focus_force()

        canvas = tk.Canvas(win, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(width // 2, 40, text=instruction, fill="white",
                           font=("Microsoft YaHei", 13))
        rect_id = canvas.create_rectangle(0, 0, 0, 0, outline="#00ff88", width=2)

        def to_local(root_x: int, root_y: int) -> tuple[int, int]:
            return root_x - left, root_y - top_

        def on_press(event):
            self._origin = (event.x_root, event.y_root)
            canvas.coords(rect_id, *to_local(event.x_root, event.y_root),
                          *to_local(event.x_root, event.y_root))

        def on_drag(event):
            if self._origin is None:
                return
            x1, y1 = to_local(*self._origin)
            x2, y2 = to_local(event.x_root, event.y_root)
            canvas.coords(rect_id, x1, y1, x2, y2)

        def on_release(event):
            if self._origin is None:
                return
            region = rect_to_region(self._origin[0], self._origin[1], event.x_root, event.y_root)
            self.result = region if min(region[2], region[3]) >= self.MIN_SIZE else None
            win.destroy()

        def on_key(event):
            if event.keysym == "Escape":
                self.result = None
                win.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        win.bind("<Key>", on_key)

        win.grab_set()
        self.parent.wait_window(win)
        return self.result
