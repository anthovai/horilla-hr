#!/bin/sh
# Downloads the upstream ONNX models that are too large to keep in git and
# verifies them against pinned SHA-256 sums. Idempotent: a file whose sum
# already matches is left alone. Sources and licences: models/MODELS.md.
# The MiniFASNet liveness models are committed (we converted them; there is
# no upstream ONNX to fetch), see tools/convert_minifasnet.py.
set -eu
cd "$(dirname "$0")"

ZOO=https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models

fetch() {
    name=$1 url=$2 sum=$3
    if [ -f "$name" ] && echo "$sum  $name" | sha256sum -c - >/dev/null 2>&1; then
        return 0
    fi
    curl -fsSL --retry 3 -o "$name.part" "$url"
    if ! echo "$sum  $name.part" | sha256sum -c - >/dev/null 2>&1; then
        echo "checksum mismatch for $name" >&2
        rm -f "$name.part"
        exit 1
    fi
    mv "$name.part" "$name"
    echo "fetched $name"
}

# macOS has shasum, not sha256sum.
if ! command -v sha256sum >/dev/null 2>&1; then
    sha256sum() { shasum -a 256 "$@"; }
fi

fetch yunet_2023mar.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
    8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
fetch sface_2021dec.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
    0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
