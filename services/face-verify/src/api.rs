//! HTTP surface, per services/face-verify/API.md.
//!
//! Nothing here stores or logs request bodies, images or embeddings: the
//! access log line is method, path, status and latency only.

use std::sync::Arc;
use std::time::Instant;

use axum::{
    body::Bytes,
    extract::{rejection::BytesRejection, DefaultBodyLimit, Request, State},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use tokio::sync::Semaphore;
use tower_http::timeout::TimeoutLayer;

use crate::config::Config;
use crate::engine::{self, Analysis, Engine, Gates, DIMS, MODEL_ID};
use crate::imaging::{self, ImageError};

pub const MAX_EMBED_FRAMES: usize = 5;

#[derive(Clone)]
pub struct AppState {
    engine: Arc<Engine>,
    cfg: Arc<Config>,
    /// Bounds concurrent CPU-heavy work to FACE_WORKERS; excess requests
    /// queue here (and hit the request timeout) instead of thrashing.
    jobs: Arc<Semaphore>,
    /// SHA-256 of the token: comparing fixed-length digests in constant time
    /// leaks neither the content nor the length of the secret.
    token_digest: [u8; 32],
}

impl AppState {
    pub fn new(engine: Arc<Engine>, cfg: Config) -> Self {
        let token_digest = Sha256::digest(cfg.token.as_bytes()).into();
        AppState {
            engine,
            jobs: Arc::new(Semaphore::new(cfg.workers)),
            cfg: Arc::new(cfg),
            token_digest,
        }
    }

    fn gates(&self) -> Gates {
        Gates {
            det_score: self.cfg.det_score,
            min_face_px: self.cfg.min_face_px,
            blur_min: self.cfg.blur_min,
            liveness_threshold: self.cfg.liveness_threshold,
            require_liveness: self.cfg.require_liveness,
        }
    }
}

type ApiError = (StatusCode, Json<serde_json::Value>);

fn err(status: StatusCode, msg: impl Into<String>) -> ApiError {
    (status, Json(json!({ "error": msg.into() })))
}

fn body_rejection(r: BytesRejection) -> ApiError {
    let status = r.status();
    let msg = if status == StatusCode::PAYLOAD_TOO_LARGE {
        "request body too large".to_string()
    } else {
        r.body_text()
    };
    err(status, msg)
}

fn image_error(e: ImageError) -> ApiError {
    match e {
        ImageError::TooLarge { .. } => err(StatusCode::PAYLOAD_TOO_LARGE, e.to_string()),
        ImageError::Invalid(_) => err(StatusCode::BAD_REQUEST, e.to_string()),
    }
}

fn parse<T: for<'de> Deserialize<'de>>(body: &[u8]) -> Result<T, ApiError> {
    serde_json::from_slice(body)
        .map_err(|e| err(StatusCode::BAD_REQUEST, format!("invalid JSON body: {e}")))
}

async fn auth(
    State(st): State<AppState>,
    headers: HeaderMap,
    req: Request,
    next: Next,
) -> Response {
    let given = headers
        .get("x-service-token")
        .map(|v| v.as_bytes())
        .unwrap_or(b"");
    let digest: [u8; 32] = Sha256::digest(given).into();
    if bool::from(digest.ct_eq(&st.token_digest)) && !given.is_empty() {
        next.run(req).await
    } else {
        err(
            StatusCode::UNAUTHORIZED,
            "missing or invalid X-Service-Token",
        )
        .into_response()
    }
}

async fn access_log(req: Request, next: Next) -> Response {
    let (method, path) = (req.method().clone(), req.uri().path().to_owned());
    let t0 = Instant::now();
    let res = next.run(req).await;
    eprintln!(
        "{method} {path} {} {}ms",
        res.status().as_u16(),
        t0.elapsed().as_millis()
    );
    res
}

/// Runs `f` on the blocking pool while holding a worker permit.
async fn run_job<T: Send + 'static>(
    st: &AppState,
    f: impl FnOnce(&Engine, &Gates) -> Result<T, ApiError> + Send + 'static,
) -> Result<T, ApiError> {
    let permit = st
        .jobs
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| err(StatusCode::SERVICE_UNAVAILABLE, "shutting down"))?;
    let engine = st.engine.clone();
    let gates = st.gates();
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        f(&engine, &gates)
    })
    .await
    .map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "worker failed"))?
}

fn internal(e: String) -> ApiError {
    // Model/runtime failure: no request data in the message.
    eprintln!("inference error: {e}");
    err(StatusCode::INTERNAL_SERVER_ERROR, "inference failed")
}

// ---------------------------------------------------------------- /healthz

async fn health() -> Json<serde_json::Value> {
    Json(json!({
        "status": "ok",
        "models": {
            "detector": engine::DETECTOR_ID,
            "recognizer": MODEL_ID,
            "liveness": engine::LIVENESS_ID,
        }
    }))
}

// ---------------------------------------------------------------- /v1/embed

#[derive(Deserialize)]
struct EmbedReq {
    images: Vec<String>,
}

#[derive(Serialize)]
struct Frame {
    faces: usize,
    quality: f32,
    live_score: f32,
    accepted: bool,
    reason: Option<&'static str>,
}

fn frame(a: &Analysis) -> Frame {
    Frame {
        faces: a.faces,
        quality: round4(a.quality),
        live_score: round4(a.live_score),
        accepted: a.reason.is_none(),
        reason: a.reason.map(|r| r.as_str()),
    }
}

