//! Image decoding and the small amount of pixel math the pipeline needs.
//!
//! Resampling deliberately mirrors OpenCV (`cv::resize` INTER_LINEAR and
//! `cv::warpAffine` with a zero border): the recogniser and the liveness
//! models were trained and evaluated on OpenCV-prepared crops.

use std::io::Cursor;

use base64::Engine as _;
use image::{imageops::FilterType, DynamicImage, ImageDecoder, ImageFormat, ImageReader, RgbImage};

/// Longest side of the working image. Larger inputs are downscaled (with an
/// antialiasing filter) once after decoding; faces are still several hundred
/// pixels wide at this size and every later step stays cheap.
pub const WORK_MAX_SIDE: u32 = 1280;

#[derive(Debug, PartialEq)]
pub enum ImageError {
    /// Not base64, or not a JPEG/PNG we can decode.
    Invalid(String),
    /// Declared dimensions exceed the pixel budget (decode-bomb guard).
    TooLarge { pixels: u64, max: u64 },
}

impl std::fmt::Display for ImageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ImageError::Invalid(m) => write!(f, "invalid image: {m}"),
            ImageError::TooLarge { pixels, max } => {
                write!(f, "image has {pixels} pixels, the limit is {max}")
            }
        }
    }
}

/// Base64 (optionally a `data:image/...;base64,` URL) to raw bytes.
pub fn decode_base64(s: &str) -> Result<Vec<u8>, ImageError> {
    let payload = match s.strip_prefix("data:") {
        Some(rest) => rest.split_once(',').map(|(_, b)| b).unwrap_or(""),
        None => s,
    };
    base64::engine::general_purpose::STANDARD
        .decode(payload.trim())
        .map_err(|_| ImageError::Invalid("not valid base64".into()))
}

/// Decodes a JPEG or PNG, applies the EXIF orientation and returns RGB.
/// The header is read first and anything over `max_pixels` is refused
/// before a single pixel buffer is allocated.
pub fn decode_image(bytes: &[u8], max_pixels: u64) -> Result<RgbImage, ImageError> {
    let invalid = |e: image::ImageError| ImageError::Invalid(e.to_string());
    let mut reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|e| ImageError::Invalid(e.to_string()))?;
    match reader.format() {
        Some(ImageFormat::Jpeg) | Some(ImageFormat::Png) => {}
        _ => return Err(ImageError::Invalid("only JPEG and PNG are accepted".into())),
    }
    let mut limits = image::Limits::default();
    // 16-bit RGBA PNG is the widest layout we accept: 8 bytes per pixel.
    limits.max_alloc = Some(max_pixels.saturating_mul(8));
    reader.limits(limits);
    let mut decoder = reader.into_decoder().map_err(invalid)?;
    let (w, h) = decoder.dimensions();
    let pixels = u64::from(w) * u64::from(h);
    if pixels > max_pixels {
        return Err(ImageError::TooLarge {
            pixels,
            max: max_pixels,
        });
    }
    if w == 0 || h == 0 {
        return Err(ImageError::Invalid("empty image".into()));
    }
    let orientation = decoder.orientation().map_err(invalid)?;
    let mut img = DynamicImage::from_decoder(decoder).map_err(invalid)?;
    img.apply_orientation(orientation);
    Ok(img.to_rgb8())
}

/// Shrinks so the longest side is at most `max_side` (antialiased). Returns
/// the image and the factor applied (`<= 1`).
pub fn limit_size(img: RgbImage, max_side: u32) -> (RgbImage, f32) {
    let (w, h) = img.dimensions();
    let longest = w.max(h);
    if longest <= max_side {
        return (img, 1.0);
    }
    let s = max_side as f32 / longest as f32;
    let nw = ((w as f32 * s).round() as u32).max(1);
    let nh = ((h as f32 * s).round() as u32).max(1);
    (
        image::imageops::resize(&img, nw, nh, FilterType::Triangle),
        s,
    )
}

#[inline]
fn px(img: &RgbImage, x: i64, y: i64) -> [f32; 3] {
    if x < 0 || y < 0 || x >= img.width() as i64 || y >= img.height() as i64 {
        return [0.0; 3];
    }
    let p = img.get_pixel(x as u32, y as u32).0;
    [p[0] as f32, p[1] as f32, p[2] as f32]
}

