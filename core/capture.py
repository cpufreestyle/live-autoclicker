"""屏幕捕获模块：基于 mss 的高速截屏与区域工具。"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

import cv2
import numpy as np

try:
    import mss
except Exception:  # pragma: no cover - 依赖缺失时给出友好错误
    mss = None


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_GA_ROOT = 2
_SW_RESTORE = 9
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
# 只改层次、不动位置大小，也不抢激活：抢焦点交给 SetForegroundWindow
_SWP_NOZCHANGE = 0x0001 | 0x0002 | 0x0010
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

if hasattr(ctypes, "windll"):
    # HWND 是 64 位指针，不设 restype 会被截断成 32 位，句柄比较就会出错
    ctypes.windll.user32.WindowFromPoint.restype = wintypes.HWND
    ctypes.windll.user32.WindowFromPoint.argtypes = [_POINT]
    ctypes.windll.user32.GetAncestor.restype = wintypes.HWND
    ctypes.windll.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    ctypes.windll.user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    ctypes.windll.user32.IsWindowVisible.argtypes = [wintypes.HWND]
    ctypes.windll.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    ctypes.windll.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    ctypes.windll.user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_wchar),
                                                    ctypes.c_int]
    ctypes.windll.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    ctypes.windll.user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_wchar), wintypes.INT]
    ctypes.windll.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    ctypes.windll.kernel32.OpenProcess.restype = wintypes.HANDLE
    ctypes.windll.kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_wchar), ctypes.POINTER(wintypes.DWORD)]
    ctypes.windll.dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                           ctypes.c_void_p, wintypes.DWORD]
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
    ctypes.windll.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    ctypes.windll.user32.SetForegroundWindow.restype = wintypes.BOOL
    ctypes.windll.user32.IsIconic.argtypes = [wintypes.HWND]
    ctypes.windll.user32.ShowWindow.argtypes = [wintypes.HWND, wintypes.INT]
    ctypes.windll.user32.BringWindowToTop.argtypes = [wintypes.HWND]
    ctypes.windll.user32.SetActiveWindow.argtypes = [wintypes.HWND]
    ctypes.windll.user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    ctypes.windll.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.INT,
                                                wintypes.INT, wintypes.INT, wintypes.INT, wintypes.UINT]
    ctypes.windll.user32.SetWindowPos.restype = wintypes.BOOL
    ctypes.windll.kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def root_hwnd(hwnd: int) -> int:
    """把任意子窗口句柄归一到它所在的顶层窗口句柄；失败返回 0。"""
    if not hwnd or not hasattr(ctypes, "windll"):
        return 0
    try:
        return int(ctypes.windll.user32.GetAncestor(wintypes.HWND(int(hwnd)), _GA_ROOT)) or 0
    except Exception:
        return 0


def window_at(x: int, y: int) -> int:
    """返回屏幕点 (x, y) 上最顶层窗口的顶层句柄；查不到返回 0。"""
    if not hasattr(ctypes, "windll"):
        return 0
    try:
        hwnd = ctypes.windll.user32.WindowFromPoint(_POINT(int(x), int(y)))
    except Exception:
        return 0
    return root_hwnd(int(hwnd or 0))



def window_identity_at(x: int, y: int) -> tuple[str, str, str]:
    """屏幕点 (x, y) 上最顶层窗口的 (标题, 类名, 进程名)；问不出来返回空串三元组。

    命中却被挡住时，日志要说清「被谁盖着」才排得动——只说「别的窗口」等于没说，
    用户还得自己去猜是不是本程序自己的界面压在直播窗上。
    """
    if not hasattr(ctypes, "windll"):
        return "", "", ""
    try:
        top = ctypes.windll.user32.WindowFromPoint(_POINT(int(x), int(y)))
    except Exception:
        return "", "", ""
    if not top:
        return "", "", ""
    try:
        return window_signature(int(top))
    except Exception:
        return "", "", ""


def window_owns_point(hwnd: int, x: int, y: int) -> bool:
    """屏幕点 (x, y) 现在是不是归 hwnd 这个窗口（含它的子窗口）所有。

    PrintWindow 后台抓窗能拍到被遮住的窗口，但点击是发给最顶层那个窗口的。
    不查这一下就会「看见按钮却点到别人身上」——尤其后台抓窗让这种窗口也能被检测出来之后，
    这个坑比以前更容易踩到。拿不到句柄时返回 True，等于不检查，
    保持固定区域扫描的老行为。
    """
    if not hwnd:
        return True
    top = window_at(x, y)
    if not top:
        return True          # 问不出来就别拦，交给已有的 SelfWindowGuard
    return top == root_hwnd(int(hwnd))


def point_owned_by_session(hwnd: int, x: int, y: int) -> bool:
    """屏幕点 (x, y) 现在能不能被 hwnd 这个直播窗口“够到”。

    三种情况都算够到：
    1) 点归 hwnd 自己（含子窗口）——正常情况；
    2) 点归一个独立的顶层窗口，而它几乎整个嵌在 hwnd 的矩形里——直播间的
       「兑换券 / 红包」弹窗正是这种：单独一个顶层窗盖在直播画面上，点击就该
       发给它。真机日志“命中兑换券但被别的窗口盖着，已跳过”连刷 13 次，根因就是
       老判断只认第 1 种；
    3) 问不出所属窗口——不拦，交给上层别的兑底。

    其余（点被完全无关的窗口占着）返回 False，调用方按「先提窗再点 / 安全跳过」处理。
    """
    if not hwnd:
        return True
    top = window_at(x, y)
    if not top:
        return True
    if root_hwnd(int(top)) == root_hwnd(int(hwnd)):
        return True
    owner_rect = _window_rect(int(top))
    tracked_rect = _window_rect(int(hwnd))
    if owner_rect is None or tracked_rect is None:
        return False
    ix = (min(owner_rect[0] + owner_rect[2], tracked_rect[0] + tracked_rect[2])
          - max(owner_rect[0], tracked_rect[0]))
    iy = (min(owner_rect[1] + owner_rect[3], tracked_rect[1] + tracked_rect[3])
          - max(owner_rect[1], tracked_rect[1]))
    if ix <= 0 or iy <= 0:
        return False
    area = owner_rect[2] * owner_rect[3]
    return area > 0 and ix * iy >= 0.8 * area


def is_minimized(hwnd: int) -> bool:
    """窗口是不是缩在任务栏里（最小化）。"""
    if not hwnd or not hasattr(ctypes, "windll"):
        return False
    try:
        return bool(ctypes.windll.user32.IsIconic(wintypes.HWND(int(hwnd))))
    except Exception:
        return False


def bring_to_front(hwnd: int) -> bool:
    """把指定窗口临时顶到最前面，返回它现在能不能真收到点击。

    后台抓窗（PrintWindow）连被遮住、被最小化的窗口都拍得到，而点击永远只发给
    最顶层那个窗口。所以「按钮明明看见了却点不中」的唯一出路，就是在点之前把
    直播窗口真正顶到前面来。

    两步都得做，少一步都上不来：
    1) 最小化/隐藏的先 ShowWindow(SW_RESTORE) 还原；
    2) SetWindowPos(HWND_TOPMOST) 顶到最前——只调 BringWindowToTop 或
       SetWindowPos(HWND_TOP) 时，Windows 经常把窗口留在原处，尤其压不住
       「正在前台的那个程序」的界面（比如本程序自己的窗口）。topmost 借来的
       层次必须用 drop_topmost() 还回去，别让用户的直播窗一直飘在最上面。

    SetForegroundWindow 受前台锁限制，跨进程抢焦点要先挂到当前前台窗口的输入
    线程，否则它会静默失败。抢不抢得到焦点其实不影响点击能不能送达，所以这里
    不把它当回事；真正的验收标准是「这个窗口自己中心那一点现在归不归它所有」。
    """
    if not hwnd or not hasattr(ctypes, "windll"):
        return False
    user32 = ctypes.windll.user32
    try:
        target = wintypes.HWND(int(hwnd))
        if user32.IsIconic(target) or not user32.IsWindowVisible(target):
            user32.ShowWindow(target, _SW_RESTORE)
        user32.SetWindowPos(target, wintypes.HWND(_HWND_TOPMOST), 0, 0, 0, 0, _SWP_NOZCHANGE)
        foreground = user32.GetForegroundWindow()
        if not (foreground and int(foreground) == int(hwnd)):
            other_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
            this_thread = int(ctypes.windll.kernel32.GetCurrentThreadId())
            if other_thread and other_thread != this_thread:
                # 借当前前台窗口的输入线程把焦点抢过来，用完立刻还回去
                user32.AttachThreadInput(this_thread, other_thread, True)
                try:
                    user32.SetForegroundWindow(target)
                finally:
                    user32.AttachThreadInput(this_thread, other_thread, False)
            else:
                user32.SetForegroundWindow(target)
        box = _visible_rect(int(hwnd))
        if not box:
            return False
        return window_owns_point(int(hwnd), box[0] + box[2] // 2, box[1] + box[3] // 2)
    except Exception:
        return False


def drop_topmost(hwnd: int) -> None:
    """撤掉 bring_to_front 借来的置顶，把窗口还给用户原来的层次。

    只动层次不动位置和大小，也不会把窗口弹到前面来；失败就静默忽略——
    最坏结果只是窗口多飘一会儿，比点不到按钮轻得多。
    """
    if not hwnd or not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.user32.SetWindowPos(wintypes.HWND(int(hwnd)),
                                          wintypes.HWND(_HWND_NOTOPMOST), 0, 0, 0, 0,
                                          _SWP_NOZCHANGE)
    except Exception:
        pass


def virtual_screen() -> tuple[int, int, int, int]:
    """虚拟桌面的 (left, top, right, bottom)——所有显示器拼起来的那块。

    点坐标落在这块外面时，Windows 会把光标夹到屏幕边缘上，点下去点的是边缘
    那个窗口，越点越错。多显示器被拔掉、远程桌面改过桌面尺寸（ToDesk/VNC/TeamViewer）
    之后，窗口常常还留在屏幕外的老坐标上，所以点之前必须先问这一句。
    问不出来时返回 (0, 0, 0, 0)，调用方按「不拦住」处理，保持老行为。
    """
    if not hasattr(ctypes, "windll"):
        return (0, 0, 0, 0)
    try:
        left = int(ctypes.windll.user32.GetSystemMetrics(76))    # SM_XVIRTUALSCREEN
        top = int(ctypes.windll.user32.GetSystemMetrics(77))     # SM_YVIRTUALSCREEN
        width = int(ctypes.windll.user32.GetSystemMetrics(78))   # SM_CXVIRTUALSCREEN
        height = int(ctypes.windll.user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
    except Exception:
        return (0, 0, 0, 0)
    if width <= 0 or height <= 0:
        return (0, 0, 0, 0)
    return (left, top, left + width, top + height)


def point_on_screen(x: int, y: int) -> bool:
    """屏幕点 (x, y) 在不在可见的虚拟桌面内。"""
    left, top, right, bottom = virtual_screen()
    if right <= left or bottom <= top:
        return True
    return left <= int(x) < right and top <= int(y) < bottom


def window_on_screen(hwnd: int) -> bool:
    """窗口矩形是否整个落在可见的虚拟桌面内（拿不到矩形时按在屏内处理）。"""
    if not hwnd or not hasattr(ctypes, "windll"):
        return True
    box = _window_rect(int(hwnd))
    if box is None:
        return True
    left, top, right, bottom = virtual_screen()
    if right <= left or bottom <= top:
        return True
    return (box[0] >= left and box[1] >= top
            and box[0] + box[2] <= right and box[1] + box[3] <= bottom)


def move_window_into_view(hwnd: int) -> tuple[int, int] | None:
    """把窗口挪进可见的虚拟桌面，返回挪之前的 (left, top) 供还原；没挪则 None。

    尽量少动：只求「整个窗口都在屏内」的最小平移，位置和大小都不改，
    也不抢焦点、不改层次。窗口比屏幕还大时贴左上角，能露多少露多少。
    """
    if not hwnd or not hasattr(ctypes, "windll"):
        return None
    box = _window_rect(int(hwnd))
    if box is None:
        return None
    left, top, width, height = box
    vs_left, vs_top, vs_right, vs_bottom = virtual_screen()
    if vs_right <= vs_left or vs_bottom <= vs_top:
        return None
    new_left = min(max(left, vs_left), max(vs_left, vs_right - width))
    new_top = min(max(top, vs_top), max(vs_top, vs_bottom - height))
    if (new_left, new_top) == (left, top):
        return None
    try:
        # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE：只挪位置
        ok = ctypes.windll.user32.SetWindowPos(wintypes.HWND(int(hwnd)), None,
                                               int(new_left), int(new_top), 0, 0, 0x0015)
    except Exception:
        return None
    if not ok:
        return None
    return (left, top)


def restore_window_position(hwnd: int, left: int, top: int) -> None:
    """把 move_window_into_view 挪走的窗口放回原位。失败就静默忽略。"""
    if not hwnd or not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.user32.SetWindowPos(wintypes.HWND(int(hwnd)), None,
                                          int(left), int(top), 0, 0, 0x0015)
    except Exception:
        pass


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """未经 DWM 修正的窗口矩形 (left, top, w, h)，含外邨那圈透明边框。

    SetWindowPos 按这个口径移动和还原：若记的是 DWM 扩展框架（比窗口矩形差几像素边框），
    每还原一次窗口就会外飘几像素，多轮之后直接飘出屏幕。
    """
    try:
        rect = _RECT()
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect))
    except Exception:
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 40 or height < 40:
        return None
    if rect.left <= -32000 or rect.top <= -32000:   # 最小化后停在屏幕外
        return None
    return rect.left, rect.top, width, height


def _window_text(hwnd: int) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(wintypes.HWND(int(hwnd)))
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(int(length) + 1)
    ctypes.windll.user32.GetWindowTextW(wintypes.HWND(int(hwnd)), buf, len(buf))
    return buf.value


def _visible_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口的可见矩形 (left, top, width, height)。

    优先用 DWM 的扩展边框：GetWindowRect 还包含 Win10/11 那圈拖拽用的透明边，
    按它截图会把相邻窗口的像素一起裁进来。
    """
    if not hasattr(ctypes, "windll"):
        return None
    rect = _RECT()
    ok = False
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)), _DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect))
        ok = hr == 0
    except Exception:
        ok = False
    if not ok:
        try:
            ctypes.windll.user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect))
        except Exception:
            return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 40 or height < 40:      # 消息中心、IME 候选框这类隐形小窗口
        return None
    if rect.left <= -32000 or rect.top <= -32000:   # 最小化后停在屏幕外，截到的是黑帧
        return None
    return rect.left, rect.top, width, height


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
    return int(pid.value or 0)


