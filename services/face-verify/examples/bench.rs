//! Per-stage latency and scores on the test images:
//! `cargo run --release --example bench [iterations]`.
//! Prints no image data; useful to compare against the OpenCV reference.

use std::time::Instant;

use face_verify::{align, engine::Gates, imaging, Engine};

fn main() {
    let iters: usize = std::env::args()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(20);
    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let engine = Engine::load(&root.join("models")).expect("models (run models/fetch.sh)");
    let g = Gates {
        det_score: 0.6,
        min_face_px: 80.0,
        blur_min: 60.0,
        liveness_threshold: 0.5,
        require_liveness: false,
    };
    let names = [
        "biden_2013.jpg",
        "biden_2021.jpg",
        "obama_2012.jpg",
        "harris_2021.jpg",
        "minivision_image_T1.jpg",
        "minivision_image_F1.jpg",
        "minivision_image_F2.jpg",
    ];
    let mut embs = Vec::new();
    for n in names {
        let bytes = std::fs::read(root.join("tests/images").join(n)).unwrap();
        let img = imaging::decode_image(&bytes, 16_000_000).unwrap();
        let faces = engine.detect(&img, 0.6).unwrap();
        let f = &faces[0];
        let aligned = engine.align(&img, f);
        println!(
            "{n}: faces {} box [{:.1} {:.1} {:.1} {:.1}] lm {:?} score {:.3} sharp {:.0} live {:.3}",
            faces.len(),
            f.x,
            f.y,
            f.w,
            f.h,
            f.landmarks.map(|p| [p[0].round(), p[1].round()]),
            f.score,
            imaging::laplacian_variance(&aligned, 16),
            engine.liveness(&img, f).unwrap()
        );
        embs.push((n, engine.embed_aligned(&aligned).unwrap()));
    }
    for i in 0..embs.len() {
        for j in i + 1..embs.len() {
            println!(
                "  cos {} {} {:.3}",
                embs[i].0,
                embs[j].0,
                align::cosine(&embs[i].1, &embs[j].1)
            );
        }
    }

    // Timings on a 640x480-class frame (decode + full pipeline) and per stage.
    let bytes = std::fs::read(root.join("tests/images/minivision_image_T1.jpg")).unwrap();
    let img = imaging::decode_image(&bytes, 16_000_000).unwrap();
    let face = engine.detect(&img, 0.6).unwrap().remove(0);
    let aligned = engine.align(&img, &face);
    let time = |label: &str, f: &mut dyn FnMut()| {
        f();
        let t0 = Instant::now();
        for _ in 0..iters {
            f();
        }
        println!(
            "{label:<28} {:7.2} ms",
            t0.elapsed().as_secs_f64() * 1e3 / iters as f64
        );
    };
    time("decode (jpeg 480x640)", &mut || {
        imaging::decode_image(&bytes, 16_000_000).unwrap();
    });
    time("detect (yunet 640x640)", &mut || {
        engine.detect(&img, 0.6).unwrap();
    });
    time("align", &mut || {
        engine.align(&img, &face);
    });
    time("embed (sface)", &mut || {
        engine.embed_aligned(&aligned).unwrap();
    });
    time("liveness (2x minifasnet)", &mut || {
        engine.liveness(&img, &face).unwrap();
    });
    time("full frame (decode+analyse)", &mut || {
        let img = imaging::decode_image(&bytes, 16_000_000).unwrap();
        engine.analyse(img, &g).unwrap();
    });
}
