//! The models and the per-frame pipeline:
//! decode -> EXIF orientation -> size limit -> detect (YuNet) -> gate on face
//! count / size / sharpness -> align (5 points) -> embed (SFace) and score
//! liveness (MiniFASNet x2).

use std::path::Path;
use std::sync::Arc;

use image::RgbImage;
use tract_onnx::prelude::*;

use crate::align::{self, CROP, TEMPLATE};
use crate::detect::{self, Face, Heads, INPUT};
use crate::imaging;

/// Embedding identity returned by /v1/embed and required by /v1/verify.
/// `@1` is the pipeline version: bump it whenever detection, alignment or
/// preprocessing changes in a way that moves embeddings, so stale templates
/// are refused with `model_mismatch` instead of silently scoring lower.
pub const MODEL_ID: &str = "sface_2021dec@1";
pub const DIMS: usize = 128;
pub const DETECTOR_ID: &str = "yunet_2023mar";
pub const LIVENESS_ID: &str = "minifasnet_v2_2.7+v1se_4.0@silent-face-anti-spoofing-b6d5f04";

const DETECTOR_FILE: &str = "yunet_2023mar.onnx";
const RECOGNIZER_FILE: &str = "sface_2021dec.onnx";
/// (file, crop scale around the face box). Both take 80x80 BGR 0..255.
const LIVENESS_FILES: [(&str, f32); 2] = [
    ("minifasnet_v2_2.7_80x80.onnx", 2.7),
    ("minifasnet_v1se_4.0_80x80.onnx", 4.0),
];
const LIVENESS_INPUT: u32 = 80;
const NMS_IOU: f32 = 0.3;
/// Inset of the blur window inside the 112x112 aligned crop.
const BLUR_INSET: u32 = 16;

type Plan = Arc<TypedRunnableModel>;

pub struct Engine {
    detector: Plan,
    recognizer: Plan,
    liveness: Vec<(Plan, f32)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    NoFace,
    MultipleFaces,
    TooSmall,
    Blurry,
    NotLive,
}

impl Reason {
    pub fn as_str(self) -> &'static str {
        match self {
            Reason::NoFace => "no_face",
            Reason::MultipleFaces => "multiple_faces",
            Reason::TooSmall => "too_small",
            Reason::Blurry => "blurry",
            Reason::NotLive => "not_live",
        }
    }
}

/// Gates applied to a frame (a subset of `Config`).
#[derive(Clone, Copy, Debug)]
pub struct Gates {
    pub det_score: f32,
    pub min_face_px: f32,
    pub blur_min: f64,
    pub liveness_threshold: f32,
    pub require_liveness: bool,
}

#[derive(Debug)]
pub struct Analysis {
    pub faces: usize,
    /// First failing gate, in order: face count, size, sharpness, liveness.
    pub reason: Option<Reason>,
    pub live_score: f32,
    pub live: bool,
    pub quality: f32,
    /// Unit-length embedding; present whenever exactly one face passed the
    /// size and sharpness gates (even if it failed liveness).
    pub embedding: Option<Vec<f32>>,
}

fn load(dir: &Path, file: &str, shape: &[usize]) -> Result<Plan, String> {
    let path = dir.join(file);
    let plan = tract_onnx::onnx()
        .model_for_path(&path)
        .and_then(|m| m.with_input_fact(0, f32::fact(shape).into()))
        .and_then(|m| m.into_optimized())
        .and_then(|m| m.into_runnable())
        .map_err(|e| format!("{}: {e:#}", path.display()))?;
    Ok(plan)
}

/// NCHW float tensor from an RGB image; `bgr` picks the channel order.
fn to_tensor(img: &RgbImage, bgr: bool, h: usize, w: usize) -> Tensor {
    let mut a = tract_ndarray::Array4::<f32>::zeros((1, 3, h, w));
    for (x, y, p) in img.enumerate_pixels() {
        let (x, y) = (x as usize, y as usize);
        if x >= w || y >= h {
            continue;
        }
        for c in 0..3 {
            let src = if bgr { 2 - c } else { c };
            a[[0, c, y, x]] = p.0[src] as f32;
        }
    }
    a.into_tensor()
}