def window_class(hwnd: int) -> str:
    """Win32 窗口类名，如 Qt5152_QWindowIcon / Chrome_WidgetWin_1 / WeChatMainWndForPC。"""
    if not hasattr(ctypes, "windll"):
        return ""
    try:
        buf = ctypes.create_unicode_buffer(257)
        ctypes.windll.user32.GetClassNameW(wintypes.HWND(int(hwnd)), buf, len(buf))
        return buf.value or ""
    except Exception:
        return ""


def window_process(hwnd: int) -> str:
    """窗口所属进程的 exe 文件名，如 Weixin.exe。拿不到时返回空串。"""
    if not hasattr(ctypes, "windll"):
        return ""
    pid = _pid_of(hwnd)
    if not pid:
        return ""
    # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION：够读进程名，又不需要管理员权限
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(len(buf))
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value or "")
    except Exception:
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


def cursor_pos() -> tuple[int, int] | None:
    """当前鼠标位置；拿不到返回 None。"""
    if not hasattr(ctypes, "windll"):
        return None
    point = _POINT()
    try:
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
    except Exception:
        return None
    return int(point.x), int(point.y)


def window_under_cursor() -> tuple[int, int, int, int] | None:
    """鼠标所在位置的顶层窗口矩形；拿不到返回 None。

    给「用鼠标指一下直播窗口就配好」用：标题可能是房间号，但鼠标下的那个窗口
    类名和进程名是错不了的。
    """
    point = cursor_pos()
    if point is None:
        return None
    hwnd = window_at(point[0], point[1])
    if not hwnd:
        return None
    return _visible_rect(hwnd)


