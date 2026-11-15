"""
Client for services/face-verify (contract: services/face-verify/API.md).

Server-to-server only: the browser never talks to the service. Settings,
read with getattr so no settings file has to change:

* ``ORVAHR_FACE_SERVICE_URL``   default ``http://face-verify:8092``
* ``ORVAHR_FACE_SERVICE_TOKEN`` sent as ``X-Service-Token``

Contract: no method raises into the caller. Every call returns a
``ServiceResult``; ``ok`` is False with ``error`` set to a short code when the
service is down, slow, misconfigured or answers garbage. Request and response
bodies (images, templates) are never logged.
"""

import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://face-verify:8092"
TIMEOUT = (3.05, 15)  # connect, read

# ServiceResult.error codes
UNAVAILABLE = "unavailable"
TIMEOUT_ERROR = "timeout"
UNAUTHORIZED = "unauthorized"
BAD_RESPONSE = "bad_response"
REJECTED = "rejected"  # 422 from /v1/embed: no frame accepted (data has per-frame reasons)


@dataclass
class ServiceResult:
    ok: bool
    status: int = 0
    data: dict = field(default_factory=dict)
    error: str = ""


class FaceServiceClient:
    def __init__(self, base_url=None, token=None, timeout=TIMEOUT):
        self.base_url = (base_url or getattr(settings, "ORVAHR_FACE_SERVICE_URL", "") or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else (getattr(settings, "ORVAHR_FACE_SERVICE_TOKEN", "") or "")
        self.timeout = timeout

    # Public API ----------------------------------------------------------
    def embed(self, images):
        """POST /v1/embed with 1..5 base64 frames."""
        return self._post("/v1/embed", {"images": list(images)})

    def verify(self, image, templates, model):
        """POST /v1/verify with one base64 frame and the employee's decrypted templates."""
        return self._post("/v1/verify", {"image": image, "templates": list(templates), "model": model})

    def health(self):
        try:
            response = requests.get(self.base_url + "/healthz", headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("face-verify health check failed: %s", type(exc).__name__)
            return ServiceResult(ok=False, error=UNAVAILABLE)
        return self._result(response, "/healthz")

    # Internals -----------------------------------------------------------
    def _headers(self):
        return {"X-Service-Token": self.token, "Content-Type": "application/json"}

    def _post(self, path, payload):
        try:
            response = requests.post(
                self.base_url + path, json=payload, headers=self._headers(), timeout=self.timeout
            )
        except requests.Timeout:
            logger.warning("face-verify %s timed out", path)
            return ServiceResult(ok=False, error=TIMEOUT_ERROR)
        except requests.RequestException as exc:
            logger.warning("face-verify %s unreachable: %s", path, type(exc).__name__)
            return ServiceResult(ok=False, error=UNAVAILABLE)
        except Exception:  # never raise into a view
            logger.exception("face-verify %s call failed", path)
            return ServiceResult(ok=False, error=UNAVAILABLE)
        return self._result(response, path)

    def _result(self, response, path):
        status = getattr(response, "status_code", 0)
        try:
            data = response.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            data = {}
            if 200 <= status < 300:
                logger.warning("face-verify %s returned a non-JSON body (HTTP %s)", path, status)
                return ServiceResult(ok=False, status=status, error=BAD_RESPONSE)
        if 200 <= status < 300:
            return ServiceResult(ok=True, status=status, data=data)
        if status == 401:
            logger.error("face-verify refused the service token (HTTP 401); check ORVAHR_FACE_SERVICE_TOKEN")
            return ServiceResult(ok=False, status=status, error=UNAUTHORIZED)
        if status == 422:
            return ServiceResult(ok=False, status=status, data=data, error=REJECTED)
        logger.warning("face-verify %s answered HTTP %s", path, status)
        return ServiceResult(ok=False, status=status, data=data, error=UNAVAILABLE)


def get_client():
    """Indirection so tests can patch one place."""
    return FaceServiceClient()
