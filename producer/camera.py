import cv2
import redis

r = redis.Redis(host="redis", port=6379, decode_responses=False)

STREAM = "frames"
RTSP = "rtsp://192.168.0.196:8080/h264.sdp"

cap = cv2.VideoCapture(RTSP)

while True:
    ok, frame = cap.read()
    if not ok:
        continue

    _, buf = cv2.imencode(".jpg", frame)

    r.xadd(
        STREAM,
        {"frame": buf.tobytes()},
        maxlen=1000
    )