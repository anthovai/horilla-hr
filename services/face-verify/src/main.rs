//! HTTP front for the face verification engine. Loads the models once, then
//! every request is a pure computation: nothing is stored or logged beyond
//! method, path, status and latency.

use std::sync::Arc;

use face_verify::{app, models_dir, AppState, Config, Engine};

#[tokio::main]
async fn main() {
    let cfg = Config::from_env().unwrap_or_else(|e| {
        eprintln!("face-verify: {e}");
        std::process::exit(2);
    });
    let dir = models_dir();
    let engine = Engine::load(&dir).unwrap_or_else(|e| {
        eprintln!(
            "face-verify: cannot load models from {}: {e}",
            dir.display()
        );
        std::process::exit(2);
    });
    // Loopback by default: only the Django tier calls this service.
    let addr = std::env::var("FACE_BIND").unwrap_or_else(|_| "127.0.0.1:8092".into());
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .unwrap_or_else(|e| {
            eprintln!("face-verify: bind {addr}: {e}");
            std::process::exit(2);
        });
    eprintln!(
        "face-verify listening on {addr} (workers {}, liveness required: {}, match threshold {})",
        cfg.workers, cfg.require_liveness, cfg.match_threshold
    );
    axum::serve(listener, app(AppState::new(Arc::new(engine), cfg)))
        .with_graceful_shutdown(shutdown())
        .await
        .expect("server error");
}

async fn shutdown() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! { _ = ctrl_c => {}, _ = term => {} }
}
