from insightface.app import FaceAnalysis

app = FaceAnalysis(
    name="buffalo_sc",
    providers=["CPUExecutionProvider"]
)

app.prepare(ctx_id=0, det_size=(640, 640))


def extract_faces(frame):
    return app.get(frame)