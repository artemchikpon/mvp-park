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


# По умолчанию потоки onnxruntime после каждого запуска модели не засыпают,
# а какое-то время крутятся в цикле, ожидая следующую задачу (spinning).
# Это экономит микросекунды при непрерывном потоке запусков, но у нас кадр
# приходит раз в ~200 мс — и все потоки пула между кадрами жгут CPU впустую
# (в замерах: ~3 ядра на воркер при ~0.06 ядра полезной работы).
# Результат модели от этой настройки не меняется.
NO_SPINNING = {
    "session.intra_op.allow_spinning": "0",
    "session.inter_op.allow_spinning": "0",
}


def session_options(cpu_threads: int = 0):
    """SessionOptions для моделей InsightFace: без spinning; cpu_threads > 0 —
    ограничить число потоков одной модели (0 — решает onnxruntime)."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    for key, value in NO_SPINNING.items():
        opts.add_session_config_entry(key, value)
    if cpu_threads > 0:
        opts.intra_op_num_threads = cpu_threads
    return opts


def preload_cuda_libs() -> None:
    """Подгружает CUDA/cuDNN из pip-пакетов nvidia-* (onnxruntime-gpu >= 1.21).
    Без видеокарты/драйвера — молча ничего не делает."""
    try:
        import onnxruntime as ort

        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
    except Exception:
        pass
