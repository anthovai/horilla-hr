//! End-to-end tests through the HTTP router with the real models.
//!
//! Needs the ONNX files: run `models/fetch.sh` once (CI does). Test images
//! and their licences: tests/images/SOURCES.md. Multi-face, no-face, small and
//! blurry inputs are synthesised from those images at run time.

use std::io::Cursor;
use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use base64::Engine as _;
use face_verify::{app, AppState, Config, Engine};
use http_body_util::BodyExt;
use image::{imageops, ImageFormat, RgbImage};
use serde_json::{json, Value};
use tower::ServiceExt;

const TOKEN: &str = "test-token-0123456789abcdef";
const MODEL: &str = "sface_2021dec@1";

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn engine() -> Arc<Engine> {
    static E: OnceLock<Arc<Engine>> = OnceLock::new();
    E.get_or_init(|| {
        Arc::new(Engine::load(&root().join("models")).unwrap_or_else(|e| {
            panic!("{e}\nRun services/face-verify/models/fetch.sh to download the models.")
        }))
    })
    .clone()
}

fn image_bytes(name: &str) -> Vec<u8> {
    std::fs::read(root().join("tests/images").join(name)).unwrap()
}

fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

fn img(name: &str) -> String {
    b64(&image_bytes(name))
}

fn rgb(name: &str) -> RgbImage {
    face_verify::imaging::decode_image(&image_bytes(name), 100_000_000).unwrap()
}

fn png(img: &RgbImage) -> String {
    let mut buf = Vec::new();
    img.write_to(&mut Cursor::new(&mut buf), ImageFormat::Png)
        .unwrap();
    b64(&buf)
}

async fn call_with(cfg: Config, req: Request<Body>) -> (StatusCode, Value) {
    let res = app(AppState::new(engine(), cfg))
        .oneshot(req)
        .await
        .unwrap();
    let status = res.status();
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    let v = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, v)
}

fn post(path: &str, body: &Value) -> Request<Body> {
    Request::post(path)
        .header("content-type", "application/json")
        .header("x-service-token", TOKEN)
        .body(Body::from(body.to_string()))
        .unwrap()
}

async fn call(req: Request<Body>) -> (StatusCode, Value) {
    call_with(Config::with_token(TOKEN), req).await
}