/// Silent-Face-Anti-Spoofing `CropImage._get_new_box`: the face box `(x, y,
/// w, h)` grown by `scale` around its centre, shifted (not clipped) to stay
/// inside the image, with the scale reduced if it cannot fit. Returns the
/// inclusive `(x0, y0, x1, y1)`.
pub fn liveness_box(src_w: u32, src_h: u32, bbox: [i32; 4], scale: f32) -> (u32, u32, u32, u32) {
    let (sw, sh) = (src_w as f32, src_h as f32);
    let (x, y) = (bbox[0] as f32, bbox[1] as f32);
    let (bw, bh) = (bbox[2].max(1) as f32, bbox[3].max(1) as f32);
    let scale = scale.min((sh - 1.0) / bh).min((sw - 1.0) / bw);
    let (nw, nh) = (bw * scale, bh * scale);
    let (cx, cy) = (bw / 2.0 + x, bh / 2.0 + y);
    let (mut l, mut t) = (cx - nw / 2.0, cy - nh / 2.0);
    let (mut r, mut b) = (cx + nw / 2.0, cy + nh / 2.0);
    if l < 0.0 {
        r -= l;
        l = 0.0;
    }
    if t < 0.0 {
        b -= t;
        t = 0.0;
    }
    if r > sw - 1.0 {
        l -= r - sw + 1.0;
        r = sw - 1.0;
    }
    if b > sh - 1.0 {
        t -= b - sh + 1.0;
        b = sh - 1.0;
    }
    let clamp = |v: f32, hi: f32| v.max(0.0).min(hi) as u32;
    let (x0, y0) = (clamp(l, sw - 1.0), clamp(t, sh - 1.0));
    let (x1, y1) = (clamp(r, sw - 1.0).max(x0), clamp(b, sh - 1.0).max(y0));
    (x0, y0, x1, y1)
}

fn floats(t: &Tensor) -> TractResult<&[f32]> {
    t.try_as_plain_ram()?.as_slice::<f32>()
}

fn softmax(v: &[f32]) -> Vec<f32> {
    let m = v.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
    let e: Vec<f32> = v.iter().map(|x| (x - m).exp()).collect();
    let s: f32 = e.iter().sum();
    e.into_iter().map(|x| x / s).collect()
}

impl Engine {
    pub fn load(dir: &Path) -> Result<Engine, String> {
        let detector = load(dir, DETECTOR_FILE, &[1, 3, INPUT, INPUT])?;
        let c = CROP as usize;
        let recognizer = load(dir, RECOGNIZER_FILE, &[1, 3, c, c])?;
        let li = LIVENESS_INPUT as usize;
        let liveness = LIVENESS_FILES
            .iter()
            .map(|(f, s)| load(dir, f, &[1, 3, li, li]).map(|p| (p, *s)))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Engine {
            detector,
            recognizer,
            liveness,
        })
    }

    /// Faces in `img` coordinates, best first.
    pub fn detect(&self, img: &RgbImage, score_thr: f32) -> Result<Vec<Face>, String> {
        let (w, h) = img.dimensions();
        // Fit into the fixed 640x640 input: downscale if needed, then pad
        // right/bottom with black (OpenCV pads the same way, to a multiple of 32).
        let k = (INPUT as f32 / w.max(h) as f32).min(1.0);
        let scaled;
        let input = if k < 1.0 {
            let nw = ((w as f32 * k).round() as u32).clamp(1, INPUT as u32);
            let nh = ((h as f32 * k).round() as u32).clamp(1, INPUT as u32);
            scaled = image::imageops::resize(img, nw, nh, image::imageops::FilterType::Triangle);
            &scaled
        } else {
            img
        };
        let t = to_tensor(input, true, INPUT, INPUT);
        let out = self
            .detector
            .run(tvec!(t.into()))
            .map_err(|e| format!("detector: {e}"))?;
        let views: Vec<&[f32]> = out
            .iter()
            .map(|o| floats(o).map_err(|e| format!("detector: {e}")))
            .collect::<Result<_, _>>()?;
        if views.len() != 12 {
            return Err(format!(
                "detector: expected 12 outputs, got {}",
                views.len()
            ));
        }
        let heads = [0, 1, 2].map(|i| Heads {
            cls: views[i],
            obj: views[3 + i],
            bbox: views[6 + i],
            kps: views[9 + i],
        });
        let faces = detect::nms(detect::decode(&heads, INPUT, INPUT, score_thr), NMS_IOU);
        Ok(faces.into_iter().map(|f| f.scaled(1.0 / k)).collect())
    }

    /// Aligned 112x112 crop for `face`.
    pub fn align(&self, img: &RgbImage, face: &Face) -> RgbImage {
        let m = align::similarity(&face.landmarks, &TEMPLATE);
        imaging::warp_affine(img, &m, CROP, CROP)
    }

    /// Unit-length SFace embedding of an aligned crop.
    pub fn embed_aligned(&self, aligned: &RgbImage) -> Result<Vec<f32>, String> {
        let c = CROP as usize;
        // OpenCV: blobFromImage(aligned, 1, 112x112, 0, swapRB=true) -> RGB.
        let t = to_tensor(aligned, false, c, c);
        let out = self
            .recognizer
            .run(tvec!(t.into()))
            .map_err(|e| format!("recognizer: {e}"))?;
        let mut v = floats(&out[0])
            .map_err(|e| format!("recognizer: {e}"))?
            .to_vec();
        if v.len() != DIMS || !align::l2_normalize(&mut v) {
            return Err("recognizer: bad embedding".into());
        }
        Ok(v)
    }

    /// Mean "real" probability over the liveness models, on crops taken
    /// around the face box exactly as Silent-Face-Anti-Spoofing does.
    pub fn liveness(&self, img: &RgbImage, face: &Face) -> Result<f32, String> {
        let bbox = [face.x as i32, face.y as i32, face.w as i32, face.h as i32];
        let mut total = 0.0;
        for (plan, scale) in &self.liveness {
            let rect = liveness_box(img.width(), img.height(), bbox, *scale);
            let crop = imaging::resize_bilinear_cv(img, rect, LIVENESS_INPUT, LIVENESS_INPUT);
            let li = LIVENESS_INPUT as usize;
            let t = to_tensor(&crop, true, li, li);
            let out = plan
                .run(tvec!(t.into()))
                .map_err(|e| format!("liveness: {e}"))?;
            let logits = floats(&out[0]).map_err(|e| format!("liveness: {e}"))?;
            if logits.len() != 3 {
                return Err("liveness: expected 3 classes".into());
            }
            // Class 1 is "real face"; 0 and 2 are the two spoof classes.
            total += softmax(logits)[1];
        }
        Ok(total / self.liveness.len() as f32)
    }

    /// Runs the whole per-frame pipeline on an image from
    /// `imaging::decode_image` (already upright and size-checked).
    pub fn analyse(&self, img: RgbImage, g: &Gates) -> Result<Analysis, String> {
        let (img, _) = imaging::limit_size(img, imaging::WORK_MAX_SIDE);
        let faces = self.detect(&img, g.det_score)?;
        let mut a = Analysis {
            faces: faces.len(),
            reason: None,
            live_score: 0.0,
            live: false,
            quality: 0.0,
            embedding: None,
        };
        let face = match faces.len() {
            0 => {
                a.reason = Some(Reason::NoFace);
                return Ok(a);
            }
            1 => &faces[0],
            _ => {
                a.reason = Some(Reason::MultipleFaces);
                return Ok(a);
            }
        };
        a.live_score = self.liveness(&img, face)?;
        a.live = a.live_score >= g.liveness_threshold;
        let aligned = self.align(&img, face);
        let sharpness = imaging::laplacian_variance(&aligned, BLUR_INSET);
        a.quality = quality(face, sharpness, g);
        if face.size() < g.min_face_px {
            a.reason = Some(Reason::TooSmall);
            return Ok(a);
        }
        if sharpness < g.blur_min {
            a.reason = Some(Reason::Blurry);
            return Ok(a);
        }
        a.embedding = Some(self.embed_aligned(&aligned)?);
        if g.require_liveness && !a.live {
            a.reason = Some(Reason::NotLive);
        }
        Ok(a)
    }
}

