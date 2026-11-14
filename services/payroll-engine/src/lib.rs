//! OrvaHR Thai payroll rules engine.
//!
//! Design rules:
//! * Money is integer **satang** (1 baht = 100). No floats touch money.
//! * Rates are integer **basis points** (100 bp = 1%).
//! * Every rate, cap and bracket comes from a rules file (`rules/*.json`) that
//!   carries an effective date. Nothing statutory is hard-coded here, because
//!   the figures change by announcement and this engine must not be the
//!   authority on them.
//! * Rounding is half-up and happens once, at the point a figure becomes a
//!   payable amount.

use serde::{Deserialize, Serialize};

pub type Satang = i64;

pub const TH_2026_RULES: &str = include_str!("../rules/th-2026.json");

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub enum EngineError {
    UnknownAllowance(String),
    InvalidInput(String),
    BadRules(String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::UnknownAllowance(c) => write!(f, "unknown allowance code: {c}"),
            EngineError::InvalidInput(m) => write!(f, "invalid input: {m}"),
            EngineError::BadRules(m) => write!(f, "bad rules file: {m}"),
        }
    }
}
impl std::error::Error for EngineError {}

// ----------------------------------------------------------------- rules

#[derive(Debug, Clone, Deserialize)]
pub struct Rules {
    pub country: String,
    pub year: i32,
    pub effective_from: String,
    pub tax: TaxRules,
    pub social_security: SsoRules,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TaxRules {
    pub expense_rate_bp: i64,
    pub expense_cap: Satang,
    pub brackets: Vec<Bracket>,
    pub allowances: Vec<AllowanceRule>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Bracket {
    pub from: Satang,
    pub to: Option<Satang>,
    pub rate_bp: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AllowanceRule {
    pub code: String,
    #[serde(flatten)]
    pub kind: AllowanceKind,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AllowanceKind {
    /// Always applies (e.g. the personal allowance).
    Fixed { amount: Satang },
    /// `units` x `amount`, optionally limited to `max_units` (spouse, children).
    PerUnit { amount: Satang, max_units: Option<u32> },
    /// The declared amount, up to `cap` (insurance, funds).
    Capped { cap: Satang },
}

#[derive(Debug, Clone, Deserialize)]
pub struct SsoRules {
    pub rate_bp: i64,
    pub base_min: Satang,
    pub base_max: Satang,
    /// Round the contribution to a multiple of this many satang (100 = whole baht).
    pub rounding_satang: Satang,
    /// Most that can be deducted from taxable income for the year.
    pub employee_deduction_cap_per_year: Satang,
}

impl Rules {
    pub fn from_json(json: &str) -> Result<Rules, EngineError> {
        let rules: Rules =
            serde_json::from_str(json).map_err(|e| EngineError::BadRules(e.to_string()))?;
        rules.validate()?;
        Ok(rules)
    }

    pub fn th_2026() -> Rules {
        Rules::from_json(TH_2026_RULES).expect("bundled rules must be valid")
    }

    fn validate(&self) -> Result<(), EngineError> {
        let b = &self.tax.brackets;
        if b.is_empty() {
            return Err(EngineError::BadRules("no tax brackets".into()));
        }
        for pair in b.windows(2) {
            if pair[0].to != Some(pair[1].from) {
                return Err(EngineError::BadRules(
                    "tax brackets must be contiguous and ordered".into(),
                ));
            }
        }
        if b.last().and_then(|x| x.to).is_some() {
            return Err(EngineError::BadRules("top bracket must be open-ended".into()));
        }
        if self.social_security.base_min > self.social_security.base_max {
            return Err(EngineError::BadRules("sso base_min exceeds base_max".into()));
        }
        if self.social_security.rounding_satang <= 0 {
            return Err(EngineError::BadRules("sso rounding_satang must be positive".into()));
        }
        Ok(())
    }
}

// ------------------------------------------------------------- arithmetic

/// `amount * rate_bp / 10_000`, rounded half up. Amounts here are never negative.
fn apply_bp(amount: Satang, rate_bp: i64) -> Satang {
    ((amount as i128 * rate_bp as i128 + 5_000) / 10_000) as Satang
}

/// Round to the nearest multiple of `step`, half up.
fn round_to(amount: Satang, step: Satang) -> Satang {
    ((amount + step / 2) / step) * step
}

// -------------------------------------------------------------------- tax

#[derive(Debug, Clone, Deserialize, Default)]
pub struct Claim {
    pub code: String,
    /// For per-unit allowances (children, parents, spouse).
    pub units: Option<u32>,
    /// For capped allowances (insurance, funds), in satang.
    pub amount: Option<Satang>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AppliedAllowance {
    pub code: String,
    pub amount: Satang,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SlabRow {
    pub from: Satang,
    pub to: Satang,
    pub rate_bp: i64,
    pub tax: Satang,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AnnualTax {
    pub gross_income: Satang,
    pub expense_deduction: Satang,
    pub allowances: Vec<AppliedAllowance>,
    pub total_allowances: Satang,
    pub taxable_income: Satang,
    pub tax: Satang,
    pub slabs: Vec<SlabRow>,
}

/// Annual tax on `gross_income`, given the employee's claims and the social
/// security they paid (deductible up to the rules' yearly cap).
pub fn annual_tax(
    rules: &Rules,
    gross_income: Satang,
    claims: &[Claim],
    sso_paid_year: Satang,
) -> Result<AnnualTax, EngineError> {
    if gross_income < 0 || sso_paid_year < 0 {
        return Err(EngineError::InvalidInput("amounts cannot be negative".into()));
    }
    let t = &rules.tax;

    let expense_deduction = apply_bp(gross_income, t.expense_rate_bp).min(t.expense_cap);

    let mut applied: Vec<AppliedAllowance> = Vec::new();
    for rule in &t.allowances {
        let amount = match &rule.kind {
            AllowanceKind::Fixed { amount } => *amount,
            AllowanceKind::PerUnit { amount, max_units } => {
                let units = claims
                    .iter()
                    .filter(|c| c.code == rule.code)
                    .map(|c| c.units.unwrap_or(0))
                    .sum::<u32>();
                let units = max_units.map_or(units, |m| units.min(m));
                *amount * units as Satang
            }
            AllowanceKind::Capped { cap } => {
                let declared: Satang = claims
                    .iter()
                    .filter(|c| c.code == rule.code)
                    .map(|c| c.amount.unwrap_or(0))
                    .sum();
                if declared < 0 {
                    return Err(EngineError::InvalidInput("claim amount is negative".into()));
                }
                declared.min(*cap)
            }
        };
        if amount > 0 {
            applied.push(AppliedAllowance { code: rule.code.clone(), amount });
        }
    }
    for claim in claims {
        if !t.allowances.iter().any(|r| r.code == claim.code) {
            return Err(EngineError::UnknownAllowance(claim.code.clone()));
        }
    }

    let sso_deduction = sso_paid_year.min(rules.social_security.employee_deduction_cap_per_year);
    if sso_deduction > 0 {
        applied.push(AppliedAllowance { code: "social_security".into(), amount: sso_deduction });
    }

    let total_allowances: Satang = applied.iter().map(|a| a.amount).sum();
    let taxable_income = (gross_income - expense_deduction - total_allowances).max(0);

    let mut slabs = Vec::new();
    let mut tax: Satang = 0;
    for b in &t.brackets {
        if taxable_income <= b.from {
            break;
        }
        let upper = b.to.map_or(taxable_income, |to| to.min(taxable_income));
        let slab_tax = apply_bp(upper - b.from, b.rate_bp);
        tax += slab_tax;
        slabs.push(SlabRow { from: b.from, to: upper, rate_bp: b.rate_bp, tax: slab_tax });
    }

    Ok(AnnualTax {
        gross_income,
        expense_deduction,
        allowances: applied,
        total_allowances,
        taxable_income,
        tax,
        slabs,
    })
}

// -------------------------------------------------------- social security

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Sso {
    pub base: Satang,
    pub employee: Satang,
    pub employer: Satang,
}

/// Monthly social security on `wage`. The base is clamped to the rules' floor
/// and ceiling; the employer matches the employee.
pub fn social_security(rules: &Rules, wage: Satang) -> Result<Sso, EngineError> {
    if wage < 0 {
        return Err(EngineError::InvalidInput("wage cannot be negative".into()));
    }
    let s = &rules.social_security;
    let base = wage.clamp(s.base_min, s.base_max);
    let amount = round_to(apply_bp(base, s.rate_bp), s.rounding_satang);
    Ok(Sso { base, employee: amount, employer: amount })
}

// ------------------------------------------------- monthly withholding

#[derive(Debug, Clone, Deserialize)]
pub struct WithholdingInput {
    /// Pay month, 1..=12 (tax year assumed to be the calendar year).
    pub month: u32,
    /// Regular salary this month; assumed to continue for the remaining months.
    pub salary: Satang,
    /// One-off income this month (bonus). Not projected forward.
    #[serde(default)]
    pub other_income: Satang,
    /// Income paid in the months before this one.
    #[serde(default)]
    pub ytd_income: Satang,
    /// Tax already withheld in the months before this one.
    #[serde(default)]
    pub ytd_withheld: Satang,
    /// Employee social security already paid in the months before this one.
    #[serde(default)]
    pub ytd_sso: Satang,
    #[serde(default)]
    pub claims: Vec<Claim>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Withholding {
    pub withhold_this_month: Satang,
    pub projected_annual_income: Satang,
    pub sso: Sso,
    pub annual: AnnualTax,
}

/// Cumulative-method withholding: project the whole year, work out the year's
/// tax, subtract what has already been withheld and spread the rest over the
/// months that remain (this one included). The last month absorbs rounding.
pub fn monthly_withholding(
    rules: &Rules,
    input: &WithholdingInput,
) -> Result<Withholding, EngineError> {
    if !(1..=12).contains(&input.month) {
        return Err(EngineError::InvalidInput("month must be 1..=12".into()));
    }
    if [input.salary, input.other_income, input.ytd_income, input.ytd_withheld, input.ytd_sso]
        .iter()
        .any(|v| *v < 0)
    {
        return Err(EngineError::InvalidInput("amounts cannot be negative".into()));
    }

    let months_left_after = (12 - input.month) as Satang;
    let periods = months_left_after + 1;

    let sso = social_security(rules, input.salary)?;
    let projected_annual_income =
        input.ytd_income + input.salary + input.other_income + input.salary * months_left_after;
    let projected_sso = input.ytd_sso + sso.employee * periods;

    let annual = annual_tax(rules, projected_annual_income, &input.claims, projected_sso)?;

    let remaining = annual.tax - input.ytd_withheld;
    let withhold_this_month = if remaining <= 0 {
        0
    } else if periods == 1 {
        remaining
    } else {
        round_to_satang_div(remaining, periods)
    };

    Ok(Withholding { withhold_this_month, projected_annual_income, sso, annual })
}

/// `a / b` rounded half up, for non-negative `a` and positive `b`.
fn round_to_satang_div(a: Satang, b: Satang) -> Satang {
    (a * 2 + b) / (b * 2)
}

#[cfg(test)]
mod tests {
    use super::*;

    const BAHT: Satang = 100;

    fn rules() -> Rules {
        Rules::th_2026()
    }

    #[test]
    fn bundled_rules_are_valid() {
        let r = rules();
        assert_eq!(r.country, "TH");
        assert_eq!(r.tax.brackets.len(), 8);
    }

    #[test]
    fn annual_tax_single_person_600k() {
        // expense min(50% of 600k, 100k) = 100k; personal 60k -> taxable 440k.
        // 0-150k @0, 150-300k @5% = 7,500, 300-440k @10% = 14,000 -> 21,500.
        let t = annual_tax(&rules(), 600_000 * BAHT, &[], 0).unwrap();
        assert_eq!(t.expense_deduction, 100_000 * BAHT);
        assert_eq!(t.taxable_income, 440_000 * BAHT);
        assert_eq!(t.tax, 21_500 * BAHT);
    }

    #[test]
    fn income_under_threshold_pays_nothing() {
        let t = annual_tax(&rules(), 200_000 * BAHT, &[], 0).unwrap();
        assert_eq!(t.tax, 0);
    }

    #[test]
    fn expense_deduction_is_half_below_the_cap() {
        let t = annual_tax(&rules(), 120_000 * BAHT, &[], 0).unwrap();
        assert_eq!(t.expense_deduction, 60_000 * BAHT);
    }

    #[test]
    fn per_unit_allowances_respect_max_units() {
        let claims = vec![
            Claim { code: "spouse".into(), units: Some(3), amount: None },
            Claim { code: "child".into(), units: Some(2), amount: None },
        ];
        let t = annual_tax(&rules(), 1_000_000 * BAHT, &claims, 0).unwrap();
        let get = |c: &str| t.allowances.iter().find(|a| a.code == c).map(|a| a.amount);
        assert_eq!(get("spouse"), Some(60_000 * BAHT)); // capped at 1 unit
        assert_eq!(get("child"), Some(60_000 * BAHT));
    }

    #[test]
    fn capped_allowance_stops_at_cap() {
        let claims = vec![Claim { code: "life_insurance".into(), units: None, amount: Some(250_000 * BAHT) }];
        let t = annual_tax(&rules(), 1_000_000 * BAHT, &claims, 0).unwrap();
        let v = t.allowances.iter().find(|a| a.code == "life_insurance").unwrap();
        assert_eq!(v.amount, 100_000 * BAHT);
    }

    #[test]
    fn unknown_claim_code_is_rejected_not_ignored() {
        let claims = vec![Claim { code: "yacht".into(), units: Some(1), amount: None }];
        assert_eq!(
            annual_tax(&rules(), 500_000 * BAHT, &claims, 0),
            Err(EngineError::UnknownAllowance("yacht".into()))
        );
    }

    #[test]
    fn sso_is_clamped_to_floor_and_ceiling() {
        let r = rules();
        assert_eq!(social_security(&r, 30_000 * BAHT).unwrap().employee, 750 * BAHT);
        assert_eq!(social_security(&r, 15_000 * BAHT).unwrap().employee, 750 * BAHT);
        assert_eq!(social_security(&r, 10_000 * BAHT).unwrap().employee, 500 * BAHT);
        assert_eq!(social_security(&r, 1_000 * BAHT).unwrap().base, 1_650 * BAHT);
        assert_eq!(social_security(&r, 1_000 * BAHT).unwrap().employee, 83 * BAHT); // 82.50 -> 83
    }

    #[test]
    fn sso_employer_matches_employee() {
        let s = social_security(&rules(), 12_345 * BAHT).unwrap();
        assert_eq!(s.employee, s.employer);
    }

    #[test]
    fn withholding_for_a_steady_salary_sums_to_the_annual_tax() {
        let r = rules();
        let (mut ytd_income, mut ytd_withheld, mut ytd_sso) = (0, 0, 0);
        let salary = 50_000 * BAHT;
        for month in 1..=12 {
            let w = monthly_withholding(
                &r,
                &WithholdingInput {
                    month,
                    salary,
                    other_income: 0,
                    ytd_income,
                    ytd_withheld,
                    ytd_sso,
                    claims: vec![],
                },
            )
            .unwrap();
            ytd_income += salary;
            ytd_withheld += w.withhold_this_month;
            ytd_sso += w.sso.employee;
        }
        let expected = annual_tax(&r, ytd_income, &[], ytd_sso).unwrap().tax;
        assert_eq!(ytd_withheld, expected, "the year must reconcile exactly");
    }

    #[test]
    fn a_bonus_is_caught_up_in_the_month_it_is_paid() {
        let r = rules();
        let base = WithholdingInput {
            month: 3, salary: 50_000 * BAHT, other_income: 0,
            ytd_income: 100_000 * BAHT, ytd_withheld: 0, ytd_sso: 1_500 * BAHT, claims: vec![],
        };
        let with_bonus = WithholdingInput { other_income: 100_000 * BAHT, ..base.clone() };
        let a = monthly_withholding(&r, &base).unwrap().withhold_this_month;
        let b = monthly_withholding(&r, &with_bonus).unwrap().withhold_this_month;
        assert!(b > a);
    }

    #[test]
    fn invalid_month_is_rejected() {
        let r = rules();
        let input = WithholdingInput {
            month: 13, salary: 1, other_income: 0, ytd_income: 0, ytd_withheld: 0, ytd_sso: 0, claims: vec![],
        };
        assert!(matches!(monthly_withholding(&r, &input), Err(EngineError::InvalidInput(_))));
    }

    #[test]
    fn broken_bracket_chain_is_rejected() {
        let mut v: serde_json::Value = serde_json::from_str(TH_2026_RULES).unwrap();
        v["tax"]["brackets"][1]["from"] = serde_json::json!(999);
        assert!(matches!(Rules::from_json(&v.to_string()), Err(EngineError::BadRules(_))));
    }
}
