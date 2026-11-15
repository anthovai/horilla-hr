//! HTTP front for the payroll engine. Stateless: the rules are loaded once at
//! start (`RULES_DIR`: every *.json in a directory; `RULES_FILE`: one file; else
//! every bundled Thai rule set, see `BUNDLED_RULES`) and every request is a pure calculation, so it
//! scales out and tests without a database. Each request may carry `as_of`
//! (YYYY-MM-DD, default today UTC) to pick the rule set in force on that date.

use std::sync::Arc;

use axum::{
    extract::State,
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use payroll_engine::{
    annual_tax, monthly_withholding, social_security, AnnualTax, Claim, EngineError, RuleBook,
    Rules, Satang, Sso, Withholding, WithholdingInput,
};
use serde::Deserialize;

type Shared = Arc<RuleBook>;
type ApiResult<T> = Result<Json<T>, (StatusCode, Json<serde_json::Value>)>;

fn fail(e: EngineError) -> (StatusCode, Json<serde_json::Value>) {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(serde_json::json!({ "error": e.to_string() })),
    )
}

#[derive(Deserialize)]
struct AnnualReq {
    as_of: Option<String>,
    gross_income: Satang,
    #[serde(default)]
    claims: Vec<Claim>,
    #[serde(default)]
    sso_paid_year: Satang,
}

#[derive(Deserialize)]
struct SsoReq {
    as_of: Option<String>,
    wage: Satang,
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "service": "payroll-engine" }))
}

#[derive(Deserialize)]
struct WithholdingReq {
    as_of: Option<String>,
    #[serde(flatten)]
    input: WithholdingInput,
}

async fn rules_info(State(book): State<Shared>) -> Json<serde_json::Value> {
    let sets: Vec<_> = book
        .sets()
        .iter()
        .map(|r| serde_json::json!({ "country": r.country, "year": r.year, "effective_from": r.effective_from }))
        .collect();
    // Top-level fields: the set in force today, else the earliest loaded.
    let current = book.select(None).unwrap_or(&book.sets()[0]);
    Json(serde_json::json!({
        "country": current.country, "year": current.year, "effective_from": current.effective_from,
        "sets": sets,
    }))
}

async fn annual(State(b): State<Shared>, Json(q): Json<AnnualReq>) -> ApiResult<AnnualTax> {
    let r = b.select(q.as_of.as_deref()).map_err(fail)?;
    annual_tax(r, q.gross_income, &q.claims, q.sso_paid_year)
        .map(Json)
        .map_err(fail)
}

async fn sso(State(b): State<Shared>, Json(q): Json<SsoReq>) -> ApiResult<Sso> {
    let r = b.select(q.as_of.as_deref()).map_err(fail)?;
    social_security(r, q.wage).map(Json).map_err(fail)
}

async fn withholding(
    State(b): State<Shared>,
    Json(q): Json<WithholdingReq>,
) -> ApiResult<Withholding> {
    let r = b.select(q.as_of.as_deref()).map_err(fail)?;
    monthly_withholding(r, &q.input).map(Json).map_err(fail)
}

pub fn app_with_book(book: RuleBook) -> Router {
    Router::new()
        .route("/healthz", get(health))
        .route("/v1/rules", get(rules_info))
        .route("/v1/tax/annual", post(annual))
        .route("/v1/sso", post(sso))
        .route("/v1/withholding/monthly", post(withholding))
        .with_state(Arc::new(book))
}

pub fn app(rules: Rules) -> Router {
    app_with_book(RuleBook::single(rules))
}

