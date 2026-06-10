import redis
import json
import numpy as np
from datetime import datetime
from sklearn.metrics.pairwise import cosine_similarity

r = redis.Redis(host="redis", port=6379, decode_responses=True)

SIMILARITY = 0.65


class FaceStorage:

    def create(self, emb, face):

        pid = r.incr("person_counter")

        data = {
            "id": pid,
            "age": int(face.age) if face.age else -1,
            "gender": face.gender,
            "embeddings": [emb.tolist()],
            "created": datetime.utcnow().isoformat()
        }

        key = f"person:{pid}"

        r.set(key, json.dumps(data))
        r.expire(key, 86400)

        return pid

    def update(self, pid, emb):

        key = f"person:{pid}"
        raw = r.get(key)
        if not raw:
            return

        data = json.loads(raw)

        data["embeddings"].append(emb.tolist())
        data["embeddings"] = data["embeddings"][-10:]

        r.set(key, json.dumps(data))
        r.expire(key, 86400)

    def find(self, emb):

        best_id = None
        best_score = 0

        for key in r.scan_iter("person:*"):

            if key == "person_counter":
                continue

            raw = r.get(key)
            if not raw:
                continue

            person = json.loads(raw)

            for v in person["embeddings"]:

                score = cosine_similarity(
                    [emb],
                    [np.array(v)]
                )[0][0]

                if score > best_score:
                    best_score = score
                    best_id = person["id"]

        return best_id, best_score

    def identify(self, emb, face):

        pid, score = self.find(emb)

        if pid and score >= SIMILARITY:
            self.update(pid, emb)
            return {"id": pid, "new": False, "score": score,
                    "age": int(face.age) if face.age else -1,
                    "gender": face.gender}

        return {
            "id": self.create(emb, face),
            "new": True,
            "score": 1.0,
            "age": int(face.age) if face.age else -1,
            "gender": face.gender
        }