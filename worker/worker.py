import redis
import cv2
import numpy as np
import logging

from face_engine import extract_faces
from storage import FaceStorage
from db.stats_service import update_stats

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

r = redis.Redis(host="redis", port=6379, decode_responses=False)

STREAM = "frames"

storage = FaceStorage()

last_id = "0"


while True:

    messages = r.xread(
        {STREAM: last_id},
        block=5000,
        count=1
    )

    if not messages:
        continue

    _, entries = messages[0]

    for message_id, data in entries:

        last_id = message_id.decode()

        frame = cv2.imdecode(
            np.frombuffer(data[b"frame"], np.uint8),
            cv2.IMREAD_COLOR
        )

        faces = extract_faces(frame)

        new_c = 0
        known_c = 0

        for face in faces:

            if face.embedding is None:
                continue

            res = storage.identify(face.embedding, face)

            update_stats(
                res["age"],
                res["gender"],
                res["new"]
            )

            if res["new"]:
                new_c += 1
            else:
                known_c += 1

        log.info(
            f"Frame | faces={len(faces)} | new={new_c} | known={known_c}"
        )