"""配置模型与读写：settings.yaml(全局)、monitors.yaml(监控窗口)、targets.yaml(按钮目标)。

这三个文件用户可以直接手改（也常改错），所以读入时一律做类型与范围校正：
坏值退回默认并回报一条中文说明，绝不让它跑到扫描线程或对话框里才炸。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict, field
from typing import Any, Callable

import yaml

CONFIG_DIR = "config"
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.yaml")
MONITORS_FILE = os.path.join(CONFIG_DIR, "monitors.yaml")
TARGETS_FILE = os.path.join(CONFIG_DIR, "targets.yaml")
# 界面自己的状态（窗口大小/位置）。单独一个文件：settings.yaml 是用户手调的运行参数，
# 而窗口大小是程序写的，混在一起会在界面的参数区里凭空多出一堆看不懂的键。
UI_STATE_FILE = os.path.join(CONFIG_DIR, "ui.yaml")

Reporter = Callable[[str], None]
# OCR 后端白名单，与 core/ocr.py 的 PROVIDER_CHOICES 保持一致
OCR_PROVIDER_CHOICES = ("auto", "cpu", "directml")


class ConfigError(Exception):
    """配置文件损坏到没法读/写，必须用户改好后重试。"""


@dataclass
class Monitor:
    """一个被监控的直播窗口/区域。region=None 表示整个虚拟屏幕。

    scan_interval_sec / ocr_max_side 留空则用全局值：小窗口可以把 ocr_max_side
    调大换取识别精度，多窗口时可以调大间隔换取整体吞吐。
    window_title 非空则改为「按标题跟随窗口」：每轮扫描现取窗口矩形，
    位置、大小变了都不用重新框选，此时 region 被忽略。
    """
    name: str
    region: list[int] | None = None
    enabled: bool = True
    scan_interval_sec: float | None = None
    ocr_max_side: int | None = None
    window_title: str = ""
    # 与 window_title 并列的另外两种身份：直播窗口的标题常常是房间号（一场一变），
    # 类名和进程名才是稳定的。三项填几项就要求同时满足几项。
    window_class: str = ""     # Win32 窗口类名子串，如 Chrome_WidgetWin_1
    window_process: str = ""   # 进程 exe 名，如 Weixin.exe
    crop_bottom: int = 0       # 从窗口矩形底部裁掉的像素，用来切掉弹幕区
    # 右侧裁切：微信视频号直播间的聊天列表在右边一列，弹幕/群名里也常出现「签到」「奖券」，
    # 不裁掉就会整列被判成命中点过去；裁掉还能少认一半文本框、OCR 直接快一半。
    crop_right: int = 0
    # 左侧同理（微信左侧是会话列表，群名里什么词都有）。默认 0 = 不裁：
    # 裁切是按窗口边缘的固定像素，窗口一改大小就偏，所以只在布局固定时用。
    crop_left: int = 0
    background_capture: bool = False  # 后台抓窗(PrintWindow)：窗口被遮挡也能抓、不抢焦点；实验性，对 GPU 渲染内容可能抓到黑屏而自动回退整屏

    @property
    def tracked(self) -> bool:
        return bool(self.window_title.strip() or self.window_class.strip()
                    or self.window_process.strip())

    @property
    def region_tuple(self) -> tuple[int, int, int, int] | None:
        if not self.region:
            return None
        return int(self.region[0]), int(self.region[1]), int(self.region[2]), int(self.region[3])

    def interval(self, settings: "Settings") -> float:
        return self.scan_interval_sec if self.scan_interval_sec else settings.scan_interval_sec

    def max_side(self, settings: "Settings") -> int:
        return int(self.ocr_max_side or settings.ocr_max_side)

    def signature(self) -> tuple:
        """用于判断配置是否变化，决定要不要重启该窗口的扫描线程。"""
        return (self.name, tuple(self.region or []), self.enabled,
                self.scan_interval_sec, self.ocr_max_side,
                self.window_title.strip(), self.window_class.strip(),
                self.window_process.strip(), int(self.crop_bottom),
                int(self.crop_right), int(self.crop_left), self.background_capture)


@dataclass
class Target:
    """一个待点击按钮的定义。

    template 与 ocr_keywords 至少有一个；两者都填则双通道命中。
    monitors 为空表示对所有监控窗口生效，否则只在名列出的窗口里查找。
    """
    name: str
    template: str = ""
    ocr_keywords: list[str] = field(default_factory=list)
    click_offset: list[int] = field(default_factory=lambda: [0, 0])
    cooldown_sec: float = 30.0
    enabled: bool = True
    match_threshold: float | None = None
    monitors: list[str] = field(default_factory=list)

    def has_detector(self) -> bool:
        return bool(self.template) or bool(self.ocr_keywords)

    def applies_to(self, monitor_name: str) -> bool:
        return not self.monitors or monitor_name in self.monitors


@dataclass
class Settings:
    """全局运行参数（每个监控窗口共享）。"""
    scan_interval_sec: float = 1.5
    jitter_sec: float = 0.6
    match_threshold: float = 0.85
    ocr_min_score: float = 0.5
    ocr_max_side: int = 1600      # OCR 前把帧最长边压到该值，4K 整屏必需
    ocr_min_side: int = 960      # 小区域放大到该值再识别，否则整段文字会漏检
    ocr_concurrency: int = 1      # 同时跑 OCR 的窗口数，防止多窗口并发把内存打爆
    ocr_threads: int = 3          # 一次 OCR 能用几个线程；不限就是按核数开满、整机卡死
    ocr_scan_every: int = 1       # 隔几轮跑一次 OCR；2 就省一半 OCR，代价是多等一个间隔
    # 连续这么多轮什么都没扫到时，OCR 自动降到每 ocr_idle_every 轮跑一次，一有命中
    # 立刻恢复每轮跑。直播间绝大多数时间没有弹窗（实测 641 帧里只 3 张），这段是
    # 纯浪费的 CPU；降频最坏只让弹窗晚一个间隔被点到，弹窗会挂到被点掉为止，不会漏。
    ocr_idle_every: int = 2
    # 同一片区域刚点过就安静一会儿再点：一张弹窗里常同时写着「立即领券」和「奖券」，
    # 两个目标各有一份冷却，会被连着点两下。0 = 关掉这个防重复。
    click_guard_sec: float = 2.5
    ocr_provider: str = "auto"   # OCR 推理后端：auto=有 GPU 就用，cpu=只用 CPU，directml=强制 GPU（起不来自动退回 CPU）
    use_pydirectinput: bool = False
    # 命中按钮但它被别的窗口盖着时，先把监控窗口调到前台再点：后台抓窗「看得见」，
    # 而点击只发给最顶层的窗口，不切换就永远停在看得见点不中。只在自己选中的
    # 直播窗口上生效，且只在马上要点的那个瞬间动焦点。
    raise_before_click: bool = True
    # 直播窗口整个或部分停在屏幕外（显示器被拔/远程桌面改过尺寸）时，先把窗口挪进
    # 可见区域再点：点到屏幕外的坐标会被 Windows 夹到屏幕边缘，点的是边缘上那个窗口。
    # 只做最小平移，点完立刻还原原位置。
    bring_window_on_screen: bool = True
    humanize_click: bool = True
    hotkey_toggle: str = "f1"
    auto_start: bool = True       # 打开软件后自动开始扫描
    highlight_sec: float = 0.6
    scales: list[float] = field(default_factory=lambda: [1.0])


# 数值参数允许的区间。超出就夹回来：手改配置的人多半只是想填个大致值，
# 但 0.0 的扫描间隔或 999 的并发会把引擎直接拖死。
_SETTINGS_BOUNDS: dict[str, tuple[float, float]] = {
    "scan_interval_sec": (0.2, 60.0),
    "jitter_sec": (0.0, 30.0),
    "match_threshold": (0.3, 1.0),
    "ocr_min_score": (0.0, 1.0),
    "ocr_max_side": (320, 8192),
    "ocr_min_side": (0, 4096),
    "ocr_concurrency": (1, 16),
    "ocr_threads": (1, max(1, os.cpu_count() or 8)),
    "ocr_scan_every": (1, 10),
    "ocr_idle_every": (1, 10),
    "click_guard_sec": (0.0, 60.0),
    "highlight_sec": (0.1, 10.0),
}
_INT_SETTINGS = frozenset({"ocr_max_side", "ocr_min_side", "ocr_concurrency", "ocr_threads",
                           "ocr_scan_every", "ocr_idle_every", "crop_bottom"})
# 窗口级可选项的取值范围，与全局参数分开列，免得把 crop_bottom 混进 Settings
_MONITOR_BOUNDS: dict[str, tuple[float, float]] = {"crop_bottom": (0, 8192),
                                   "crop_right": (0, 8192), "crop_left": (0, 8192)}


def _resolve_base_dir() -> str:
    """兼容 PyInstaller 冻结环境：配置文件放在 exe 同级目录，便于用户编辑。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _resolve_base_dir()