async fn enrol(names: &[&str]) -> Vec<f64> {
    let images: Vec<String> = names.iter().map(|n| img(n)).collect();
    let (s, v) = call(post("/v1/embed", &json!({ "images": images }))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v["template"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_f64().unwrap())
        .collect()
}

async fn verify(image: String, template: &[f64]) -> Value {
    let (s, v) = call(post(
        "/v1/verify",
        &json!({ "image": image, "templates": [template], "model": MODEL }),
    ))
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v
}

// ------------------------------------------------------------------ auth

#[tokio::test]
async fn healthz_requires_the_token_and_lists_models() {
    let ok = Request::get("/healthz")
        .header("x-service-token", TOKEN)
        .body(Body::empty())
        .unwrap();
    let (s, v) = call(ok).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["models"]["recognizer"], MODEL);
    assert!(v["models"]["detector"].is_string() && v["models"]["liveness"].is_string());

    let none = Request::get("/healthz").body(Body::empty()).unwrap();
    assert_eq!(call(none).await.0, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn wrong_or_missing_token_is_401_before_any_work() {
    let body = json!({ "images": [img("biden_2013.jpg")] }).to_string();
    for token in [
        Some("nope"),
        Some(""),
        Some(&TOKEN[..TOKEN.len() - 1]),
        None,
    ] {
        let mut b = Request::post("/v1/embed").header("content-type", "application/json");
        if let Some(t) = token {
            b = b.header("x-service-token", t);
        }
        let (s, v) = call(b.body(Body::from(body.clone())).unwrap()).await;
        assert_eq!(s, StatusCode::UNAUTHORIZED, "token {token:?}");
        assert!(v["error"].is_string());
    }
}

// ------------------------------------------------------------------ limits

#[tokio::test]
async fn oversized_body_is_413() {
    let mut cfg = Config::with_token(TOKEN);
    cfg.max_body_bytes = 64 * 1024;
    let (s, _) = call_with(
        cfg,
        post("/v1/embed", &json!({ "images": [img("biden_2013.jpg")] })),
    )
    .await;
    assert_eq!(s, StatusCode::PAYLOAD_TOO_LARGE);
}

#[tokio::test]
async fn too_many_pixels_is_413_without_decoding() {
    let mut cfg = Config::with_token(TOKEN);
    cfg.max_pixels = 100_000; // the 500x625 portrait is 312,500
    let (s, v) = call_with(
        cfg,
        post("/v1/embed", &json!({ "images": [img("biden_2013.jpg")] })),
    )
    .await;
    assert_eq!(s, StatusCode::PAYLOAD_TOO_LARGE, "{v}");
}

#[tokio::test]
async fn bad_inputs_are_400() {
    let cases = [
        ("/v1/embed", json!({ "images": [] })),
        (
            "/v1/embed",
            json!({ "images": vec![img("biden_2013.jpg"); 6] }),
        ),
        ("/v1/embed", json!({ "images": ["not base64!"] })),
        ("/v1/embed", json!({ "images": [b64(b"GIF89a....")] })),
        ("/v1/embed", json!({ "pictures": [] })),
        (
            "/v1/verify",
            json!({ "image": img("biden_2013.jpg"), "templates": [[1.0, 2.0]], "model": MODEL }),
        ),
        (
            "/v1/verify",
            json!({ "image": img("biden_2013.jpg"), "templates": [vec![0.0; 128]], "model": MODEL }),
        ),
        (
            "/v1/verify",
            json!({ "image": img("biden_2013.jpg"), "templates": [], "model": MODEL }),
        ),
    ];
    for (path, body) in cases {
        let (s, v) = call(post(path, &body)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{path} {v}");
        assert!(v["error"].is_string());
    }
}

// ------------------------------------------------------------------ embed

#[tokio::test]
async fn embed_returns_a_unit_template_and_frame_report() {
    let (s, v) = call(post(
        "/v1/embed",
        &json!({ "images": [img("biden_2013.jpg"), img("biden_2021.jpg")] }),
    ))
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["dims"], 128);
    assert_eq!(v["model"], MODEL);
    let t: Vec<f64> = v["template"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_f64().unwrap())
        .collect();
    assert_eq!(t.len(), 128);
    let norm: f64 = t.iter().map(|x| x * x).sum::<f64>().sqrt();
    assert!((norm - 1.0).abs() < 1e-4, "norm {norm}");
    let frames = v["frames"].as_array().unwrap();
    assert_eq!(frames.len(), 2);
    for f in frames {
        assert_eq!(f["faces"], 1);
        assert_eq!(f["accepted"], true);
        assert!(f["reason"].is_null());
        let q = f["quality"].as_f64().unwrap();
        assert!((0.0..=1.0).contains(&q) && q > 0.5, "{f}");
        assert!(f["live_score"].as_f64().unwrap() > 0.9, "{f}");
    }
}

#[tokio::test]
async fn embed_with_no_accepted_frame_is_422_with_reasons() {
    let blank = RgbImage::from_fn(320, 240, |x, y| {
        image::Rgb([(x / 2) as u8, (y / 2) as u8, 128])
    });
    let (s, v) = call(post(
        "/v1/embed",
        &json!({ "images": [png(&blank), img("minivision_image_F2.jpg")] }),
    ))
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY, "{v}");
    let frames = v["frames"].as_array().unwrap();
    assert_eq!(frames[0]["reason"], "no_face");
    assert_eq!(frames[0]["faces"], 0);
    assert_eq!(frames[1]["reason"], "not_live");
    assert_eq!(frames[1]["accepted"], false);
}

#[tokio::test]
async fn embed_keeps_good_frames_and_reports_bad_ones() {
    let blank = RgbImage::from_pixel(200, 200, image::Rgb([30, 30, 30]));
    let (s, v) = call(post(
        "/v1/embed",
        &json!({ "images": [img("biden_2013.jpg"), png(&blank)] }),
    ))
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["frames"][0]["accepted"], true);
    assert_eq!(v["frames"][1]["reason"], "no_face");
}

// ------------------------------------------------------------------ verify

#[tokio::test]
async fn same_person_matches_across_years() {
    let t = enrol(&["biden_2013.jpg"]).await;
    let v = verify(img("biden_2021.jpg"), &t).await;
    assert_eq!(v["match"], true, "{v}");
    assert!(v["score"].as_f64().unwrap() > 0.5, "{v}");
    assert_eq!(v["faces"], 1);
    assert_eq!(v["live"], true);
    assert!(v["reason"].is_null());
    assert!((v["threshold"].as_f64().unwrap() - 0.363).abs() < 1e-6);
}

#[tokio::test]
async fn different_people_do_not_match() {
    let t = enrol(&["biden_2013.jpg", "biden_2021.jpg"]).await;
    for other in ["obama_2012.jpg", "harris_2021.jpg"] {
        let v = verify(img(other), &t).await;
        assert_eq!(v["match"], false, "{other}: {v}");
        assert!(v["score"].as_f64().unwrap() < 0.3, "{other}: {v}");
        assert!(
            v["reason"].is_null(),
            "a clean non-match has no reason: {v}"
        );
    }
}

