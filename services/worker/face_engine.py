"""Обёртка над InsightFace. Модель загружается при импорте модуля.

Устройство задаётся FACE_DEVICE (cpu | gpu, см. device.py). Если GPU
запрошен, но недоступен, onnxruntime откатывается на CPU — это видно в
логе строкой "InsightFace device: requested=gpu, active=cpu".
"""

import logging

from insightface.app import FaceAnalysis

import device
from worker_config import FACE_CPU_THREADS, FACE_DEVICE

log = logging.getLogger("worker")

if FACE_DEVICE == "gpu":
    device.preload_cuda_libs()

app = FaceAnalysis(
    name="buffalo_sc",
    providers=device.providers_for(FACE_DEVICE),
    sess_options=device.session_options(FACE_CPU_THREADS),
)

app.prepare(ctx_id=0, det_size=(640, 640))

ACTIVE_DEVICE = device.active_device(
    [m.session.get_providers() for m in app.models.values()]
)
_msg = (
    f"InsightFace device: requested={FACE_DEVICE}, active={ACTIVE_DEVICE}, "
    f"cpu_threads={FACE_CPU_THREADS or 'auto'}, spinning=off"
)
if FACE_DEVICE == "gpu" and ACTIVE_DEVICE != "gpu":
    log.warning(_msg + " — GPU недоступен, модель работает на CPU")
else:
    log.info(_msg)


def extract_faces(frame):
    return app.get(frame)
