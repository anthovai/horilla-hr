# face-verify models

All weights run on CPU through [tract](https://github.com/sonos/tract)
(pure Rust ONNX runtime). Licence texts, copied from the upstream
repositories at the pinned commits, are in `LICENSES/` and ship inside the
Docker image next to the models.

| File | Role | Licence | In git? |
| --- | --- | --- | --- |
| `yunet_2023mar.onnx` | face detector + 5 landmarks | MIT | no: `fetch.sh` |
| `sface_2021dec.onnx` | 128-d face embedding | Apache-2.0 | no: `fetch.sh` (39 MB) |
| `minifasnet_v2_2.7_80x80.onnx` | passive liveness (2.7x crop) | Apache-2.0 | yes (converted by us) |
| `minifasnet_v1se_4.0_80x80.onnx` | passive liveness (4.0x crop) | Apache-2.0 | yes (converted by us) |

Run `./fetch.sh` once after cloning (CI and the Dockerfile do). It downloads
the two upstream files and refuses anything whose SHA-256 differs.

## YuNet face detector

- File: `face_detection_yunet_2023mar.onnx` from OpenCV Zoo, commit
  `48b10fa505aeb51fbd127fc60a5c660040e823b7`
- URL: https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
- SHA-256: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`
- Licence: MIT, Copyright (c) 2020 Shiqi Yu
  ([LICENSE](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE)),
  copy: `LICENSES/opencv-zoo-yunet-MIT.txt`. Commercial use permitted.
- Training data: WIDER FACE (per libfacedetection.train). The MIT licence
  covers the weights; WIDER FACE itself is a research dataset (see "Residual
  risk" below).
- Input 1x3x640x640 BGR 0..255; we downscale to fit and pad right/bottom, as
  OpenCV's `FaceDetectorYN` pads. Post-processing mirrors OpenCV
  `face_detect.cpp` (anchor-free grid decode, score = sqrt(cls*obj), NMS 0.3).
- Published accuracy: WIDER FACE val AP 0.884 / 0.866 / 0.750
  (easy/medium/hard), OpenCV Zoo README.

## SFace recogniser

- File: `face_recognition_sface_2021dec.onnx` from OpenCV Zoo, same commit
- URL: https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
- SHA-256: `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`
- Licence: Apache-2.0
  ([LICENSE](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE)),
  copy: `LICENSES/opencv-zoo-sface-Apache-2.0.txt`. Commercial use permitted
  by the licence. Contributed by Yaoyao Zhong (paper:
  https://arxiv.org/abs/2205.12010); ONNX conversion by Chengrui Wang.
- Input: 112x112 crop aligned by a 5-point similarity transform to the
  ArcFace template (as OpenCV `FaceRecognizerSF::alignCrop`), RGB 0..255.
  Output 128 floats; we L2-normalise.
- Model id in the API: `sface_2021dec@1` (`@1` = our pipeline version).
- **Match threshold (FACE_MATCH_THRESHOLD, default 0.363)**: the cosine
  threshold published with the model in OpenCV Zoo
  [`sface.py`](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/sface.py)
  (`_threshold_cosine = 0.363`) and the OpenCV tutorial
  [DNN-based Face Detection And Recognition](https://docs.opencv.org/4.x/d0/dd4/tutorial_dnn_face.html);
  it is the LFW operating point, where the zoo reports 99.40 % accuracy.
  LFW is web photos of celebrities, so treat 0.363 as a starting point and
  re-tune on OrvaHR's own attendance captures (raise it for fewer false
  accepts).

## MiniFASNet passive liveness (Silent-Face-Anti-Spoofing)

- Source: https://github.com/minivision-ai/Silent-Face-Anti-Spoofing, commit
  `b6d5f04ad78778917853b25c778acef6d5626d15`, weights
  `resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.pth`
  (SHA-256 `a5eb02e1843f19b5386b953cc4c9f011c3f985d0ee2bb9819eea9a142099bec0`) and
  `resources/anti_spoof_models/4_0_0_80x80_MiniFASNetV1SE.pth`
  (SHA-256 `84ee1d37d96894d5e82de5a57df044ef80a58be2b218b5ed7cdfd875ec2f5990`).
- Licence: Apache-2.0 for the whole repository, weights included (they are
  committed in the repo; no separate weights licence exists). Copy:
  `LICENSES/Silent-Face-Anti-Spoofing-Apache-2.0.txt`. Trained by Minivision
  (a company) on its own data; commercial use permitted.
- Converted to ONNX by `../tools/convert_minifasnet.py` (torch 2.x,
  `weights_only=True`, opset 13; ONNX Runtime parity check max abs diff
  ~2e-6). Committed results:
  - `minifasnet_v2_2.7_80x80.onnx` SHA-256 `1cb86b65b7e204950a10a868049e1012c0892f6f908dc93587f29cf3b3794419`
  - `minifasnet_v1se_4.0_80x80.onnx` SHA-256 `0a4539e34fd8428967614df33657edbc8ffc0404049104a22dc7eb1589f378a9`
  (re-exporting with another torch version may change the bytes, not the
  numbers.)
- Input: 80x80 BGR 0..255 crops of the face box grown 2.7x and 4.0x
  (upstream `CropImage`, shifted to stay inside the frame), INTER_LINEAR
  resize. Output 3 logits; class 1 = real. `live_score` = mean softmax
  P(real) over both models.
- **FACE_LIVENESS_THRESHOLD, default 0.5.** Upstream decides by argmax of
  the summed probabilities, so 0.5 is at least as strict. Upstream reports
  TPR 97.8 % at FPR 1e-5 for its APK model on its own test set; we have not
  measured it on OrvaHR captures. Training used phone-camera images at
  3:4; webcams, IR cameras or heavy compression may shift scores. Passive
  RGB liveness stops casual print/screen replays, not masks or injected
  video streams.

## Other gates (heuristics, not from a paper)

- FACE_MIN_FACE_PX = 80: shorter side of the face box (working image is at
  most 1280 px on its longest side). SFace upsamples anything smaller than
  its 112 px crop.
- FACE_BLUR_MIN = 60: variance of the 4-neighbour Laplacian on the central
  80x80 of the aligned 112x112 crop. Sharp portraits score 600-2400 here; a
  Gaussian blur of sigma 4 drops a portrait well below 60. Calibrate on real
  devices.
- `quality` (0..1, informational) = detector score x min(1, sharpness /
  (4 x FACE_BLUR_MIN)) x min(1, face px / (2 x FACE_MIN_FACE_PX)).

## Residual licence risk (read before launch)

The weights' licences (MIT, Apache-2.0) permit commercial use, and we use no
InsightFace/buffalo/MS1M-licensed weights. However, OpenCV Zoo does not state
which dataset the published SFace model was trained on. The SFace authors'
code trains on CASIA-WebFace, VGGFace2 and MS1MV2 (all distributed for
research use), and the upstream code repository (github.com/zhongyy/SFace)
carries no licence file. Whether a dataset's research-only terms reach a
model trained on it is legally unsettled; OpenCV publishes this model under
Apache-2.0 and it ships in commercial products, but have counsel confirm
before relying on it at scale. YuNet's training set (WIDER FACE) carries the
same, smaller, question. Swapping the recogniser means re-enrolling everyone
(new model id), which the API's `model_mismatch` already handles.