fn round4(x: f32) -> f32 {
    (x * 10_000.0).round() / 10_000.0
}

async fn embed(State(st): State<AppState>, body: Result<Bytes, BytesRejection>) -> Response {
    match embed_inner(st, body).await {
        Ok(r) => r,
        Err(e) => e.into_response(),
    }
}

async fn embed_inner(
    st: AppState,
    body: Result<Bytes, BytesRejection>,
) -> Result<Response, ApiError> {
    let body = body.map_err(body_rejection)?;
    let req: EmbedReq = parse(&body)?;
    if req.images.is_empty() || req.images.len() > MAX_EMBED_FRAMES {
        return Err(err(
            StatusCode::BAD_REQUEST,
            format!("images: expected 1..={MAX_EMBED_FRAMES} frames"),
        ));
    }
    let max_pixels = st.cfg.max_pixels;
    let images = req.images;
    let (frames, embeddings) = run_job(&st, move |engine, gates| {
        let mut frames = Vec::with_capacity(images.len());
        let mut embeddings = Vec::new();
        for b64 in &images {
            let bytes = imaging::decode_base64(b64).map_err(image_error)?;
            let img = imaging::decode_image(&bytes, max_pixels).map_err(image_error)?;
            let a = engine.analyse(img, gates).map_err(internal)?;
            if a.reason.is_none() {
                if let Some(e) = &a.embedding {
                    embeddings.push(e.clone());
                }
            }
            frames.push(frame(&a));
        }
        Ok((frames, embeddings))
    })
    .await?;
    match crate::align::mean_template(&embeddings) {
        Some(template) => Ok(Json(json!({
            "template": template,
            "dims": DIMS,
            "model": MODEL_ID,
            "frames": frames,
        }))
        .into_response()),
        None => Ok((
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "no frame accepted",
                "template": null,
                "dims": DIMS,
                "model": MODEL_ID,
                "frames": frames,
            })),
        )
            .into_response()),
    }
}

// ---------------------------------------------------------------- /v1/verify

#[derive(Deserialize)]
struct VerifyReq {
    image: String,
    templates: Vec<Vec<f32>>,
    model: String,
}

#[derive(Serialize)]
struct VerifyResp {
    #[serde(rename = "match")]
    is_match: bool,
    score: f32,
    threshold: f32,
    live: bool,
    live_score: f32,
    faces: usize,
    reason: Option<&'static str>,
}

async fn verify(State(st): State<AppState>, body: Result<Bytes, BytesRejection>) -> Response {
    match verify_inner(st, body).await {
        Ok(r) => Json(r).into_response(),
        Err(e) => e.into_response(),
    }
}

async fn verify_inner(
    st: AppState,
    body: Result<Bytes, BytesRejection>,
) -> Result<VerifyResp, ApiError> {
    let body = body.map_err(body_rejection)?;
    let req: VerifyReq = parse(&body)?;
    let threshold = st.cfg.match_threshold;
    if req.model != MODEL_ID {
        // Templates from another model/pipeline are not comparable; the image
        // is not analysed, hence faces = 0.
        return Ok(VerifyResp {
            is_match: false,
            score: 0.0,
            threshold,
            live: false,
            live_score: 0.0,
            faces: 0,
            reason: Some("model_mismatch"),
        });
    }
    if req.templates.is_empty() || req.templates.len() > st.cfg.max_templates {
        return Err(err(
            StatusCode::BAD_REQUEST,
            format!("templates: expected 1..={} templates", st.cfg.max_templates),
        ));
    }
    let mut templates = req.templates;
    for t in templates.iter_mut() {
        if t.len() != DIMS || !crate::align::l2_normalize(t) {
            return Err(err(
                StatusCode::BAD_REQUEST,
                format!("templates: each must be {DIMS} finite, non-zero floats"),
            ));
        }
    }
    let max_pixels = st.cfg.max_pixels;
    let image = req.image;
    let a = run_job(&st, move |engine, gates| {
        let bytes = imaging::decode_base64(&image).map_err(image_error)?;
        let img = imaging::decode_image(&bytes, max_pixels).map_err(image_error)?;
        engine.analyse(img, gates).map_err(internal)
    })
    .await?;
    let score = a
        .embedding
        .as_ref()
        .map(|e| {
            templates
                .iter()
                .map(|t| crate::align::cosine(e, t))
                .fold(f32::NEG_INFINITY, f32::max)
        })
        .unwrap_or(0.0);
    Ok(VerifyResp {
        is_match: a.reason.is_none() && a.embedding.is_some() && score >= threshold,
        score: round4(score),
        threshold,
        live: a.live,
        live_score: round4(a.live_score),
        faces: a.faces,
        reason: a.reason.map(|r| r.as_str()),
    })
}

// ---------------------------------------------------------------- router

pub fn app(state: AppState) -> Router {
    let limit = state.cfg.max_body_bytes;
    let timeout = state.cfg.request_timeout;
    Router::new()
        .route("/healthz", get(health))
        .route("/v1/embed", post(embed))
        .route("/v1/verify", post(verify))
        .layer(DefaultBodyLimit::max(limit))
        .layer(middleware::from_fn_with_state(state.clone(), auth))
        .layer(TimeoutLayer::with_status_code(
            StatusCode::REQUEST_TIMEOUT,
            timeout,
        ))
        .layer(middleware::from_fn(access_log))
        .with_state(state)
}