/// 0..1 summary for the UI: detector confidence x sharpness x size, where
/// sharpness and size saturate at 4x and 2x their minimums.
fn quality(face: &Face, sharpness: f64, g: &Gates) -> f32 {
    let det = face.score.clamp(0.0, 1.0);
    let sharp = if g.blur_min > 0.0 {
        (sharpness / (4.0 * g.blur_min)).min(1.0) as f32
    } else {
        1.0
    };
    let size = if g.min_face_px > 0.0 {
        (face.size() / (2.0 * g.min_face_px)).min(1.0)
    } else {
        1.0
    };
    (det * sharp * size).clamp(0.0, 1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn liveness_box_grows_around_centre_when_it_fits() {
        // 20x20 box centred at (100, 100) in a 400x400 image, scale 2.
        let r = liveness_box(400, 400, [90, 90, 20, 20], 2.0);
        assert_eq!(r, (80, 80, 120, 120));
    }

    #[test]
    fn liveness_box_shifts_inside_the_image_instead_of_clipping() {
        // Near the left/top edge: shifted right/down, size kept (40x40).
        let r = liveness_box(400, 400, [0, 0, 20, 20], 2.0);
        assert_eq!(r, (0, 0, 40, 40));
        // Near the right/bottom edge.
        let r = liveness_box(400, 400, [385, 385, 14, 14], 2.0);
        assert_eq!(r, (371, 371, 399, 399));
    }

    #[test]
    fn liveness_box_reduces_scale_to_fit() {
        // 200-px box, scale 4 would need 800 px; capped at (h-1)/h.
        let (x0, y0, x1, y1) = liveness_box(480, 640, [140, 200, 200, 200], 4.0);
        assert!(x0 == 0 && x1 == 479);
        assert!(y1 - y0 <= 639);
    }

    #[test]
    fn softmax_sums_to_one() {
        let p = softmax(&[1.0, 2.0, 3.0]);
        assert!((p.iter().sum::<f32>() - 1.0).abs() < 1e-6);
        assert!(p[2] > p[1] && p[1] > p[0]);
    }
}
