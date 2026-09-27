"""全局热键：优先用 pynput 注册系统级热键，缺失时由 UI 退回窗口内绑定。"""
from __future__ import annotations

from typing import Callable

try:
    from pynput import keyboard
except Exception:  # pragma: no cover
    keyboard = None

_MULTI_KEY = {"ctrl", "alt", "shift", "cmd", "f1", "f2", "f3", "f4", "f5",
              "f6", "f7", "f8", "f9", "f10", "f11", "f12", "enter", "space",
              "esc", "home", "end", "page_up", "page_down"}


def _normalize(key: str) -> str:
    """把 'f1' 这类键名转成 pynput 需要的 '<f1>' 语法。"""
    key = key.strip().lower()
    if key.startswith("<") and key.endswith(">"):
        return key
    if len(key) == 1:
        return key
    return f"<{key}>" if key in _MULTI_KEY else key


class HotkeyManager:
    def __init__(self) -> None:
        self._listener = None

    @property
    def available(self) -> bool:
        return keyboard is not None

    def register(self, bindings: dict[str, Callable[[], None]]) -> bool:
        """bindings 形如 {"f1": fn}。返回 False 表示 pynput 不可用。"""
        if not self.available:
            return False
        mapping = {_normalize(key): (lambda fn=fn: fn()) for key, fn in bindings.items()}
        listener = keyboard.GlobalHotKeys(mapping)
        listener.daemon = True
        listener.start()
        self._listener = listener
        return True

    def unregister(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
