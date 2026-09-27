"""设计令牌与 ttk 主题：颜色、字号、间距只在这里定义一次。

界面代码引用样式名（Header.TLabel、Primary.TButton…）而不是字面量，改配色不用翻遍所有文件。
Windows 自带的 vista 主题会忽略 Treeview、按钮的大半配色选项，所以整体切到 clam 再重绘。
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable, Mapping

# 主色只有蓝色一种，其余是语义色与中性色阶。文字色按白底 4.5:1 以上挑。
BG = "#F4F6FA"
SURFACE = "#FFFFFF"
BORDER = "#DCE0E8"
BORDER_STRONG = "#BFC7D4"     # 按钮描边、滑杆轨道：白底上也要看得见
TEXT = "#1B2330"
TEXT_MUTED = "#4F5866"
TEXT_FAINT = "#6F7885"
ACCENT = "#2563EB"
ACCENT_DARK = "#1B4ACB"
ACCENT_SOFT = "#EAF0FE"
ROW_ALT = "#F7F9FC"
ROW_SELECT = "#D8E5FB"
OK = "#0E7A5A"
WARN = "#A15C07"
DANGER = "#BE3455"


@dataclass(frozen=True)
class Palette:
    bg: str = BG
    surface: str = SURFACE
    border: str = BORDER
    border_strong: str = BORDER_STRONG
    text: str = TEXT
    muted: str = TEXT_MUTED
    faint: str = TEXT_FAINT
    accent: str = ACCENT
    accent_dark: str = ACCENT_DARK
    accent_soft: str = ACCENT_SOFT
    row_alt: str = ROW_ALT
    row_select: str = ROW_SELECT
    ok: str = OK
    warn: str = WARN
    danger: str = DANGER


@dataclass(frozen=True)
class Theme:
    palette: Palette
    fonts: Mapping[str, tkfont.Font]
    px: Callable[[int], int]


def apply_theme(root: tk.Misc, px: Callable[[int], int]) -> Theme:
    """套用主题，返回设计令牌。root 是根窗口；样式表本身是整个解释器共享的。"""
    style = ttk.Style(root)
    style.theme_use("clam")
    family = tkfont.nametofont("TkDefaultFont").actual("family")
    fonts = {
        "title": tkfont.Font(family=family, size=13, weight="bold"),
        "heading": tkfont.Font(family=family, size=10, weight="bold"),
        "hint": tkfont.Font(family=family, size=8),
        "mono": tkfont.Font(family="Consolas", size=9),
    }
    p = Palette()

    for widget in ("Toplevel", "TFrame", "TLabel", "TCheckbutton", "TLabelframe",
                   "TLabelframe.Label", "TSeparator", "TMenubutton"):
        style.configure(widget, background=p.bg, foreground=p.text)

    # 顶部应用栏：白底 + 下边框，和灰色画布分开
    style.configure("Header.TFrame", background=p.surface)
    style.configure("Header.TLabel", background=p.surface, foreground=p.text)
    style.configure("Header.Title.TLabel", background=p.surface, foreground=p.text,
                    font=fonts["title"])
    style.configure("Header.Subtitle.TLabel", background=p.surface, foreground=p.faint,
                    font=fonts["hint"])
    style.configure("Header.TCheckbutton", background=p.surface)
    style.configure("Header.TSeparator", background=p.border)

    style.configure("Heading.TLabel", font=fonts["heading"])
    style.configure("Hint.TLabel", foreground=p.faint, font=fonts["hint"])
    style.configure("Value.TLabel", foreground=p.muted, font=fonts["mono"], anchor="e")

    for name, color in (("StatusRun", p.ok), ("StatusIdle", p.muted), ("StatusWarn", p.danger)):
        style.configure(f"{name}.TLabel", background=color, foreground=p.surface,
                        font=fonts["heading"], padding=(px(10), px(5)))

    _buttons(style, p, px)
    _fields(style, p, px, fonts)
    _tables(style, p, px, fonts)
    return Theme(p, fonts, px)


def _buttons(style: ttk.Style, p: Palette, px: Callable[[int], int]) -> None:
    pad = {"padx": px(12), "pady": px(6)}
    # relief 必须是 solid：flat 时 clam 只按 lightcolor/darkcolor 画立体边，白底上等于没边框
    style.configure("TButton", background=p.surface, foreground=p.text,
                    bordercolor=p.border_strong, lightcolor=p.surface, darkcolor=p.surface,
                    relief="solid", borderwidth=1, focuscolor=p.bg, padding=pad)
    style.map("TButton", background=[("disabled", p.bg), ("active", p.accent_soft)],
              foreground=[("disabled", p.faint)], bordercolor=[("active", p.accent)])

    for name, bg, fg in (("Primary", p.accent, p.surface), ("Stop", p.danger, p.surface)):
        style.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=bg,
                        lightcolor=bg, darkcolor=bg, focuscolor=bg)
        style.map(f"{name}.TButton",
                  background=[("active", p.accent_dark if name == "Primary" else p.danger)])

    # clam 的勾选指示器是张固定的「方框里画叉」位图，-indicatorsize 只会把叉一起放大，
    # 怎么看都像「取消」而不是「选中」。所以复选框改用内置 Toolbutton 布局——它没有指示器，
    # 整条文字就是开关本身。命名字如 "X.TCheckbutton" 仍会继承指示器布局，必须直接改 Toolbutton。
    style.configure("Toolbutton", background=p.surface, foreground=p.text,
                    bordercolor=p.border_strong, lightcolor=p.surface, darkcolor=p.surface,
                    relief="solid", borderwidth=1, focuscolor=p.bg,
                    padding={"padx": px(10), "pady": px(5)})
    style.map("Toolbutton",
              background=[("selected", p.accent_soft), ("active", p.row_alt)],
              foreground=[("selected", p.accent_dark)],
              bordercolor=[("selected", p.accent)],
              lightcolor=[("selected", p.accent_soft), ("active", p.row_alt)],
              darkcolor=[("selected", p.accent_soft), ("active", p.row_alt)])


def _fields(style: ttk.Style, p: Palette, px: Callable[[int], int],
            fonts: Mapping[str, tkfont.Font]) -> None:
    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=p.surface, background=p.surface,
                        foreground=p.text, bordercolor=p.border, lightcolor=p.surface,
                        darkcolor=p.surface, arrowcolor=p.muted, relief="flat", borderwidth=1,
                        insertcolor=p.text, padding=(px(6), px(4)))
        style.map(widget, bordercolor=[("focus", p.accent)], lightcolor=[("focus", p.surface)],
                  darkcolor=[("focus", p.surface)])

    style.configure("TCheckbutton", focuscolor=p.bg, indicatorcolor=p.surface,
                    indicatorsize=px(16), bordercolor=p.border_strong, padding=(px(2), px(4)))
    style.map("TCheckbutton", indicatorcolor=[("selected", p.accent)],
              background=[("active", p.bg)])

    # clam 默认的滑块只有几个像素，高 DPI 下几乎看不见，得显式给厚度
    style.configure("TScale", background=p.bg, troughcolor=p.border_strong, borderwidth=0,
                    focuscolor=p.bg)
    style.configure("Horizontal.TScale", sliderthickness=px(16), sliderlength=px(30))
    style.configure("Horizontal.TScale.slider", background=p.surface, bordercolor=p.accent,
                    lightcolor=p.surface, darkcolor=p.surface, relief="flat", borderwidth=2)
    style.map("Horizontal.TScale.slider", background=[("active", p.accent_soft)])

    style.configure("Vertical.TScrollbar", background="#C7CDD8", troughcolor=p.bg,
                    borderwidth=0, lightcolor=p.bg, darkcolor=p.bg, arrowsize=px(12))
    style.map("Vertical.TScrollbar", background=[("active", p.faint)])
    style.configure("TScrollbar", borderwidth=0, relief="flat")


def _tables(style: ttk.Style, p: Palette, px: Callable[[int], int],
            fonts: Mapping[str, tkfont.Font]) -> None:
    linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    style.configure("Treeview", background=p.surface, fieldbackground=p.surface,
                    foreground=p.text, borderwidth=0, relief="flat", rowheight=linespace + px(8),
                    padding=(px(6), px(3)))
    style.map("Treeview", background=[("selected", p.row_select)],
              foreground=[("selected", p.text)])
    style.configure("Treeview.Heading", background=p.bg, foreground=p.faint,
                    font=fonts["hint"], borderwidth=0, relief="flat",
                    padding=(px(8), px(5)))
    style.map("Treeview.Heading", background=[("active", p.bg)], foreground=[("active", p.faint)])
    style.configure("Treeview.Item", padding=(px(6), px(3)))

    style.configure("Card.TLabelframe", background=p.bg, bordercolor=p.border,
                    lightcolor=p.bg, darkcolor=p.bg, relief="solid", borderwidth=1,
                    padding=px(10))
    style.configure("Card.TLabelframe.Label", background=p.bg, foreground=p.text,
                    font=fonts["heading"])
