/*
 * Face check-in camera widget (orvahr_face/_camera.html).
 *
 * Captures frames with getUserMedia, downsizes them to JPEG in a canvas and
 * posts them as base64 JSON to Django, which calls the face service. Frames
 * are kept only in local variables and dropped after the request; nothing is
 * written to storage.
 */
(function () {
    "use strict";

    var T = window.ORVAHR_FACE_STRINGS || {};
    var MAX_WIDTH = 480;
    var JPEG_QUALITY = 0.85;
    var STEP_MS = 900;

    function fmt(text, values) {
        return String(text || "").replace(/%\((\w+)\)s/g, function (_m, key) {
            return values[key] !== undefined ? values[key] : "";
        });
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function init(root) {
        var video = root.querySelector(".orvahr-face__video");
        var guide = root.querySelector(".orvahr-face__guide");
        var button = root.querySelector(".orvahr-face__start");
        var list = root.querySelector(".orvahr-face__frames");
        var csrf = root.querySelector("[name=csrfmiddlewaretoken]");
        var mode = root.dataset.mode;
        var wanted = Math.max(1, Math.min(5, parseInt(root.dataset.frames || "1", 10)));
        var stream = null;
        var busy = false;

        function say(text, cls) {
            guide.textContent = text || "";
            guide.className = "orvahr-face__guide text-sm mt-2" + (cls ? " " + cls : "");
        }

        function stop() {
            if (stream) {
                stream.getTracks().forEach(function (track) { track.stop(); });
                stream = null;
            }
        }

        function start() {
            if (stream) { return Promise.resolve(); }
            if (!window.isSecureContext) { return Promise.reject(new Error(T.insecure)); }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                return Promise.reject(new Error(T.no_camera));
            }
            say(T.starting);
            return navigator.mediaDevices.getUserMedia({
                video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
                audio: false
            }).then(function (s) {
                stream = s;
                video.srcObject = s;
                return video.play().catch(function () {});
            }).catch(function () {
                throw new Error(T.no_camera);
            });
        }

        function grab() {
            var w = video.videoWidth || 640;
            var h = video.videoHeight || 480;
            var scale = Math.min(1, MAX_WIDTH / w);
            var canvas = document.createElement("canvas");
            canvas.width = Math.round(w * scale);
            canvas.height = Math.round(h * scale);
            canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
            var data = canvas.toDataURL("image/jpeg", JPEG_QUALITY);
            canvas.width = canvas.height = 0;
            return data;
        }

        function location() {
            if (root.dataset.geo !== "1" || !navigator.geolocation) { return Promise.resolve(null); }
            return new Promise(function (resolve) {
                navigator.geolocation.getCurrentPosition(
                    function (pos) { resolve(pos.coords); },
                    function () { resolve(null); },
                    { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
                );
            });
        }

        function post(payload, coords) {
            var url = root.dataset.postUrl;
            if (coords) {
                url += (url.indexOf("?") < 0 ? "?" : "&") +
                    "latitude=" + encodeURIComponent(coords.latitude) +
                    "&longitude=" + encodeURIComponent(coords.longitude);
            }
            return fetch(url, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrf ? csrf.value : "",
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify(payload)
            }).then(function (response) {
                return response.json().catch(function () {
                    return { ok: false, message: T.network };
                });
            });
        }

        function showFrames(frames) {
            list.innerHTML = "";
            (frames || []).forEach(function (frame) {
                var li = document.createElement("li");
                li.className = frame.accepted ? "ok" : "bad";
                li.textContent = (T.frame || "") + " " + (frame.index + 1) + ": " +
                    (frame.accepted ? (T.accepted || "OK") : (frame.message || frame.reason));
                list.appendChild(li);
            });
        }

        var guides = [T.guide_center, T.guide_hold, T.guide_turn_left, T.guide_turn_right, T.guide_center];

        async function run() {
            if (busy) { return; }
            busy = true;
            button.disabled = true;
            list.innerHTML = "";
            try {
                await start();
                var frames = [];
                var coordsPromise = location();
                for (var i = 0; i < wanted; i++) {
                    say(mode === "enrol" ? guides[i % guides.length] : T.guide_center);
                    await sleep(i === 0 ? STEP_MS * 1.5 : STEP_MS);
                    frames.push(grab());
                    if (mode === "enrol") {
                        say(fmt(T.captured, { n: i + 1, total: wanted }));
                    }
                }
                say(T.sending);
                var payload;
                if (mode === "enrol") {
                    payload = { images: frames };
                    if (root.dataset.employeeId) { payload.employee_id = root.dataset.employeeId; }
                } else {
                    payload = { image: frames[0], direction: root.dataset.direction };
                }
                frames = null;
                var coords = await coordsPromise;
                var result = await post(payload, coords);
                payload = null;
                showFrames(result.frames);
                if (result.ok) {
                    say(result.message, "success");
                    stop();
                    if (root.dataset.doneUrl) {
                        setTimeout(function () { window.location.href = root.dataset.doneUrl; }, 2000);
                    }
                    return;
                }
                say(result.message || T.network, "error");
                if (result.code === "state_changed" || result.code === "rate_limited") { stop(); return; }
            } catch (err) {
                say((err && err.message) || T.network, "error");
                stop();
            }
            button.textContent = T.retry;
            button.disabled = false;
            busy = false;
        }

        button.addEventListener("click", run);
        window.addEventListener("pagehide", stop);
    }

    document.querySelectorAll(".orvahr-face").forEach(init);
})();