#[tokio::test]
async fn best_of_several_templates_is_used() {
    let obama = enrol(&["obama_2012.jpg"]).await;
    let biden = enrol(&["biden_2013.jpg"]).await;
    let (s, v) = call(post(
        "/v1/verify",
        &json!({ "image": img("biden_2021.jpg"), "templates": [obama, biden], "model": MODEL }),
    ))
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["match"], true, "{v}");
}

#[tokio::test]
async fn model_mismatch_never_matches() {
    let t = enrol(&["biden_2013.jpg"]).await;
    let (s, v) = call(post(
        "/v1/verify",
        &json!({ "image": img("biden_2013.jpg"), "templates": [t], "model": "sface_2021dec@0" }),
    ))
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["match"], false);
    assert_eq!(v["reason"], "model_mismatch");
}

#[tokio::test]
async fn no_face_and_multiple_faces_are_reported() {
    let t = enrol(&["biden_2013.jpg"]).await;
    let blank = RgbImage::from_pixel(400, 300, image::Rgb([200, 200, 200]));
    let v = verify(png(&blank), &t).await;
    assert_eq!(
        (v["match"].clone(), v["reason"].clone(), v["faces"].clone()),
        (json!(false), json!("no_face"), json!(0))
    );

    // Two portraits side by side.
    let (a, b) = (rgb("biden_2013.jpg"), rgb("obama_2012.jpg"));
    let mut pair = RgbImage::new(a.width() + b.width(), a.height().max(b.height()));
    imageops::replace(&mut pair, &a, 0, 0);
    imageops::replace(&mut pair, &b, a.width() as i64, 0);
    let v = verify(png(&pair), &t).await;
    assert_eq!(v["match"], false);
    assert_eq!(v["reason"], "multiple_faces", "{v}");
    assert_eq!(v["faces"], 2);
}

#[tokio::test]
async fn small_and_blurry_faces_are_rejected() {
    let t = enrol(&["biden_2013.jpg"]).await;
    let full = rgb("biden_2021.jpg");
    // Face box is ~150 px wide at 500x625; at 1/3 scale it is ~50 px.
    let small = imageops::resize(&full, 167, 208, imageops::FilterType::Triangle);
    let v = verify(png(&small), &t).await;
    assert_eq!(v["reason"], "too_small", "{v}");
    assert_eq!(v["match"], false);

    let blurry = imageops::blur(&full, 4.0);
    let v = verify(png(&blurry), &t).await;
    assert_eq!(v["reason"], "blurry", "{v}");
    assert_eq!(v["match"], false);
}

// ------------------------------------------------------------------ liveness

#[tokio::test]
async fn spoof_samples_score_low_and_never_match() {
    // minivision samples: T1 is a live capture, F1/F2 are spoofs (F2 shows the
    // same person as T1 on a screen/print).
    let t = enrol(&["minivision_image_T1.jpg"]).await;
    let live = verify(img("minivision_image_T1.jpg"), &t).await;
    assert_eq!(live["live"], true, "{live}");
    let live_score = live["live_score"].as_f64().unwrap();
    for spoof in ["minivision_image_F1.jpg", "minivision_image_F2.jpg"] {
        let v = verify(img(spoof), &t).await;
        assert_eq!(v["live"], false, "{spoof}: {v}");
        assert_eq!(v["match"], false, "{spoof}: {v}");
        assert_eq!(v["reason"], "not_live", "{spoof}: {v}");
        assert!(
            v["live_score"].as_f64().unwrap() < live_score - 0.5,
            "{spoof}: {v}"
        );
    }
    // The F2 spoof is the enrolled person: identity alone would have matched.
    let v = verify(img("minivision_image_F2.jpg"), &t).await;
    assert!(v["score"].as_f64().unwrap() >= 0.363, "{v}");
}

#[tokio::test]
async fn liveness_can_be_made_advisory() {
    let mut cfg = Config::with_token(TOKEN);
    cfg.require_liveness = false;
    let (s, v) = call_with(
        cfg,
        post(
            "/v1/embed",
            &json!({ "images": [img("minivision_image_F2.jpg")] }),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["frames"][0]["accepted"], true);
    assert!(v["frames"][0]["live_score"].as_f64().unwrap() < 0.5);
}

// ------------------------------------------------------------------ decoding

#[test]
fn exif_orientation_is_applied() {
    // The minivision samples are stored 640x480 with an EXIF rotation tag;
    // upright they are 480 wide and 640 tall.
    let raw = image::load_from_memory(&image_bytes("minivision_image_T1.jpg")).unwrap();
    assert_eq!((raw.width(), raw.height()), (640, 480));
    let upright = rgb("minivision_image_T1.jpg");
    assert_eq!(upright.dimensions(), (480, 640));
}