/// OpenCV INTER_LINEAR resize (half-pixel centres, edge clamp, no antialias)
/// of the inclusive source rectangle `[x0, x1] x [y0, y1]`.
pub fn resize_bilinear_cv(
    img: &RgbImage,
    (x0, y0, x1, y1): (u32, u32, u32, u32),
    out_w: u32,
    out_h: u32,
) -> RgbImage {
    let sw = (x1 - x0 + 1) as f32;
    let sh = (y1 - y0 + 1) as f32;
    let (sx_scale, sy_scale) = (sw / out_w as f32, sh / out_h as f32);
    let axis = |d: u32, scale: f32, len: f32| -> (i64, f32) {
        let s = (d as f32 + 0.5) * scale - 0.5;
        let mut i = s.floor();
        let mut f = s - i;
        if i < 0.0 {
            i = 0.0;
            f = 0.0;
        }
        if i >= len - 1.0 {
            i = len - 1.0;
            f = 0.0;
        }
        (i as i64, f)
    };
    let mut out = RgbImage::new(out_w, out_h);
    for dy in 0..out_h {
        let (iy, fy) = axis(dy, sy_scale, sh);
        for dx in 0..out_w {
            let (ix, fx) = axis(dx, sx_scale, sw);
            let (ax, ay) = (x0 as i64 + ix, y0 as i64 + iy);
            let x1c = (ax + 1).min(x1 as i64);
            let y1c = (ay + 1).min(y1 as i64);
            let (p00, p10) = (px(img, ax, ay), px(img, x1c, ay));
            let (p01, p11) = (px(img, ax, y1c), px(img, x1c, y1c));
            let mut v = [0u8; 3];
            for c in 0..3 {
                let top = p00[c] * (1.0 - fx) + p10[c] * fx;
                let bot = p01[c] * (1.0 - fx) + p11[c] * fx;
                v[c] = (top * (1.0 - fy) + bot * fy).round().clamp(0.0, 255.0) as u8;
            }
            out.put_pixel(dx, dy, image::Rgb(v));
        }
    }
    out
}

/// `cv::warpAffine(src, dst, m, size, INTER_LINEAR, BORDER_CONSTANT, 0)`:
/// `m` maps source to destination; each destination pixel samples the source
/// at `m^-1 (x, y)`.
pub fn warp_affine(img: &RgbImage, m: &[[f32; 3]; 2], out_w: u32, out_h: u32) -> RgbImage {
    let [[a, b, tx], [c, d, ty]] = *m;
    let det = a * d - b * c;
    let mut out = RgbImage::new(out_w, out_h);
    if det.abs() < 1e-12 {
        return out;
    }
    let (ia, ib, ic, id) = (d / det, -b / det, -c / det, a / det);
    for y in 0..out_h {
        for x in 0..out_w {
            let (u, v) = (x as f32 - tx, y as f32 - ty);
            let sx = ia * u + ib * v;
            let sy = ic * u + id * v;
            let (fx0, fy0) = (sx.floor(), sy.floor());
            let (fx, fy) = (sx - fx0, sy - fy0);
            let (x0, y0) = (fx0 as i64, fy0 as i64);
            let (p00, p10) = (px(img, x0, y0), px(img, x0 + 1, y0));
            let (p01, p11) = (px(img, x0, y0 + 1), px(img, x0 + 1, y0 + 1));
            let mut val = [0u8; 3];
            for k in 0..3 {
                let top = p00[k] * (1.0 - fx) + p10[k] * fx;
                let bot = p01[k] * (1.0 - fx) + p11[k] * fx;
                val[k] = (top * (1.0 - fy) + bot * fy).round().clamp(0.0, 255.0) as u8;
            }
            out.put_pixel(x, y, image::Rgb(val));
        }
    }
    out
}

