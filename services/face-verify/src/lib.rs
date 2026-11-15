//! OrvaHR face verification service: stateless, internal-only.
//! Contract: services/face-verify/API.md. Models and licences: models/MODELS.md.

pub mod align;
pub mod api;
pub mod config;
pub mod detect;
pub mod engine;
pub mod imaging;

pub use api::{app, AppState};
pub use config::Config;
pub use engine::Engine;

/// Where the ONNX files live: FACE_MODELS_DIR, else ./models.
pub fn models_dir() -> std::path::PathBuf {
    std::env::var_os("FACE_MODELS_DIR")
        .map(Into::into)
        .unwrap_or_else(|| "models".into())
}
