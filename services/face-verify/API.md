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

## Implementation notes (added by the service; the contract above is unchanged)
- Model id today: `sface_2021dec@1`. Treat it as opaque; store it with the
  template and send it back on verify. A different id gives
  `reason: "model_mismatch"`, `match: false`, `faces: 0` (image not analysed).
- `/healthz` also requires the token (the Docker HEALTHCHECK sends it).
- Errors are JSON `{"error": "..."}`: 400 bad JSON/base64/image/template
  (templates must be 128 finite non-zero floats, 1..32 per call, re-normalised
  server-side), 401 token, 413 body over FACE_MAX_BODY_BYTES (default 4 MiB)
  or image over FACE_MAX_PIXELS (default 16 MP), 408 timeout (default 15 s).
- 422 from /v1/embed: `{"error":"no frame accepted","template":null,"dims":128,"model":...,"frames":[...]}`.
- `score` is reported whenever exactly one usable face was found, including
  `not_live` (so callers can audit spoof attempts); it is 0 otherwise.
- `reason` precedence: no_face, multiple_faces, too_small, blurry, not_live.
  `live`/`live_score` are reported whenever exactly one face was found.
- FACE_REQUIRE_LIVENESS=false makes liveness advisory: scores are still
  returned but `not_live` is never set and does not block `match`/`accepted`.
