"""鼠标点击模块：支持 pyautogui 与 pydirectinput 两种后端，并可选人性化移动。"""
from __future__ import annotations

import random
import threading
import time

import pyautogui

try:
    import pydirectinput
except Exception:  # pragma: no cover
    pydirectinput = None

# 允许在极端误操作时通过移动到左上角中断 pyautogui
pyautogui.FAILSAFE = True


class Clicker:
    """执行实际点击。

    backend="pyautogui" 适用于绝大多数窗口；
    backend="pydirectinput" 发送硬件级输入事件，适用于部分直播客户端/游戏化页面。
    """

    def __init__(self, backend: str = "pyautogui", humanize: bool = True,
                 move_duration: tuple[float, float] = (0.08, 0.25),
                 pre_click_delay: tuple[float, float] = (0.02, 0.12)) -> None:
        self.backend = backend
        self.humanize = humanize
        self.move_duration = move_duration
        self.pre_click_delay = pre_click_delay
        # 多窗口线程共用一只鼠标，整段「移动+点击」必须串行，否则会互相打断
        self._lock = threading.Lock()

    def click(self, x: int, y: int) -> bool:
        """在绝对屏幕坐标 (x, y) 点击一次。"""
        x, y = int(x), int(y)
        with self._lock:
            if self.humanize and self.backend == "pyautogui":
                duration = random.uniform(*self.move_duration)
                try:
                    pyautogui.moveTo(x, y, duration=duration)
                except Exception:
                    pyautogui.moveTo(x, y)
                time.sleep(random.uniform(*self.pre_click_delay))

            if self.backend == "pydirectinput":
                if pydirectinput is None:
                    raise ImportError("缺少依赖 pydirectinput，请先安装 requirements.txt")
                pydirectinput.click(x, y)
                return True

            pyautogui.click(x, y)
            return True

    @staticmethod
    def position() -> tuple[int, int]:
        pos = pyautogui.position()
        return int(pos.x), int(pos.y)
