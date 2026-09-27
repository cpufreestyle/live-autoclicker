"""OCR 模块：基于 RapidOCR 的离线中文文字识别。"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import cv2
import numpy as np

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # pragma: no cover
    RapidOCR = None

# 4K 整屏直接推理会让 onnxruntime 内存分配失败，且明显变慢，故先降采样
DEFAULT_MAX_SIDE = 1600
# 小区域（单个直播窗口）文字太小会被检测网络整段漏掉，故先放大
DEFAULT_MIN_SIDE = 960
MAX_UPSCALE = 3.0
# 默认值：rapidocr 建会话时不传线程数，onnxruntime 就会按逻辑核数把 intra-op 线程开满。
# 12 核机上实测一帧 1100x1150 的 OCR 平均吃掉 4~6 个核、还要 5.7s；压到 3 线程反而
# 只要 2.0s / 3.2 核。PP-OCR 这几个 mobile 模型太小，线程开多了纯亏。
DEFAULT_THREADS = 3
# OCR 推理后端。auto（默认）= 装了带 DirectML 的 onnxruntime 就走 GPU，否则 CPU；
# cpu = 只用 CPU；directml = 强制走 GPU，起不来时自动退回 CPU。
PROVIDER_CHOICES = ("auto", "cpu", "directml")
DEFAULT_PROVIDER = "auto"
# 顺手把常见的顺手写法都认了：填 GPU / dml 的人本意都是「用显卡跑」
PROVIDER_ALIASES = {"gpu": "directml", "dml": "directml", "directml": "directml",
                    "cpu": "cpu", "auto": "auto", "": "auto"}
# DirectML 的设备号，只有一张卡时无所谓；多卡机器想指定就改这里。
DML_OPTIONS = {"device_id": 0}

_ORIGINAL_OPTIONS = None          # rapidocr 原本的 SessionOptions，只记一次
_capped_threads = DEFAULT_THREADS


def cap_cpu_threads(threads: int) -> str:
    """限制 OCR 与 OpenCV 能用的线程数。返回空串表示限住了，否则是没限住的原因。

    rapidocr 建推理会话时用的是它自己模块里的 `SessionOptions` 这个名字，
    把它换成我们的工厂就能带上参数——库本身没有任何地方能传这个值。
    """
    global _ORIGINAL_OPTIONS, _capped_threads
    n = max(1, int(threads))
    cv2.setNumThreads(n)
    try:
        from rapidocr_onnxruntime import utils as ru
    except ImportError as exc:                      # 没装 OCR，线程数自然无从限制
        return f"未安装 rapidocr-onnxruntime，OCR 线程数未限制：{exc}"
    if _ORIGINAL_OPTIONS is None:
        original = getattr(ru, "SessionOptions", None)
        if not callable(original):
            return "rapidocr 版本里没有 SessionOptions，OCR 线程数未限制"
        _ORIGINAL_OPTIONS = original
    _capped_threads = n
    ru.SessionOptions = _capped_options
    return ""


def available_providers() -> list[str]:
    """当前 onnxruntime 支持哪些执行后端。装的是 CPU 版就只有 CPUExecutionProvider。"""
    try:
        import onnxruntime
    except Exception:
        return []
    try:
        return list(onnxruntime.get_available_providers())
    except Exception:
        return []


def gpu_provider() -> str:
    """能用的 GPU 后端名；没有则空串。"""
    providers = available_providers()
    for name in ("DmlExecutionProvider", "CUDAExecutionProvider"):
        if name in providers:
            return name
    return ""


def normalize_provider(value) -> str:
    """把配置里的写法归一成 auto/cpu/directml；认不出来的当 auto（等于不表态）。"""
    return PROVIDER_ALIASES.get(str(value or "").strip().lower(), DEFAULT_PROVIDER)


# 后端 enum -> 打印/配置里用的短名
_GPU_NAMES = {"DmlExecutionProvider": "directml", "CUDAExecutionProvider": "cuda"}

_SESSION_LOCK = threading.Lock()
_REAL_SESSION = None          # 被替换掉的真 InferenceSession，只在打过补丁时非空


class _GpuInferenceSession:
    """把 GPU 后端塞到 providers 最前面的 InferenceSession 替身。

    rapidocr 建推理会话时只认 CUDA，装了 DirectML 它也不知道用；而 providers 一栏
    永远把 CPU 兜在后面，所以这里替它决定。直接对窗口说「这个点归谁」一样：
    真正干活的 session 由本类持有，外面照常调 run/get_inputs/get_providers。
    """

    def __init__(self, model_path, sess_options=None, providers=None, **kwargs):
        gpu = gpu_provider()
        options = dict(DML_OPTIONS) if gpu == "DmlExecutionProvider" else {"device_id": 0}
        wanted = ([(gpu, options)] if gpu else [])
        # 原来那一串（CPU 兜底）原样留在后面，一次都不少
        wanted += [p for p in (providers or []) if p[0] != gpu]
        try:
            self.session = _REAL_SESSION(model_path, sess_options=sess_options,
                                         providers=wanted, **kwargs)
        except Exception:
            # 驱动太旧/没 GPU：退回原来的 CPU 列表，不能因为想用 GPU 就让 OCR 整个不可用
            self.session = _REAL_SESSION(model_path, sess_options=sess_options,
                                         providers=providers, **kwargs)

    def __getattr__(self, name):
        return getattr(self.session, name)


def apply_provider(provider: str) -> str:
    """按配置切换推理后端，返回实际生效的后端名（cpu / directml / cuda）。

    必须在「建推理会话之前」调用：rapidocr 的 OrtInferSession 用的是它自己
    utils 模块里的 `InferenceSession` 这个名字，替换那个名字就能改到它，
    库本身没给这个入口。没装 rapidocr 时无事可做，返回 cpu。
    """
    want = normalize_provider(provider)
    try:
        from rapidocr_onnxruntime import utils as ru
    except Exception:
        return "cpu"
    gpu = gpu_provider()
    use_gpu = bool(gpu) and (want == "directml" or want == "auto")
    global _REAL_SESSION
    with _SESSION_LOCK:
        if use_gpu:
            if _REAL_SESSION is None:
                _REAL_SESSION = ru.InferenceSession
                ru.InferenceSession = _GpuInferenceSession
            return _GPU_NAMES.get(gpu, gpu.replace("ExecutionProvider", "").lower())
        if _REAL_SESSION is not None:      # 配置改回 cpu：把原来的会话工厂还回去
            ru.InferenceSession = _REAL_SESSION
            _REAL_SESSION = None
        return "cpu"


def session_providers(engine) -> list[str]:
    """建好引擎后实际跑在哪个后端上（CPU 兜底永远在里面，取第一个才算真正在用的）。"""
    session = getattr(getattr(engine, "text_detector", None), "session", None)
    if session is None:
        return []
    try:
        return list(session.get_providers())
    except Exception:
        return []


def _capped_options():
    options = _ORIGINAL_OPTIONS()
    options.intra_op_num_threads = _capped_threads
    options.inter_op_num_threads = 1
    return options


@dataclass
class OcrHit:
    text: str
    score: float
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


class OcrEngine:
    """封装 RapidOCR，懒加载模型（首次调用才占用内存）。

    多窗口并发时同一个实例可复用：模型加载用锁保护，推理由可选信号量限流，
    避免多个窗口同时跑 4K 推理导致内存分配失败。
    """

    def __init__(self, gate: threading.Semaphore | None = None,
                 threads: int = DEFAULT_THREADS,
                 provider: str = DEFAULT_PROVIDER) -> None:
        self._engine = None
        self._load_lock = threading.Lock()
        self._gate = gate
        self._threads = max(1, int(threads))
        self.provider = normalize_provider(provider)
        self.cap_note = ""
        self.gpu_note = ""            # 实际跑在哪个后端，建好会话后回填
        self._available = RapidOCR is not None

    @property
    def available(self) -> bool:
        return self._available

    def warmup(self) -> bool:
        """提前加载模型：否则各窗口线程会在首帧上排队，被误判成「扫描太慢」。"""
        if not self._available:
            return False
        self._ensure_engine()
        return True

    def _ensure_engine(self):
        if not self._available:
            raise ImportError("缺少依赖 rapidocr-onnxruntime，请先安装 requirements.txt")
        if self._engine is None:
            with self._load_lock:
                if self._engine is None:
                    # 必须在建会话之前设好：会话一旦创建就改不了线程数
                    self.cap_note = cap_cpu_threads(self._threads)
                    # 建会话之前把后端换成 GPU（装了就白拿 4 倍速度，没装就还是 CPU）
                    active = apply_provider(self.provider)
                    self.gpu_note = ("" if active == "cpu" else f"OCR 使用 {active} 后端")
                    if self.provider == "directml" and active == "cpu":
                        self.cap_note = self.cap_note or (
                            "ocr_provider: directml 但当前 onnxruntime 不带 GPU 后端，"
                            "已退回 CPU（想用 GPU：pip install onnxruntime-directml）")
                    # 关掉方向分类：直播浮层的「签到」「奖券」永远是正着写的，
                    # 而分类器是每检出一个文字框就跑一次——弹幕带上几十个框纯浪费。
                    self._engine = RapidOCR(use_angle_cls=False)
                    self.gpu_note = (self.gpu_note or "").strip()
                    providers = session_providers(self._engine)
                    if providers and providers[0] != "CPUExecutionProvider":
                        self.gpu_note = ("OCR 使用 "
                                         f"{_GPU_NAMES.get(providers[0], providers[0])} 后端")
        return self._engine

    def recognize(self, frame_bgr: np.ndarray, max_side: int = DEFAULT_MAX_SIDE,
                  min_side: int = DEFAULT_MIN_SIDE) -> list[OcrHit]:
        """识别整帧文字，返回原始命中列表。

        帧的最长边会被调进 [min_side, max_side] 区间：4K 整屏降采样省内存，
        小窗口放大后才能被检测网络看清（否则整段文字直接漏检）。
        坐标最终换算回原帧。
        """
        engine = self._ensure_engine()
        scale = 1.0
        source = frame_bgr
        longest = max(frame_bgr.shape[0], frame_bgr.shape[1])
        target = 0
        if max_side and longest > max_side:
            target = max_side
        elif min_side and longest < min_side:
            target = int(round(longest * min(min_side / longest, MAX_UPSCALE)))
        if target and target != longest:
            scale = target / longest
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            source = cv2.resize(frame_bgr, None, fx=scale, fy=scale, interpolation=interp)

        if self._gate is not None:
            self._gate.acquire()
        try:
            raw, _ = engine(source)
        finally:
            if self._gate is not None:
                self._gate.release()
        hits: list[OcrHit] = []
        for item in raw or []:
            box, text, score = _unpack(item)
            if not text:
                continue
            x, y, w, h = _box_to_rect(box, source.shape)
            if scale != 1.0:
                x = int(round(x / scale))
                y = int(round(y / scale))
                w = int(round(w / scale))
                h = int(round(h / scale))
                x, y, w, h = _clamp_rect(x, y, w, h, frame_bgr.shape)
            hits.append(OcrHit(str(text), float(score), x, y, w, h))
        return hits


def compact_text(text: str) -> str:
    """去掉空白与标点，只留字母数字和汉字。

    OCR 经常把「立即签到」读成「立即 签 到」，关键词也得走同一个函数才能对上，
    所以检测与识别两边共用这一份实现。
    """
    return "".join(ch for ch in str(text) if ch.isalnum())


def _unpack(item):
    """兼容 RapidOCR 不同版本返回结构：[box, text, score] 或 [box, (text, score)]。"""
    box = item[0]
    payload = item[1] if len(item) > 1 else ""
    score = item[2] if len(item) > 2 else 1.0
    if isinstance(payload, (tuple, list)):
        text = payload[0] if payload else ""
        if len(payload) > 1:
            score = payload[1]
    else:
        text = payload
    return box, text, score


def _box_to_rect(box, shape) -> tuple[int, int, int, int]:
    xs = [int(round(float(p[0]))) for p in box]
    ys = [int(round(float(p[1]))) for p in box]
    x, y = max(0, min(xs)), max(0, min(ys))
    w = max(1, max(xs) - x)
    h = max(1, max(ys) - y)
    return _clamp_rect(x, y, w, h, shape)


def _clamp_rect(x: int, y: int, w: int, h: int, shape) -> tuple[int, int, int, int]:
    """把矩形裁进帧内，避免缩放换算后坐标越界。"""
    img_h, img_w = shape[:2]
    x = max(0, min(x, img_w - 1))
    y = max(0, min(y, img_h - 1))
    w = max(1, min(w, img_w - x))
    h = max(1, min(h, img_h - y))
    return x, y, w, h