def abs_path(path: str) -> str:
    """把配置里的相对路径转成绝对路径。"""
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path.replace("/", os.sep))


def _path(name: str) -> str:
    return os.path.join(BASE_DIR, name)


# ---------- 校正工具 ----------
def _warn(report: Reporter | None, message: str) -> None:
    if report is not None:
        report(message)


def _as_bool(raw: Any, default: bool = False) -> bool:
    """yaml 里写 "false" 字符串时 bool() 会得到 True，这里按字面意思认。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "是", "y"}
    if raw is None:
        return default
    return bool(raw)


def _as_number(raw: Any, lo: float, hi: float, as_int: bool = False) -> float | int:
    value = min(hi, max(lo, float(raw)))
    return int(round(value)) if as_int else value


def _as_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(item).strip() for item in items if str(item).strip()]


def _as_region(raw: Any, name: str, report: Reporter | None) -> list[int] | None:
    """区域必须是 4 个整数 (left, top, width, height)，否则当作没设置。

    留着坏值的后果不是崩就是点错位置：坐标一路传到点击调用，
    所以宁可退回全屏并明确说一句。
    """
    if raw is None:
        return None
    try:
        region = [int(round(float(v))) for v in raw]
    except (TypeError, ValueError):
        _warn(report, f"「{name}」的区域 {raw!r} 不是数字，已改为按整屏扫描。")
        return None
    if len(region) != 4:
        _warn(report, f"「{name}」的区域应为 [left, top, width, height] 四项，"
                      f"实际 {len(region)} 项，已改为按整屏扫描。")
        return None
    if region[2] <= 0 or region[3] <= 0:
        _warn(report, f"「{name}」的区域宽高必须为正数，实际 {region[2]}x{region[3]}，"
                      f"已改为按整屏扫描。")
        return None
    return region


def _opt_number(raw: Any, key: str, label: str, report: Reporter | None) -> float | int | None:
    """窗口级可选项：留空/0 表示沿用全局值，填了就必须落在合法区间。"""
    if raw is None or raw == "" or raw == 0:
        return None
    lo, hi = {**_SETTINGS_BOUNDS, **_MONITOR_BOUNDS}[key]
    try:
        return _as_number(raw, lo, hi, as_int=key in _INT_SETTINGS)
    except (TypeError, ValueError):
        _warn(report, f"{label} 的 {key}={raw!r} 不是数字，改用默认值。")
        return None


# ---------- 加载 ----------
def load_settings(report: Reporter | None = None) -> Settings:
    data = _read_yaml(_path(SETTINGS_FILE))
    if not isinstance(data, dict) or not data:
        if data:
            _warn(report, "settings.yaml 顶层应是「键: 值」列表，已忽略并改用默认参数。")
        return Settings()
    return _build_settings(data, report)


def _build_settings(data: dict, report: Reporter | None) -> Settings:
    settings = Settings()
    unknown = [k for k in data if k not in Settings.__dataclass_fields__ and k != "region"]
    if unknown:
        # 少一个下划线、写错一个字母就会让整条配置失效，必须说出来
        _warn(report, f"settings.yaml 里这些键不认识（可能拼错，已忽略）：{', '.join(sorted(unknown))}")
    for key in Settings.__dataclass_fields__:
        if key not in data:
            continue
        raw = data[key]
        default = getattr(settings, key)
        try:
            if key == "scales":
                value = _as_scales(raw)
            elif key == "hotkey_toggle":
                value = str(raw).strip() or default
            elif key == "ocr_provider":
                # 顺手写法（GPU/dml）都认；白名单之外的写法要退回默认并说一句，
                # 否则「以为在用显卡、其实一直在 CPU 上跑」这种静默失效最难查。
                value = str(raw).strip().lower()
                value = {"gpu": "directml", "dml": "directml"}.get(value, value)
                if value not in OCR_PROVIDER_CHOICES:
                    _warn(report, f"settings.yaml 的 ocr_provider={raw!r} 不认识，"
                                  f"只支持 {'/'.join(OCR_PROVIDER_CHOICES)}，已用默认 {default!r}。")
                    value = default
            elif key in _SETTINGS_BOUNDS:
                value = _as_number(raw, *_SETTINGS_BOUNDS[key], as_int=key in _INT_SETTINGS)
            elif isinstance(default, bool):
                value = _as_bool(raw, bool(default))
            else:
                value = default
        except (TypeError, ValueError):
            _warn(report, f"settings.yaml 的 {key}={raw!r} 不可用，沿用默认值 {default!r}。")
            continue
        if value != default:
            setattr(settings, key, value)
    return settings


def _as_scales(raw: Any) -> list[float]:
    scales = [round(float(s), 3) for s in (raw or [])]
    scales = [s for s in scales if 0.2 <= s <= 5.0]
    return scales or [1.0]


def load_monitors(report: Reporter | None = None) -> list[Monitor]:
    """读监控窗口清单，并保证名称唯一。

    名称是扫描线程、统计表和目标归属的键，重名会让后一个窗口静默覆盖
    前一个（于是「加了两个窗口却只扫一个」）。
    若还没有 monitors.yaml，则沿用旧版 settings.yaml 里的单一 region，
    避免升级配置结构后出现「一个窗口都不扫」的静默失效。
    """
    data = _read_yaml(_path(MONITORS_FILE))
    items = data.get("monitors") if isinstance(data, dict) else data
    monitors = _build_monitors(items, report)
    if monitors:
        return monitors
    legacy = None
    if os.path.exists(_path(SETTINGS_FILE)):
        old = _read_yaml(_path(SETTINGS_FILE))
        legacy = old.get("region") if isinstance(old, dict) else None
    return [Monitor(name="主屏幕", region=_as_region(legacy, "主屏幕", report))]


def _build_monitors(items: Any, report: Reporter | None) -> list[Monitor]:
    out: list[Monitor] = []
    for item in items or []:
        if not isinstance(item, dict):
            _warn(report, f"monitors.yaml 里有一项不是「键: 值」结构，已跳过：{item!r}")
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            _warn(report, "monitors.yaml 里有一项缺 name，已跳过（名称用于绑定按钮目标）。")
            continue
        out.append(Monitor(
            name=name,
            region=_as_region(item.get("region"), name, report),
            enabled=_as_bool(item.get("enabled"), True),
            scan_interval_sec=_opt_number(item.get("scan_interval_sec"),
                                          "scan_interval_sec", f"窗口「{name}」", report),
            ocr_max_side=_opt_number(item.get("ocr_max_side"), "ocr_max_side",
                                     f"窗口「{name}」", report),
            window_title=str(item.get("window_title") or "").strip(),
            window_class=str(item.get("window_class") or "").strip(),
            window_process=str(item.get("window_process") or "").strip(),
            crop_bottom=int(_opt_number(item.get("crop_bottom"), "crop_bottom",
                                        f"窗口「{name}」", report) or 0),
            crop_right=int(_opt_number(item.get("crop_right"), "crop_right",
                                       f"窗口「{name}」", report) or 0),
            crop_left=int(_opt_number(item.get("crop_left"), "crop_left",
                                      f"窗口「{name}」", report) or 0),
            background_capture=_as_bool(item.get("background_capture"), False),
        ))
    return _dedupe_names(out, report)


def _dedupe_names(monitors: list[Monitor], report: Reporter | None) -> list[Monitor]:
    taken: list[str] = []
    for monitor in monitors:
        if monitor.name in taken:
            new_name = unique_name(monitor.name, taken)
            _warn(report, f"监控窗口名称重复：「{monitor.name}」已改名为「{new_name}」，"
                          f"引用旧名的按钮目标需要重新指定生效窗口。")
            monitor.name = new_name
        taken.append(monitor.name)
    return monitors


def load_targets(report: Reporter | None = None) -> list[Target]:
    data = _read_yaml(_path(TARGETS_FILE))
    items = data.get("targets") if isinstance(data, dict) else data
    return _build_targets(items, report)


def _build_targets(items: Any, report: Reporter | None) -> list[Target]:
    out: list[Target] = []
    for item in items or []:
        if not isinstance(item, dict):
            _warn(report, f"targets.yaml 里有一项不是「键: 值」结构，已跳过：{item!r}")
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            _warn(report, "targets.yaml 里有一项缺 name，已跳过。")
            continue
        target = Target(
            name=name,
            template=str(item.get("template") or "").strip(),
            ocr_keywords=_as_str_list(item.get("ocr_keywords")),
            click_offset=_as_offset(item.get("click_offset"), name, report),
            cooldown_sec=_as_cooldown(item.get("cooldown_sec"), name, report),
            enabled=_as_bool(item.get("enabled"), True),
            match_threshold=_as_threshold(item.get("match_threshold"), name, report),
            monitors=_as_str_list(item.get("monitors")),
        )
        if not target.has_detector():
            _warn(report, f"目标「{name}」既没有模板也没有 OCR 关键词，扫描时会忽略它。")
        out.append(target)
    return out


def _as_offset(raw: Any, name: str, report: Reporter | None) -> list[int]:
    if raw is None:
        return [0, 0]
    try:
        values = [int(round(float(v))) for v in raw]
    except (TypeError, ValueError):
        _warn(report, f"目标「{name}」的 click_offset={raw!r} 不是数字对，改用 0, 0。")
        return [0, 0]
    if len(values) != 2:
        _warn(report, f"目标「{name}」的 click_offset 应为 [dx, dy] 两项，"
                      f"实际 {len(values)} 项，改用 0, 0。")
        return [0, 0]
    return values


def _as_cooldown(raw: Any, name: str, report: Reporter | None) -> float:
    if raw is None or raw == "":
        return 30.0
    try:
        return float(_as_number(raw, 0.0, 86400.0))
    except (TypeError, ValueError):
        _warn(report, f"目标「{name}」的 cooldown_sec={raw!r} 不是数字，改用 30 秒。")
        return 30.0


def _as_threshold(raw: Any, name: str, report: Reporter | None) -> float | None:
    if raw is None or raw == "" or raw == 0:
        return None
    try:
        return float(_as_number(raw, 0.3, 1.0))
    except (TypeError, ValueError):
        _warn(report, f"目标「{name}」的 match_threshold={raw!r} 不是数字，改用全局阈值。")
        return None


# ---------- 保存 ----------
def save_settings(settings: Settings, report: Reporter | None = None) -> None:
    _write_yaml(_path(SETTINGS_FILE), asdict(settings), report)


def save_monitors(monitors: list[Monitor], report: Reporter | None = None) -> None:
    _write_yaml(_path(MONITORS_FILE), {"monitors": [asdict(m) for m in monitors]}, report)


def save_targets(targets: list[Target], report: Reporter | None = None) -> None:
    _write_yaml(_path(TARGETS_FILE), {"targets": [asdict(t) for t in targets]}, report)


def config_files() -> dict[str, str]:
    """返回三个配置文件的「逻辑名 -> 绝对路径」，供界面监听外部改动用。

    selftest.py --pick --write 等会直接改这些文件，运行中的程序据此热加载。
    冻结成 exe 后，_path 会指向 exe 旁边的 config，所以 exe 用户改自己那份也会自动生效。
    """
    return {"settings": _path(SETTINGS_FILE),
            "monitors": _path(MONITORS_FILE),
            "targets": _path(TARGETS_FILE)}


def changed_config_files(files: dict, prev: dict) -> tuple:
    """对比各配置文件的当前 mtime 与 prev，返回 (发生变化的键, 新的 mtime 表)。

    只把“现在比记录的更新”的键算作改动；文件不存在时跳过（既不算改动也不报错）。
    只做探测、不解析 yaml，因此可在离线测试里用临时目录直接验证。
    """
    now = dict(prev)
    changed: list[str] = []
    for key, path in files.items():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if prev.get(key) != mtime:
            now[key] = mtime
            changed.append(key)
    return changed, now


def unique_name(desired: str, taken: list[str]) -> str:
    """保证监控窗口名称不重复（名称是目标归属与冷却键的一部分）。"""
    base = (desired or "").strip() or "窗口"
    name = base
    idx = 2
    while name in taken:
        name = f"{base}{idx}"
        idx += 1
    return name


# ---------- 读写原语 ----------
def _read_yaml(path: str) -> Any:
    """读 YAML。语法错误要变成一句人话，而不是把 traceback 甩给用户。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{os.path.basename(path)} 格式有误，请修正后重启：\n{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取 {os.path.basename(path)}：{exc}") from exc


def _write_yaml(path: str, payload: Any, report: Reporter | None = None) -> None:
    """先写同名 .tmp 再原子替换：中途崩溃或磁盘满不会把原配置写成半截。"""
    basename = os.path.basename(path)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        message = f"无法保存 {basename}：{exc}"
        _warn(report, message)
        raise ConfigError(message) from exc

# ---------- 界面状态（窗口大小/位置） ----------
def load_ui_state(report: Reporter | None = None) -> dict:
    """读界面状态。文件不存在或内容坏了都退回空 dict：窗口大小记不住不该拦住启动。"""
    try:
        data = _read_yaml(_path(UI_STATE_FILE))
    except ConfigError:
        return {}
    return data if isinstance(data, dict) else {}


def save_ui_state(state: dict, report: Reporter | None = None) -> None:
    _write_yaml(_path(UI_STATE_FILE), state, report)
