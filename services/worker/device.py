"""Выбор устройства для InsightFace / onnxruntime.

FACE_DEVICE=cpu (по умолчанию) — как раньше, только CPU.
FACE_DEVICE=gpu — сначала CUDA, при недоступности onnxruntime сам
откатывается на CPU (воркер не падает). Фактически выбранное устройство
определяется по сессиям модели после загрузки — см. active_device().
"""

CUDA = "CUDAExecutionProvider"
CPU = "CPUExecutionProvider"

VALID_DEVICES = ("cpu", "gpu")


def providers_for(device: str) -> list[str]:
    device = device.strip().lower()
    if device not in VALID_DEVICES:
        raise ValueError(f"FACE_DEVICE должен быть одним из {VALID_DEVICES}, получено: {device!r}")
    return [CUDA, CPU] if device == "gpu" else [CPU]


def active_device(session_providers: list[list[str]]) -> str:
    """'gpu', если хотя бы одна модель реально работает через CUDA."""
    return "gpu" if any(p and p[0] == CUDA for p in session_providers) else "cpu"


def preload_cuda_libs() -> None:
    """Подгружает CUDA/cuDNN из pip-пакетов nvidia-* (onnxruntime-gpu >= 1.21).
    Без видеокарты/драйвера — молча ничего не делает."""
    try:
        import onnxruntime as ort

        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
    except Exception:
        pass
