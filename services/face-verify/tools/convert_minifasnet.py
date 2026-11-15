"""Convert the minivision Silent-Face-Anti-Spoofing MiniFASNet weights to ONNX.

Reproduces models/minifasnet_*.onnx. Run from any directory:

    python -I convert_minifasnet.py <path to Silent-Face-Anti-Spoofing checkout> <out dir>

Needs torch + onnx (+ onnxruntime for the parity check). The checkout must be
commit b6d5f04ad78778917853b25c778acef6d5626d15 (see models/MODELS.md).
Weights are loaded with weights_only=True (no pickle code execution).
The exported graph returns raw logits [1, 3]; class 1 is "real face".
Input: [1, 3, 80, 80] float32, BGR, 0..255, no mean/std (upstream's own
ToTensor deliberately skips the /255, see src/data_io/functional.py).
"""

import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np
import torch

repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(repo))
from src.model_lib.MiniFASNet import MiniFASNetV1SE, MiniFASNetV2  # noqa: E402

MODELS = [
    ("2.7_80x80_MiniFASNetV2.pth", MiniFASNetV2, "minifasnet_v2_2.7_80x80.onnx"),
    ("4_0_0_80x80_MiniFASNetV1SE.pth", MiniFASNetV1SE, "minifasnet_v1se_4.0_80x80.onnx"),
]

for pth, ctor, onnx_name in MODELS:
    model = ctor(conv6_kernel=(5, 5))  # get_kernel(80, 80) == (5, 5)
    state = torch.load(repo / "resources" / "anti_spoof_models" / pth, map_location="cpu", weights_only=True)
    state = OrderedDict((k[7:] if k.startswith("module.") else k, v) for k, v in state.items())
    model.load_state_dict(state)
    model.eval()
    x = torch.rand(1, 3, 80, 80) * 255
    dst = out / onnx_name
    torch.onnx.export(model, x, str(dst), input_names=["input"], output_names=["logits"],
                      opset_version=13, dynamo=False)
    with torch.no_grad():
        ref = model(x).numpy()
    import onnxruntime as ort

    got = ort.InferenceSession(str(dst)).run(None, {"input": x.numpy()})[0]
    assert np.allclose(ref, got, atol=1e-3), (ref, got)
    print(f"{onnx_name}: ok, max abs diff {np.abs(ref - got).max():.2e}")
