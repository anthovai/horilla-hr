//! YuNet (OpenCV Zoo `face_detection_yunet_2023mar`) post-processing.
//!
//! The 2023mar model is anchor-free: every cell of the stride-8/16/32 feature
//! grids is one prior point. For the cell at (row `r`, col `c`) of stride `s`
//! the heads give, exactly as OpenCV's `FaceDetectorYN` decodes them
//! (modules/objdetect/src/face_detect.cpp):
//!
//! * score = sqrt(clamp01(cls) * clamp01(obj))
//! * centre = ((c + dx) * s, (r + dy) * s), size = (exp(dw) * s, exp(dh) * s)
//! * landmark k = ((c + kx_k) * s, (r + ky_k) * s), in the order right eye,
//!   left eye, nose tip, right mouth corner, left mouth corner (subject's
//!   right/left, i.e. image left/right on an unmirrored photo).
//!
//! followed by greedy NMS on IoU.

/// Detector input side (the 2023mar export has a fixed 640x640 input).
pub const INPUT: usize = 640;
pub const STRIDES: [usize; 3] = [8, 16, 32];

#[derive(Clone, Debug, PartialEq)]
pub struct Face {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
    pub landmarks: [[f32; 2]; 5],
    pub score: f32,
}

impl Face {
    pub fn scaled(&self, k: f32) -> Face {
        let mut lm = self.landmarks;
        for p in lm.iter_mut() {
            p[0] *= k;
            p[1] *= k;
        }
        Face {
            x: self.x * k,
            y: self.y * k,
            w: self.w * k,
            h: self.h * k,
            landmarks: lm,
            score: self.score,
        }
    }

    /// Shorter side of the box, in pixels.
    pub fn size(&self) -> f32 {
        self.w.min(self.h)
    }
}

/// Grid (cols, rows) of every stride for an input of `w` x `h`: the prior
/// points, in head output order.
pub fn prior_grids(w: usize, h: usize) -> Vec<(usize, usize, usize)> {
    STRIDES.iter().map(|&s| (s, w / s, h / s)).collect()
}

/// Head outputs for one stride, flattened row-major as the model emits them.
pub struct Heads<'a> {
    pub cls: &'a [f32],
    pub obj: &'a [f32],
    pub bbox: &'a [f32],
    pub kps: &'a [f32],
}

/// Decodes all strides and keeps candidates scoring at least `score_thr`.
pub fn decode(heads: &[Heads; 3], w: usize, h: usize, score_thr: f32) -> Vec<Face> {
    let mut faces = Vec::new();
    for ((stride, cols, rows), hd) in prior_grids(w, h).into_iter().zip(heads.iter()) {
        let s = stride as f32;
        for r in 0..rows {
            for c in 0..cols {
                let idx = r * cols + c;
                let cls = hd.cls[idx].clamp(0.0, 1.0);
                let obj = hd.obj[idx].clamp(0.0, 1.0);
                let score = (cls * obj).sqrt();
                if score < score_thr {
                    continue;
                }
                let b = &hd.bbox[idx * 4..idx * 4 + 4];
                let (cx, cy) = ((c as f32 + b[0]) * s, (r as f32 + b[1]) * s);
                let (bw, bh) = (b[2].exp() * s, b[3].exp() * s);
                let k = &hd.kps[idx * 10..idx * 10 + 10];
                let mut landmarks = [[0f32; 2]; 5];
                for (n, p) in landmarks.iter_mut().enumerate() {
                    *p = [(k[2 * n] + c as f32) * s, (k[2 * n + 1] + r as f32) * s];
                }
                faces.push(Face {
                    x: cx - bw / 2.0,
                    y: cy - bh / 2.0,
                    w: bw,
                    h: bh,
                    landmarks,
                    score,
                });
            }
        }
    }
    faces
}

pub fn iou(a: &Face, b: &Face) -> f32 {
    let x1 = a.x.max(b.x);
    let y1 = a.y.max(b.y);
    let x2 = (a.x + a.w).min(b.x + b.w);
    let y2 = (a.y + a.h).min(b.y + b.h);
    let inter = (x2 - x1).max(0.0) * (y2 - y1).max(0.0);
    let union = a.w * a.h + b.w * b.h - inter;
    if union <= 0.0 {
        0.0
    } else {
        inter / union
    }
}

/// Greedy NMS: highest score first, drop anything overlapping a kept box by
/// more than `iou_thr`. Output is sorted by descending score.
pub fn nms(mut faces: Vec<Face>, iou_thr: f32) -> Vec<Face> {
    faces.sort_by(|a, b| b.score.total_cmp(&a.score));
    let mut kept: Vec<Face> = Vec::new();
    for f in faces {
        if kept.iter().all(|k| iou(k, &f) <= iou_thr) {
            kept.push(f);
        }
    }
    kept
}

