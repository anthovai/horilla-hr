# face-verify service API (contract between services/face-verify and orvahr_face)

Stateless. Stores nothing (no images, no templates, no logs of request bodies).
Internal only: listens on FACE_BIND (default 127.0.0.1:8092), every request
must carry `X-Service-Token: <FACE_SERVICE_TOKEN>` (constant-time compare);
otherwise 401. Request bodies limited (e.g. 4 MB). JSON in/out.

Images are base64 JPEG or PNG, one person expected in frame.

## POST /v1/embed
Request:  {"images": ["<b64>", ...]}              (1..5 frames from enrolment)
Response: {"template": [float, ...],               (L2-normalised embedding, averaged over accepted frames)
           "dims": 128,
           "model": "<recognition model id>@<version>",
           "frames": [{"faces": 1, "quality": 0.0-1.0, "live_score": 0.0-1.0, "accepted": true,
                       "reason": null | "no_face" | "multiple_faces" | "too_small" | "blurry" | "not_live"}]}
422 when no frame is accepted (body carries the per-frame reasons).

## POST /v1/verify
Request:  {"image": "<b64>", "templates": [[float, ...], ...], "model": "<id>@<version>"}
Response: {"match": bool, "score": float (best cosine similarity), "threshold": float,
           "live": bool, "live_score": float, "faces": int,
           "reason": null | "no_face" | "multiple_faces" | "not_live" | "model_mismatch" | "too_small" | "blurry"}
`match` is true only when exactly one face, live, and score >= threshold.

## GET /healthz -> {"status":"ok","models":{"detector":..,"recognizer":..,"liveness":..}}
