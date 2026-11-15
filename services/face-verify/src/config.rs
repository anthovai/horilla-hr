//! Runtime settings, all from environment variables. Defaults and their
//! sources are documented per field and in models/MODELS.md.

use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Config {
    /// FACE_SERVICE_TOKEN: shared secret expected in `X-Service-Token`.
    pub token: String,
    /// FACE_MATCH_THRESHOLD: cosine similarity at or above which two faces
    /// match. Default 0.363, the SFace threshold published with the model
    /// (OpenCV Zoo `sface.py`, tuned on LFW).
    pub match_threshold: f32,
    /// FACE_REQUIRE_LIVENESS (default true). When false the liveness score is
    /// still computed and reported but no longer gates `match`/`accepted`.
    pub require_liveness: bool,
    /// FACE_LIVENESS_THRESHOLD: mean "real face" probability of the two
    /// MiniFASNet models needed to count as live. Default 0.5.
    pub liveness_threshold: f32,
    /// FACE_MIN_FACE_PX: shorter side of the face box, in working-image pixels.
    pub min_face_px: f32,
    /// FACE_BLUR_MIN: minimum variance of the Laplacian on the aligned crop.
    pub blur_min: f64,
    /// FACE_DET_SCORE: detector confidence threshold.
    pub det_score: f32,
    /// FACE_MAX_BODY_BYTES: request body limit.
    pub max_body_bytes: usize,
    /// FACE_MAX_PIXELS: largest decoded image (width x height) accepted.
    pub max_pixels: u64,
    /// FACE_REQUEST_TIMEOUT_SECS.
    pub request_timeout: Duration,
    /// FACE_WORKERS: concurrent inference jobs (default: CPU count).
    pub workers: usize,
    /// FACE_MAX_TEMPLATES: templates accepted per verify call.
    pub max_templates: usize,
}

pub const MIN_TOKEN_LEN: usize = 16;

impl Config {
    /// Defaults with the given token; used by tests and as the base for env.
    pub fn with_token(token: impl Into<String>) -> Self {
        Config {
            token: token.into(),
            match_threshold: 0.363,
            require_liveness: true,
            liveness_threshold: 0.5,
            min_face_px: 80.0,
            blur_min: 60.0,
            det_score: 0.6,
            max_body_bytes: 4 * 1024 * 1024,
            max_pixels: 16_000_000,
            request_timeout: Duration::from_secs(15),
            workers: std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(2),
            max_templates: 32,
        }
    }

    pub fn from_env() -> Result<Self, String> {
        let token = std::env::var("FACE_SERVICE_TOKEN").unwrap_or_default();
        if token.len() < MIN_TOKEN_LEN {
            return Err(format!(
                "FACE_SERVICE_TOKEN must be set (at least {MIN_TOKEN_LEN} characters)"
            ));
        }
        let mut c = Config::with_token(token);
        c.match_threshold = env_parse("FACE_MATCH_THRESHOLD", c.match_threshold)?;
        c.require_liveness = env_bool("FACE_REQUIRE_LIVENESS", c.require_liveness)?;
        c.liveness_threshold = env_parse("FACE_LIVENESS_THRESHOLD", c.liveness_threshold)?;
        c.min_face_px = env_parse("FACE_MIN_FACE_PX", c.min_face_px)?;
        c.blur_min = env_parse("FACE_BLUR_MIN", c.blur_min)?;
        c.det_score = env_parse("FACE_DET_SCORE", c.det_score)?;
        c.max_body_bytes = env_parse("FACE_MAX_BODY_BYTES", c.max_body_bytes)?;
        c.max_pixels = env_parse("FACE_MAX_PIXELS", c.max_pixels)?;
        c.request_timeout = Duration::from_secs(env_parse(
            "FACE_REQUEST_TIMEOUT_SECS",
            c.request_timeout.as_secs(),
        )?);
        c.workers = env_parse("FACE_WORKERS", c.workers)?.max(1);
        c.max_templates = env_parse("FACE_MAX_TEMPLATES", c.max_templates)?.max(1);
        if !(-1.0..=1.0).contains(&c.match_threshold) {
            return Err("FACE_MATCH_THRESHOLD must be within [-1, 1]".into());
        }
        if !(0.0..=1.0).contains(&c.liveness_threshold) || !(0.0..=1.0).contains(&c.det_score) {
            return Err("FACE_LIVENESS_THRESHOLD and FACE_DET_SCORE must be within [0, 1]".into());
        }
        Ok(c)
    }
}

fn env_parse<T: std::str::FromStr>(name: &str, default: T) -> Result<T, String> {
    match std::env::var(name) {
        Ok(v) if !v.trim().is_empty() => v
            .trim()
            .parse()
            .map_err(|_| format!("{name}: cannot parse {v:?}")),
        _ => Ok(default),
    }
}

fn env_bool(name: &str, default: bool) -> Result<bool, String> {
    match std::env::var(name) {
        Ok(v) => match v.trim().to_ascii_lowercase().as_str() {
            "" => Ok(default),
            "1" | "true" | "yes" | "on" => Ok(true),
            "0" | "false" | "no" | "off" => Ok(false),
            _ => Err(format!("{name}: expected true/false, got {v:?}")),
        },
        Err(_) => Ok(default),
    }
}