#[cfg(test)]
mod tests {
    use super::*;

    fn face(x: f32, y: f32, w: f32, h: f32, score: f32) -> Face {
        Face {
            x,
            y,
            w,
            h,
            landmarks: [[0.0; 2]; 5],
            score,
        }
    }

    #[test]
    fn prior_grids_match_the_model_head_lengths() {
        let g = prior_grids(640, 640);
        assert_eq!(g, vec![(8, 80, 80), (16, 40, 40), (32, 20, 20)]);
        // 6400 + 1600 + 400 rows, as in the ONNX outputs cls_8/16/32.
        assert_eq!(g.iter().map(|(_, c, r)| c * r).sum::<usize>(), 8400);
    }

    #[test]
    fn decode_places_box_and_landmarks_on_the_prior_grid() {
        let (cols, rows) = (80usize, 80usize);
        let n8 = cols * rows;
        let mut cls8 = vec![0.0; n8];
        let mut obj8 = vec![0.0; n8];
        let mut bbox8 = vec![0.0; n8 * 4];
        let mut kps8 = vec![0.0; n8 * 10];
        // Cell (r=10, c=20) of stride 8.
        let idx = 10 * cols + 20;
        cls8[idx] = 0.81;
        obj8[idx] = 1.5; // clamped to 1
        bbox8[idx * 4..idx * 4 + 4].copy_from_slice(&[0.5, 0.25, 2f32.ln(), 0.0]);
        kps8[idx * 10] = 1.0; // landmark 0 x offset
        kps8[idx * 10 + 1] = -1.0; // landmark 0 y offset
        let z16 = vec![0.0; 1600 * 10];
        let z32 = vec![0.0; 400 * 10];
        let heads = [
            Heads {
                cls: &cls8,
                obj: &obj8,
                bbox: &bbox8,
                kps: &kps8,
            },
            Heads {
                cls: &z16,
                obj: &z16,
                bbox: &z16,
                kps: &z16,
            },
            Heads {
                cls: &z32,
                obj: &z32,
                bbox: &z32,
                kps: &z32,
            },
        ];
        let faces = decode(&heads, 640, 640, 0.6);
        assert_eq!(faces.len(), 1);
        let f = &faces[0];
        assert!((f.score - 0.9).abs() < 1e-6);
        // centre (20.5*8, 10.25*8) = (164, 82); size (16, 8)
        assert!((f.x - 156.0).abs() < 1e-4 && (f.y - 78.0).abs() < 1e-4);
        assert!((f.w - 16.0).abs() < 1e-4 && (f.h - 8.0).abs() < 1e-4);
        assert_eq!(f.landmarks[0], [168.0, 72.0]);
        assert_eq!(f.landmarks[1], [160.0, 80.0]);
        // Below threshold: nothing.
        assert!(decode(&heads, 640, 640, 0.95).is_empty());
    }

    #[test]
    fn iou_of_identical_disjoint_and_half_overlap() {
        let a = face(0.0, 0.0, 10.0, 10.0, 1.0);
        assert!((iou(&a, &a) - 1.0).abs() < 1e-6);
        assert_eq!(iou(&a, &face(20.0, 20.0, 5.0, 5.0, 1.0)), 0.0);
        // Overlap 5x10 = 50, union 150.
        assert!((iou(&a, &face(5.0, 0.0, 10.0, 10.0, 1.0)) - 1.0 / 3.0).abs() < 1e-6);
    }

    #[test]
    fn nms_keeps_best_of_a_cluster_and_separate_faces() {
        let faces = vec![
            face(0.0, 0.0, 10.0, 10.0, 0.7),
            face(1.0, 1.0, 10.0, 10.0, 0.9),
            face(100.0, 100.0, 10.0, 10.0, 0.8),
            face(6.0, 0.0, 10.0, 10.0, 0.75), // IoU with best 45/155 ~ 0.29 < 0.3: kept
        ];
        let kept = nms(faces, 0.3);
        let scores: Vec<f32> = kept.iter().map(|f| f.score).collect();
        assert_eq!(scores, vec![0.9, 0.8, 0.75]);
    }

    #[test]
    fn scaling_maps_box_and_landmarks() {
        let mut f = face(10.0, 20.0, 30.0, 40.0, 0.9);
        f.landmarks[2] = [15.0, 25.0];
        let g = f.scaled(2.0);
        assert_eq!((g.x, g.y, g.w, g.h), (20.0, 40.0, 60.0, 80.0));
        assert_eq!(g.landmarks[2], [30.0, 50.0]);
        assert_eq!(g.size(), 60.0);
    }
}
