//! Five-point face alignment to the SFace / ArcFace 112x112 template, and the
//! vector maths on embeddings.
//!
//! OpenCV's `FaceRecognizerSF::alignCrop` estimates a similarity transform
//! (rotation + uniform scale + translation, no reflection) from the five YuNet
//! landmarks to the template below with Umeyama's method, then warps with
//! INTER_LINEAR. In 2-D the non-reflective least-squares similarity has a
//! closed form: treating points as complex numbers, the rotation-and-scale is
//! `a + ib = sum(q_i * conj(p_i)) / sum(|p_i|^2)` over the de-meaned source `p`
//! and destination `q`. This equals Umeyama's solution whenever the optimal
//! transform is not a reflection (always the case for a detected face).

/// Destination landmarks (right eye, left eye, nose, right mouth, left mouth)
/// in the 112x112 crop, from OpenCV `face_recognize.cpp`.
pub const TEMPLATE: [[f32; 2]; 5] = [
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
];
pub const CROP: u32 = 112;

/// Least-squares similarity mapping `src` onto `dst`, as a 2x3 forward matrix
/// `[[a, -b, tx], [b, a, ty]]`.
pub fn similarity(src: &[[f32; 2]; 5], dst: &[[f32; 2]; 5]) -> [[f32; 3]; 2] {
    let mean = |pts: &[[f32; 2]; 5]| {
        let (sx, sy) = pts
            .iter()
            .fold((0f64, 0f64), |(x, y), p| (x + p[0] as f64, y + p[1] as f64));
        (sx / 5.0, sy / 5.0)
    };
    let (smx, smy) = mean(src);
    let (dmx, dmy) = mean(dst);
    let (mut re, mut im, mut norm) = (0f64, 0f64, 0f64);
    for (p, q) in src.iter().zip(dst.iter()) {
        let (px, py) = (p[0] as f64 - smx, p[1] as f64 - smy);
        let (qx, qy) = (q[0] as f64 - dmx, q[1] as f64 - dmy);
        // q * conj(p)
        re += qx * px + qy * py;
        im += qy * px - qx * py;
        norm += px * px + py * py;
    }
    if norm <= f64::EPSILON {
        return [[0.0; 3]; 2];
    }
    let (a, b) = (re / norm, im / norm);
    let tx = dmx - (a * smx - b * smy);
    let ty = dmy - (b * smx + a * smy);
    [
        [a as f32, -b as f32, tx as f32],
        [b as f32, a as f32, ty as f32],
    ]
}

pub fn apply(m: &[[f32; 3]; 2], p: [f32; 2]) -> [f32; 2] {
    [
        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2],
        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2],
    ]
}

/// Scales `v` to unit length in place; returns false (and leaves `v`) when it
/// is zero or not finite.
pub fn l2_normalize(v: &mut [f32]) -> bool {
    let n: f64 = v
        .iter()
        .map(|x| (*x as f64) * (*x as f64))
        .sum::<f64>()
        .sqrt();
    if !n.is_finite() || n <= 1e-12 {
        return false;
    }
    for x in v.iter_mut() {
        *x = (*x as f64 / n) as f32;
    }
    true
}

/// Cosine similarity of two unit vectors (a plain dot product).
pub fn cosine(a: &[f32], b: &[f32]) -> f32 {
    a.iter()
        .zip(b.iter())
        .map(|(x, y)| (*x as f64) * (*y as f64))
        .sum::<f64>() as f32
}

/// Mean of unit vectors, re-normalised: the enrolment template.
pub fn mean_template(embeddings: &[Vec<f32>]) -> Option<Vec<f32>> {
    let dims = embeddings.first()?.len();
    let mut acc = vec![0f32; dims];
    for e in embeddings {
        for (a, x) in acc.iter_mut().zip(e.iter()) {
            *a += *x;
        }
    }
    l2_normalize(&mut acc).then_some(acc)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: [f32; 2], b: [f32; 2], tol: f32) -> bool {
        (a[0] - b[0]).abs() < tol && (a[1] - b[1]).abs() < tol
    }

    #[test]
    fn template_maps_onto_itself() {
        let m = similarity(&TEMPLATE, &TEMPLATE);
        assert!((m[0][0] - 1.0).abs() < 1e-5 && m[1][0].abs() < 1e-5);
        assert!(m[0][2].abs() < 1e-3 && m[1][2].abs() < 1e-3);
    }

    #[test]
    fn recovers_a_known_rotation_scale_translation() {
        // dst = R(30deg) * 2.5 * src + (7, -3)
        let (s, th) = (2.5f32, 30f32.to_radians());
        let (a, b) = (s * th.cos(), s * th.sin());
        let truth = [[a, -b, 7.0], [b, a, -3.0]];
        let src = [
            [10.0, 12.0],
            [40.0, 11.0],
            [25.0, 30.0],
            [14.0, 45.0],
            [38.0, 44.0],
        ];
        let dst = src.map(|p| apply(&truth, p));
        let m = similarity(&src, &dst);
        for (r, t) in m.iter().zip(truth.iter()) {
            for (x, y) in r.iter().zip(t.iter()) {
                assert!((x - y).abs() < 1e-3, "{m:?} vs {truth:?}");
            }
        }
    }

    #[test]
    fn aligns_detected_landmarks_close_to_the_template() {
        // Landmarks of a slightly tilted face: the fitted transform should put
        // the eyes on the template eyes within a pixel or two.
        let lm = [
            [272.0, 113.2],
            [319.4, 113.4],
            [291.1, 138.1],
            [273.6, 159.4],
            [316.7, 159.6],
        ];
        let m = similarity(&lm, &TEMPLATE);
        assert!(close(apply(&m, lm[0]), TEMPLATE[0], 3.0));
        assert!(close(apply(&m, lm[1]), TEMPLATE[1], 3.0));
        // Uniform scale, no shear.
        assert!((m[0][0] - m[1][1]).abs() < 1e-6 && (m[0][1] + m[1][0]).abs() < 1e-6);
    }

    #[test]
    fn degenerate_points_give_a_zero_matrix() {
        let p = [[5.0, 5.0]; 5];
        assert_eq!(similarity(&p, &TEMPLATE), [[0.0; 3]; 2]);
    }

    #[test]
    fn cosine_and_normalisation() {
        let mut a = vec![3.0, 4.0];
        assert!(l2_normalize(&mut a));
        assert_eq!(a, vec![0.6, 0.8]);
        assert!((cosine(&a, &a) - 1.0).abs() < 1e-6);
        assert!((cosine(&a, &[-0.6, -0.8]) + 1.0).abs() < 1e-6);
        assert!(cosine(&[1.0, 0.0], &[0.0, 1.0]).abs() < 1e-9);
        let mut z = vec![0.0, 0.0];
        assert!(!l2_normalize(&mut z));
        let mut nan = vec![f32::NAN, 1.0];
        assert!(!l2_normalize(&mut nan));
    }

    #[test]
    fn mean_template_is_unit_length_and_between_inputs() {
        let t = mean_template(&[vec![1.0, 0.0], vec![0.0, 1.0]]).unwrap();
        let h = std::f32::consts::FRAC_1_SQRT_2;
        assert!((t[0] - h).abs() < 1e-6 && (t[1] - h).abs() < 1e-6);
        assert!(mean_template(&[vec![1.0, 0.0], vec![-1.0, 0.0]]).is_none());
        assert!(mean_template(&[]).is_none());
    }
}