#[tokio::main]
async fn main() {
    let book = match (std::env::var("RULES_DIR"), std::env::var("RULES_FILE")) {
        (Ok(_), Ok(_)) => panic!("set only one of RULES_DIR and RULES_FILE"),
        (Ok(dir), _) => {
            RuleBook::from_dir(std::path::Path::new(&dir)).unwrap_or_else(|e| panic!("{dir}: {e}"))
        }
        (_, Ok(path)) => {
            let text = std::fs::read_to_string(&path)
                .unwrap_or_else(|e| panic!("cannot read {path}: {e}"));
            RuleBook::single(Rules::from_json(&text).unwrap_or_else(|e| panic!("{path}: {e}")))
        }
        _ => RuleBook::bundled(),
    };
    // Loopback by default: this service is called by the app tier, not the internet.
    let addr = std::env::var("BIND").unwrap_or_else(|_| "127.0.0.1:8090".into());
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .expect("bind failed");
    eprintln!(
        "payroll-engine listening on {addr} ({} rule set(s))",
        book.sets().len()
    );
    axum::serve(listener, app_with_book(book))
        .await
        .expect("server error");
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
        assert_eq!(v["employee"], 87_500);
    }

    #[tokio::test]
    async fn annual_endpoint_returns_the_breakdown() {
        let (s, v) = post_json(
            "/v1/tax/annual",
            serde_json::json!({ "gross_income": 60_000_000 }),
        )
        .await;
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

    async fn call(book: RuleBook, req: Request<Body>) -> (StatusCode, serde_json::Value) {
        let res = app_with_book(book).oneshot(req).await.unwrap();
        let status = res.status();
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap())
    }

    fn two_year_book() -> RuleBook {
        let mut sets = vec![Rules::th_2026()];
        let mut v: serde_json::Value = serde_json::from_str(payroll_engine::TH_2026_RULES).unwrap();
        v["effective_from"] = serde_json::json!("2027-01-01");
        v["year"] = serde_json::json!(2027);
        v["social_security"]["base_max"] = serde_json::json!(1_700_000);
        sets.push(Rules::from_json(&v.to_string()).unwrap());
        RuleBook::from_rules(sets).unwrap()
    }

    fn post_req(path: &str, body: serde_json::Value) -> Request<Body> {
        Request::post(path)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap()
    }

    #[tokio::test]
    async fn as_of_selects_the_rule_set() {
        let q = |d: &str| {
            post_req(
                "/v1/sso",
                serde_json::json!({ "wage": 3_000_000, "as_of": d }),
            )
        };
        let (s, v) = call(two_year_book(), q("2026-06-01")).await;
        assert_eq!(
            (s, &v["employee"]),
            (StatusCode::OK, &serde_json::json!(87_500))
        );
        let (s, v) = call(two_year_book(), q("2027-06-01")).await;
        assert_eq!(
            (s, &v["employee"]),
            (StatusCode::OK, &serde_json::json!(85_000))
        );
    }

    #[tokio::test]
    async fn as_of_before_any_rule_set_is_422() {
        let (s, v) = post_json(
            "/v1/sso",
            serde_json::json!({ "wage": 1, "as_of": "2020-01-01" }),
        )
        .await;
        assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
        assert!(v["error"].as_str().unwrap().contains("2020-01-01"));
    }

    #[tokio::test]
    async fn invalid_as_of_is_422_on_every_endpoint() {
        for (path, body) in [
            (
                "/v1/sso",
                serde_json::json!({ "wage": 1, "as_of": "2026-02-30" }),
            ),
            (
                "/v1/tax/annual",
                serde_json::json!({ "gross_income": 1, "as_of": "yesterday" }),
            ),
            (
                "/v1/withholding/monthly",
                serde_json::json!({ "month": 1, "salary": 1, "as_of": "2026-1-1" }),
            ),
        ] {
            let (s, _) = post_json(path, body).await;
            assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY, "{path}");
        }
    }

    #[tokio::test]
    async fn withholding_accepts_as_of_alongside_its_own_fields() {
        let (s, v) = post_json(
            "/v1/withholding/monthly",
            serde_json::json!({ "month": 1, "salary": 5_000_000, "as_of": "2026-03-01" }),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        assert!(v["withhold_this_month"].as_i64().unwrap() > 0);
    }

    #[tokio::test]
    async fn rules_endpoint_lists_sets_and_keeps_old_fields() {
        let (s, v) = call(
            two_year_book(),
            Request::get("/v1/rules").body(Body::empty()).unwrap(),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["country"], "TH");
        assert!(v["year"].is_number() && v["effective_from"].is_string());
        let sets = v["sets"].as_array().unwrap();
        assert_eq!(sets.len(), 2);
        assert_eq!(sets[0]["effective_from"], "2026-01-01");
        assert_eq!(sets[1]["year"], 2027);
    }

    #[tokio::test]
    async fn bundled_book_selects_the_sso_ceiling_by_as_of() {
        for (d, employee) in [
            ("2028-12-31", 87_500),
            ("2029-01-01", 100_000),
            ("2032-06-01", 115_000),
        ] {
            let (s, v) = call(
                RuleBook::bundled(),
                post_req(
                    "/v1/sso",
                    serde_json::json!({ "wage": 10_000_000, "as_of": d }),
                ),
            )
            .await;
            assert_eq!(s, StatusCode::OK, "{d}");
            assert_eq!(v["employee"], employee, "{d}");
        }
    }

    #[tokio::test]
    async fn rules_endpoint_lists_every_bundled_set() {
        let (s, v) = call(
            RuleBook::bundled(),
            Request::get("/v1/rules").body(Body::empty()).unwrap(),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        let dates: Vec<&str> = v["sets"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["effective_from"].as_str().unwrap())
            .collect();
        assert_eq!(dates, ["2026-01-01", "2029-01-01", "2032-01-01"]);
    }

    #[tokio::test]
    async fn annual_endpoint_reports_group_trimming() {
        let (s, v) = post_json(
            "/v1/tax/annual",
            serde_json::json!({ "gross_income": 200_000_000, "claims": [
                { "code": "life_insurance", "amount": 10_000_000 },
                { "code": "health_insurance", "amount": 2_500_000 } ] }),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["group_adjustments"][0]["group"], "insurance_combined");
        assert_eq!(v["group_adjustments"][0]["trimmed"], 2_500_000);
    }

    #[tokio::test]
    async fn rules_dir_loads_every_json_file() {
        let dir = std::env::temp_dir().join(format!("payroll-rules-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("a.json"), payroll_engine::TH_2026_RULES).unwrap();
        let v2027 = payroll_engine::TH_2026_RULES.replace("2026-01-01", "2027-01-01");
        std::fs::write(dir.join("b.json"), v2027).unwrap();
        std::fs::write(dir.join("ignored.txt"), "x").unwrap();
        let book = RuleBook::from_dir(&dir).unwrap();
        std::fs::remove_dir_all(&dir).ok();
        assert_eq!(book.sets().len(), 2);
        assert_eq!(
            book.select(Some("2027-05-05")).unwrap().effective_from,
            "2027-01-01"
        );
    }
}