/// Variance of the 4-neighbour Laplacian (OpenCV `Laplacian`, ksize 1) of the
/// luma, over the central window of `img` inset by `inset` pixels: the face
/// region of an aligned crop, away from any black warp border.
pub fn laplacian_variance(img: &RgbImage, inset: u32) -> f64 {
    let (w, h) = img.dimensions();
    let luma = |x: u32, y: u32| -> f64 {
        let p = img.get_pixel(x, y).0;
        0.299 * p[0] as f64 + 0.587 * p[1] as f64 + 0.114 * p[2] as f64
    };
    let (lo, hix, hiy) = (
        inset.max(1),
        w.saturating_sub(inset.max(1)),
        h.saturating_sub(inset.max(1)),
    );
    let (mut n, mut sum, mut sq) = (0f64, 0f64, 0f64);
    for y in lo..hiy {
        for x in lo..hix {
            let l = luma(x - 1, y) + luma(x + 1, y) + luma(x, y - 1) + luma(x, y + 1)
                - 4.0 * luma(x, y);
            n += 1.0;
            sum += l;
            sq += l * l;
        }
    }
    if n == 0.0 {
        return 0.0;
    }
    let mean = sum / n;
    sq / n - mean * mean
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_warp_copies_pixels() {
        let mut img = RgbImage::new(4, 3);
        img.put_pixel(2, 1, image::Rgb([10, 20, 30]));
        let out = warp_affine(&img, &[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], 4, 3);
        assert_eq!(out, img);
    }

    #[test]
    fn translation_warp_shifts_and_pads_with_black() {
        let mut img = RgbImage::from_pixel(2, 2, image::Rgb([100, 100, 100]));
        img.put_pixel(0, 0, image::Rgb([7, 8, 9]));
        // dst = src + (1, 0)
        let out = warp_affine(&img, &[[1.0, 0.0, 1.0], [0.0, 1.0, 0.0]], 3, 2);
        assert_eq!(out.get_pixel(0, 0).0, [0, 0, 0]);
        assert_eq!(out.get_pixel(1, 0).0, [7, 8, 9]);
        assert_eq!(out.get_pixel(2, 1).0, [100, 100, 100]);
    }

    #[test]
    fn cv_resize_of_a_constant_is_constant_and_halving_averages() {
        let img = RgbImage::from_pixel(10, 10, image::Rgb([50, 60, 70]));
        let out = resize_bilinear_cv(&img, (0, 0, 9, 9), 3, 7);
        assert!(out.pixels().all(|p| p.0 == [50, 60, 70]));
        // 2x downscale samples exactly between two source pixels.
        let mut stripes = RgbImage::new(4, 1);
        for x in 0..4 {
            let v = if x % 2 == 0 { 0 } else { 200 };
            stripes.put_pixel(x, 0, image::Rgb([v, v, v]));
        }
        let half = resize_bilinear_cv(&stripes, (0, 0, 3, 0), 2, 1);
        assert_eq!(half.get_pixel(0, 0).0, [100, 100, 100]);
    }

    #[test]
    fn laplacian_is_zero_on_flat_and_high_on_checkerboard() {
        let flat = RgbImage::from_pixel(20, 20, image::Rgb([90, 90, 90]));
        assert_eq!(laplacian_variance(&flat, 2), 0.0);
        let checker = RgbImage::from_fn(20, 20, |x, y| {
            let v = if (x + y) % 2 == 0 { 0 } else { 255 };
            image::Rgb([v, v, v])
        });
        assert!(laplacian_variance(&checker, 2) > 1e5);
    }

    #[test]
    fn base64_accepts_data_urls_and_rejects_garbage() {
        assert_eq!(decode_base64("aGk=").unwrap(), b"hi");
        assert_eq!(decode_base64("data:image/png;base64,aGk=").unwrap(), b"hi");
        assert!(decode_base64("@@@").is_err());
    }

    #[test]
    fn non_images_and_other_formats_are_refused() {
        assert!(matches!(
            decode_image(b"hello", 1000),
            Err(ImageError::Invalid(_))
        ));
        // GIF header: a real format, but not one we accept.
        assert!(matches!(
            decode_image(b"GIF89a\x01\x00\x01\x00\x00\x00\x00;", 1000),
            Err(ImageError::Invalid(_))
        ));
    }

    #[test]
    fn decode_bomb_is_refused_from_the_header() {
        // The pixel budget is checked against the header: exactly 64x64 passes, one less fails.
        let mut buf = Vec::new();
        RgbImage::new(64, 64)
            .write_to(&mut Cursor::new(&mut buf), ImageFormat::Png)
            .unwrap();
        assert!(decode_image(&buf, 64 * 64).is_ok());
        assert_eq!(
            decode_image(&buf, 64 * 64 - 1),
            Err(ImageError::TooLarge {
                pixels: 4096,
                max: 4095
            })
        );
    }

    #[test]
    fn limit_size_keeps_aspect() {
        let (img, s) = limit_size(RgbImage::new(2560, 1440), 1280);
        assert_eq!(img.dimensions(), (1280, 720));
        assert!((s - 0.5).abs() < 1e-6);
        let (img, s) = limit_size(RgbImage::new(640, 480), 1280);
        assert_eq!((img.dimensions(), s), ((640, 480), 1.0));
    }
}
