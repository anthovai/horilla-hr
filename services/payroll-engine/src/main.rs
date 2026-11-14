//! HTTP front for the payroll engine. Stateless: the rules are loaded once at
//! start (a file given by `RULES_FILE`, else the bundled Thai rules) and every
//! request is a pure calculation, so it scales out and tests without a database.

use std::sync::Arc;

use axum::{
    extract::State,
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use payroll_engine::{
    annual_tax, monthly_withholding, social_security, AnnualTax, Claim, EngineError, Rules, Satang,
    Sso, Withholding, WithholdingInput,
};
use serde::Deserialize;

type Shared = Arc<Rules>;
type ApiResult<T> = Result<Json<T>, (StatusCode, Json<serde_json::Value>)>;

fn fail(e: EngineError) -> (StatusCode, Json<serde_json::Value>) {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(serde_json::json!({ "error": e.to_string() })),
    )
}

#[derive(Deserialize)]
struct AnnualReq {
    gross_income: Satang,
    #[serde(default)]
    claims: Vec<Claim>,
    #[serde(default)]
    sso_paid_year: Satang,
}

#[derive(Deserialize)]
struct SsoReq {
    wage: Satang,
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "service": "payroll-engine" }))
}

async fn rules_info(State(r): State<Shared>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "country": r.country, "year": r.year, "effective_from": r.effective_from,
    }))
}

async fn annual(State(r): State<Shared>, Json(q): Json<AnnualReq>) -> ApiResult<AnnualTax> {
    annual_tax(&r, q.gross_income, &q.claims, q.sso_paid_year).map(Json).map_err(fail)
}

async fn sso(State(r): State<Shared>, Json(q): Json<SsoReq>) -> ApiResult<Sso> {
    social_security(&r, q.wage).map(Json).map_err(fail)
}

async fn withholding(State(r): State<Shared>, Json(q): Json<WithholdingInput>) -> ApiResult<Withholding> {
    monthly_withholding(&r, &q).map(Json).map_err(fail)
}

pub fn app(rules: Rules) -> Router {
    Router::new()
        .route("/healthz", get(health))
        .route("/v1/rules", get(rules_info))
        .route("/v1/tax/annual", post(annual))
        .route("/v1/sso", post(sso))
        .route("/v1/withholding/monthly", post(withholding))
        .with_state(Arc::new(rules))
}

#[tokio::main]
async fn main() {
    let rules = match std::env::var("RULES_FILE") {
        Ok(path) => {
            let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("cannot read {path}: {e}"));
            Rules::from_json(&text).unwrap_or_else(|e| panic!("{path}: {e}"))
        }
        Err(_) => Rules::th_2026(),
    };
    // Loopback by default: this service is called by the app tier, not the internet.
    let addr = std::env::var("BIND").unwrap_or_else(|_| "127.0.0.1:8090".into());
    let listener = tokio::net::TcpListener::bind(&addr).await.expect("bind failed");
    eprintln!("payroll-engine listening on {addr} (rules {})", rules.year);
    axum::serve(listener, app(rules)).await.expect("server error");
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::Body, http::Request};
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    async fn post_json(path: &str, body: serde_json::Value) -> (StatusCode, serde_json::Value) {
        let res = app(Rules::th_2026())
            .oneshot(
                Request::post(path)
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = res.status();
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap())
    }

    #[tokio::test]
    async fn sso_endpoint_applies_the_ceiling() {
        let (s, v) = post_json("/v1/sso", serde_json::json!({ "wage": 3_000_000 })).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["employee"], 75_000);
    }

    #[tokio::test]
    async fn annual_endpoint_returns_the_breakdown() {
        let (s, v) = post_json("/v1/tax/annual", serde_json::json!({ "gross_income": 60_000_000 })).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["tax"], 2_150_000);
    }

    #[tokio::test]
    async fn unknown_allowance_is_a_422_not_a_silent_zero() {
        let (s, v) = post_json(
            "/v1/tax/annual",
            serde_json::json!({ "gross_income": 1, "claims": [{ "code": "yacht", "units": 1 }] }),
        )
        .await;
        assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
        assert!(v["error"].as_str().unwrap().contains("yacht"));
    }
}