def signature_under_cursor() -> tuple[tuple[str, str, str], tuple[int, int, int, int]] | None:
    """鼠标下窗口的 (标题, 类名, 进程) 与矩形。"""
    point = cursor_pos()
    if point is None:
        return None
    hwnd = window_at(point[0], point[1])
    if not hwnd:
        return None
    box = _visible_rect(hwnd)
    if box is None:
        return None
    return window_signature(hwnd), box

def window_signature(hwnd: int) -> tuple[str, str, str]:
    """窗口的三样身份：标题、窗口类名、进程 exe 名。"""
    return _window_text(hwnd), window_class(hwnd), window_process(hwnd)


def _matching_windows(title: str = "", window_class_name: str = "", process: str = "",
                      exclude_pid: int | None = None) -> list[tuple[int, tuple[int, int, int, int]]]:
    """按身份找出所有命中的可见顶层窗口，返回 [(hwnd, 矩形)] 列表。

    三个条件填几项就要求同时满足几项（AND）；本进程自己的窗口永远排除。
    window_rect_by 只取面积最大的那个，WindowLocator 则在此基础上做粘性续接。
    """
    needles = [(title or "").strip().lower(),
               (window_class_name or "").strip().lower(),
               (process or "").strip().lower()]
    if not any(needles) or not hasattr(ctypes, "windll"):
        return []
    skip_pid = os.getpid() if exclude_pid is None else int(exclude_pid)
    user32 = ctypes.windll.user32
    out: list[tuple[int, tuple[int, int, int, int]]] = []

    @_ENUM_PROC
    def _each(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _pid_of(int(hwnd)) == skip_pid:
            return True
        text, klass, proc = window_signature(int(hwnd))
        text, klass, proc = text.lower(), klass.lower(), proc.lower()
        if ((not needles[0] or needles[0] in text)
                and (not needles[1] or needles[1] in klass)
                and (not needles[2] or needles[2] in proc)):
            box = _visible_rect(int(hwnd))
            if box is not None:
                out.append((int(hwnd), box))
        return True

    try:
        user32.EnumWindows(_each, 0)
    except Exception:
        return []
    return out


def window_rect_by(title: str = "", window_class_name: str = "", process: str = "",
                   exclude_pid: int | None = None) -> tuple[int, int, int, int] | None:
    """按标题 / 窗口类名 / 进程名找可见顶层窗口，返回 (left, top, width, height)。

    三个条件填几项就要求同时满足几项（AND）——直播窗口的标题常常是房间号、一场一变，
    所以类名和进程名是标题之外更稳的身份。命中多个时取面积最大的那个；没有命中或
    一项都不填时返回 None。
    """
    found = _matching_windows(title, window_class_name, process, exclude_pid)
    if not found:
        return None
    return max(found, key=lambda hb: hb[1][2] * hb[1][3])[1]


class WindowLocator:
    """粘性窗口定位：同一身份上次认的是哪个窗口，这一轮优先还认它。

    只用「面积最大」会在两个同身份窗口之间来回翻——比如微信主界面和直播小窗都叫
    Weixin.exe，或多开了几个直播间——跟丢一帧矩形就跳变，点下去自然点歪。这里记住
    上次命中的 hwnd：只要它还可见、还匹配就续上，真的不见了才重新挑最大的。
    每个监控线程各持一个实例，天然按窗口隔离，互不影响。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str, str], int] = {}
        self._current_hwnd = 0

    @staticmethod
    def _key(title: str, window_class_name: str, process: str) -> tuple[str, str, str]:
        return ((title or "").strip().lower(),
                (window_class_name or "").strip().lower(),
                (process or "").strip().lower())

    def box(self, monitor) -> tuple[int, int, int, int] | None:
        title = getattr(monitor, "window_title", "")
        klass = getattr(monitor, "window_class", "")
        proc = getattr(monitor, "window_process", "")
        key = self._key(title, klass, proc)
        matches = _matching_windows(title, klass, proc)
        if not matches:
            with self._lock:
                self._last.pop(key, None)
            self._current_hwnd = 0
            return None
        with self._lock:
            preferred = self._last.get(key)
        chosen = next((hb for hb in matches if hb[0] == preferred), None)
        if chosen is None:
            chosen = max(matches, key=lambda hb: hb[1][2] * hb[1][3])
        with self._lock:
            self._last[key] = chosen[0]
        self._current_hwnd = chosen[0]
        return chosen[1]

    def current_hwnd(self) -> int:
        """最近一次 box() 选中的窗口句柄；无有效命中为 0。"""
        return self._current_hwnd

    def forget(self, monitor) -> None:
        key = self._key(getattr(monitor, "window_title", ""),
                        getattr(monitor, "window_class", ""),
                        getattr(monitor, "window_process", ""))
        with self._lock:
            self._last.pop(key, None)



def window_rect_for(monitor) -> tuple[int, int, int, int] | None:
    """按 Monitor 上填的身份找窗口：标题 / 类名 / 进程名，填几项要求同时满足几项。"""
    return window_rect_by(getattr(monitor, "window_title", ""),
                         getattr(monitor, "window_class", ""),
                         getattr(monitor, "window_process", ""))

def window_rect(title_contains: str, exclude_pid: int | None = None) -> tuple[int, int, int, int] | None:
    """按标题子串找可见顶层窗口，返回 (left, top, width, height)；没找到返回 None。

    命中多个时取面积最大的那个——直播窗口总比同名的小弹窗大。
    exclude_pid 默认排除本进程：本程序自己的标题里也带「直播」，跟着自己扫就乱了。
    """
    return window_rect_by(title_contains, exclude_pid=exclude_pid)


def list_windows(with_identity: bool = False) -> list[tuple]:
    """当前可见的顶层窗口 (标题, 矩形)。with_identity 为真时额外返回窗口类名与进程 exe 名。"""
    if not hasattr(ctypes, "windll"):
        return []
    user32 = ctypes.windll.user32
    out: list[tuple] = []

    @_ENUM_PROC
    def _each(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(int(hwnd)).strip()
        box = _visible_rect(int(hwnd)) if title else None
        if box is None:
            return True
        if with_identity:
            klass, proc = window_class(int(hwnd)), window_process(int(hwnd))
            out.append((title, klass, proc, box))
        else:
            out.append((title, box))
        return True

    user32.EnumWindows(_each, 0)
    # 带身份时元组是 (标题, 类名, 进程, 矩形)，矩形永远在最后一项
    return sorted(out, key=lambda item: item[-1][2] * item[-1][3], reverse=True)


def set_process_dpi_awareness() -> None:
    """启用进程级 DPI 感知，避免 Windows 缩放下坐标错位。"""
    if hasattr(ctypes, "windll"):
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


class ScreenCapture:
    """封装 mss，提供整屏/区域截图，返回 BGR numpy 数组。"""

    def __init__(self) -> None:
        if mss is None:
            raise ImportError("缺少依赖 mss，请先 pip install -r requirements.txt")
        self._sct = mss.mss()
        self._monitor = self._sct.monitors[0]  # 虚拟屏幕(所有显示器合集)

    @property
    def full_region(self) -> tuple[int, int, int, int]:
        """返回 (left, top, width, height) 表示整个虚拟屏幕。"""
        m = self._monitor
        return m["left"], m["top"], m["width"], m["height"]

    def grab(self, region: tuple[int, int, int, int] | None = None) -> np.ndarray:
        """截取指定区域，region=(left, top, width, height)；None 表示全屏。

        返回 BGR 顺序的 ndarray，可直接交给 OpenCV 使用。
        """
        if region is None:
            box = {"left": self._monitor["left"], "top": self._monitor["top"],
                   "width": self._monitor["width"], "height": self._monitor["height"]}
        else:
            left, top, width, height = _normalize_region(region)
            box = {"left": left, "top": top, "width": width, "height": height}

        img = self._sct.grab(box)
        frame = np.asarray(img)  # BGRA
        return frame[:, :, :3].copy()  # 转 BGR

    def grab_screen(self) -> np.ndarray:
        return self.grab(None)

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


def _normalize_region(region: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """校验 (left, top, width, height)，宽高非正时抛出可读的错误。

    这里刻意不做「裁到屏幕内」的兜底：截图框一旦被裁小，帧内坐标就和
    Monitor.region 的左上角错位，算出来的点击点会偏，宁可报错让用户重框。
    """
    left, top, width, height = (int(region[0]), int(region[1]),
                                int(region[2]), int(region[3]))
    if width <= 0 or height <= 0:
        raise ValueError(f"截图区域 {tuple(region)} 的宽高必须为正数，"
                         f"请在「监控窗口」里重新框选。")
    return left, top, width, height


def rect_to_region(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
    """两点矩形转成 (left, top, width, height)。"""
    left = min(x1, x2)
    top = min(y1, y2)
    width = abs(x2 - x1)
    height = abs(y2 - y1)
    return left, top, max(width, 1), max(height, 1)


def frame_digest(frame_bgr, size: int = 48):
    """把一帧压成 size×size 灰度缩略图，用于廉价的「这帧和上帧比有没有变」判断。

    直播画面每帧都在动，所以它不是“静止才跳过”的万能开关：调用方只在缩略图几乎
    完全一致时才跳过检测，另有连续跳过多轮上限兜底，绝不会该点的没点到。太小的帧
    或空帧返回 None，调用方据此当作“有变化”。
    """
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return None
    shape = getattr(frame_bgr, "shape", ())
    if len(shape) < 2 or shape[0] < size or shape[1] < size:
        return None
    if frame_bgr.ndim == 3:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame_bgr
    return cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)


def frame_changed(prev, cur, threshold: float = 1.2) -> bool:
    """两张缩略图的平均绝对差 ≥ threshold 判为“有变化”。任一张为 None 也算有变化。"""
    if prev is None or cur is None:
        return True
    return float(cv2.absdiff(prev, cur).mean()) >= threshold


def grab_window(hwnd: int):
    """用 PrintWindow 抓一个窗口自己的像素（不抢焦点、被别的窗口盖住也能抓）。

    返回 (BGR ndarray, (left, top, width, height))；失败、空帧、句柄无效一律 None，
    调用方可回退到整屏 mss 截图。注意：部分 GPU/DirectX 渲染的内容（如视频画面）
    PrintWindow 可能抓到黑屏，所以调用方必须配合 looks_blank() 判定，别一味当成成功。
    参考：ocr-trigger-clicker / pixel-locator 的后台截图做法。
    """
    if not hwnd or not hasattr(ctypes, "windll"):
        return None
    rect = _visible_rect(hwnd)
    if rect is None:
        return None
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return None
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetWindowDC.restype = wintypes.HDC
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HANDLE
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                       wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HANDLE
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class _BMI(ctypes.Structure):
        _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3)]

    win = wintypes.HWND(int(hwnd))
    win_dc = user32.GetWindowDC(win)
    if not win_dc:
        return None
    mem_dc = dib = old = None
    raw = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(win_dc)
        if not mem_dc:
            return None
        info = _BMI()
        ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
        info.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        info.bmiHeader.biWidth = int(width)
        info.bmiHeader.biHeight = -int(height)   # 负 => 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32           # BGRA
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not dib or not bits.value:
            return None
        old = gdi32.SelectObject(mem_dc, dib)
        # 2 = PW_RENDERFULLCONTENT，对 Win8.1+ 被重定向的窗口更稳
        if not user32.PrintWindow(win, mem_dc, 2):
            return None
        raw = ctypes.string_at(bits, int(width) * int(height) * 4)
    finally:
        try:
            if mem_dc:
                if old:
                    gdi32.SelectObject(mem_dc, old)
                gdi32.DeleteDC(mem_dc)
            if dib:
                gdi32.DeleteObject(dib)
        finally:
            if win_dc:
                user32.ReleaseDC(win, win_dc)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((int(height), int(width), 4))
    return arr[:, :, :3].copy(), rect


def looks_blank(frame_bgr, threshold: float = 1.0) -> bool:
    """帧近乎纯色（黑屏/白屏/抓取失败）判 True，供后台截图回退用。"""
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return True
    return float(np.std(frame_bgr)) < threshold
