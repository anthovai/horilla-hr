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
    /// No loaded rule set has `effective_from <= as_of`.
    NoRuleSet(String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::UnknownAllowance(c) => write!(f, "unknown allowance code: {c}"),
            EngineError::InvalidInput(m) => write!(f, "invalid input: {m}"),
            EngineError::BadRules(m) => write!(f, "bad rules file: {m}"),
            EngineError::NoRuleSet(d) => write!(f, "no rule set is effective on {d}"),
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
    /// Joint ceilings over several allowances, applied after the per-item caps.
    #[serde(default)]
    pub groups: Vec<GroupRule>,
}

/// A ceiling shared by several `capped` allowances (e.g. insurance premiums
/// combined, retirement funds combined). Members are trimmed in the order they
/// are listed: earlier members keep their amount first.
#[derive(Debug, Clone, Deserialize)]
pub struct GroupRule {
    pub code: String,
    pub members: Vec<String>,
    pub cap: Satang,
    /// Optional second ceiling: this many basis points of gross income. The
    /// lower of `cap` and this applies.
    pub cap_percent_of_income_bp: Option<i64>,
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
    PerUnit {
        amount: Satang,
        max_units: Option<u32>,
    },
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
            return Err(EngineError::BadRules(
                "top bracket must be open-ended".into(),
            ));
        }
        if self.social_security.base_min > self.social_security.base_max {
            return Err(EngineError::BadRules(
                "sso base_min exceeds base_max".into(),
            ));
        }
        if self.social_security.rounding_satang <= 0 {
            return Err(EngineError::BadRules(
                "sso rounding_satang must be positive".into(),
            ));
        }
        parse_date(&self.effective_from)
            .map_err(|e| EngineError::BadRules(format!("effective_from: {e}")))?;
        let mut seen_members: Vec<&str> = Vec::new();
        for (i, g) in self.tax.groups.iter().enumerate() {
            if self.tax.groups[..i].iter().any(|o| o.code == g.code) {
                return Err(EngineError::BadRules(format!(
                    "duplicate group code {}",
                    g.code
                )));
            }
            if g.members.is_empty() {
                return Err(EngineError::BadRules(format!(
                    "group {} has no members",
                    g.code
                )));
            }
            if g.cap < 0 || g.cap_percent_of_income_bp.is_some_and(|b| b < 0) {
                return Err(EngineError::BadRules(format!(
                    "group {} has a negative cap",
                    g.code
                )));
            }
            for m in &g.members {
                let rule = self
                    .tax
                    .allowances
                    .iter()
                    .find(|a| &a.code == m)
                    .ok_or_else(|| {
                        EngineError::BadRules(format!(
                            "group {} names unknown allowance {m}",
                            g.code
                        ))
                    })?;
                if !matches!(rule.kind, AllowanceKind::Capped { .. }) {
                    return Err(EngineError::BadRules(format!(
                        "group {}: member {m} must be a capped allowance",
                        g.code
                    )));
                }
                if seen_members.contains(&m.as_str()) {
                    return Err(EngineError::BadRules(format!(
                        "allowance {m} is in more than one group"
                    )));
                }
                seen_members.push(m);
            }
        }
        Ok(())
    }
}

// ------------------------------------------------------------------ dates

/// Parse a strict `YYYY-MM-DD` calendar date (leap years honoured).
pub fn parse_date(s: &str) -> Result<(i32, u32, u32), String> {
    let b = s.as_bytes();
    let bad = || format!("{s:?} is not a YYYY-MM-DD date");
    if b.len() != 10 || b[4] != b'-' || b[7] != b'-' {
        return Err(bad());
    }
    if !b
        .iter()
        .enumerate()
        .all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit())
    {
        return Err(bad());
    }
    let y: i32 = s[0..4].parse().map_err(|_| bad())?;
    let m: u32 = s[5..7].parse().map_err(|_| bad())?;
    let d: u32 = s[8..10].parse().map_err(|_| bad())?;
    let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    let dim = match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return Err(bad()),
    };
    if y < 1 || d < 1 || d > dim {
        return Err(bad());
    }
    Ok((y, m, d))
}

/// Today's date in UTC as `YYYY-MM-DD`.
pub fn today_utc() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let z = (secs / 86_400) as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}")
}

// -------------------------------------------------------------- rule book

/// Every loaded rule set, ordered by `effective_from`.
#[derive(Debug, Clone)]
pub struct RuleBook {
    sets: Vec<Rules>,
}

impl RuleBook {
    pub fn from_rules(mut sets: Vec<Rules>) -> Result<RuleBook, EngineError> {
        if sets.is_empty() {
            return Err(EngineError::BadRules("no rule sets loaded".into()));
        }
        sets.sort_by(|a, b| a.effective_from.cmp(&b.effective_from));
        for pair in sets.windows(2) {
            if pair[0].effective_from == pair[1].effective_from {
                return Err(EngineError::BadRules(format!(
                    "two rule sets share effective_from {}",
                    pair[0].effective_from
                )));
            }
        }
        if sets.iter().any(|r| r.country != sets[0].country) {
            return Err(EngineError::BadRules("rule sets mix countries".into()));
        }
        Ok(RuleBook { sets })
    }

    /// Load every `*.json` file in `dir` (non-recursive).
    pub fn from_dir(dir: &std::path::Path) -> Result<RuleBook, EngineError> {
        let bad = |m: String| EngineError::BadRules(m);
        let mut paths: Vec<_> = std::fs::read_dir(dir)
            .map_err(|e| bad(format!("cannot read {}: {e}", dir.display())))?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|x| x == "json"))
            .collect();
        paths.sort();
        let mut sets = Vec::new();
        for p in paths {
            let text = std::fs::read_to_string(&p)
                .map_err(|e| bad(format!("cannot read {}: {e}", p.display())))?;
            sets.push(Rules::from_json(&text).map_err(|e| bad(format!("{}: {e}", p.display())))?);
        }
        RuleBook::from_rules(sets)
    }

    pub fn single(rules: Rules) -> RuleBook {
        RuleBook { sets: vec![rules] }
    }

    pub fn bundled() -> RuleBook {
        RuleBook::single(Rules::th_2026())
    }

    pub fn sets(&self) -> &[Rules] {
        &self.sets
    }

    /// The latest rule set with `effective_from <= as_of` (default: today, UTC).
    pub fn select(&self, as_of: Option<&str>) -> Result<&Rules, EngineError> {
        let today;
        let date = match as_of {
            Some(d) => d,
            None => {
                today = today_utc();
                &today
            }
        };
        parse_date(date).map_err(EngineError::InvalidInput)?;
        self.sets
            .iter()
            .rev()
            .find(|r| r.effective_from.as_str() <= date)
            .ok_or_else(|| EngineError::NoRuleSet(date.to_string()))
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

/// What a group ceiling removed. Only reported when something was trimmed.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct GroupAdjustment {
    pub group: String,
    /// Sum of the members' per-item-capped amounts, before the group ceiling.
    pub requested: Satang,
    /// The ceiling that applied: min(`cap`, percent-of-income cap if any).
    pub cap_applied: Satang,
    pub allowed: Satang,
    pub trimmed: Satang,
    /// Per member that lost something.
    pub members_trimmed: Vec<AppliedAllowance>,
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
    pub group_adjustments: Vec<GroupAdjustment>,
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
        return Err(EngineError::InvalidInput(
            "amounts cannot be negative".into(),
        ));
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
            applied.push(AppliedAllowance {
                code: rule.code.clone(),
                amount,
            });
        }
    }
    for claim in claims {
        if !t.allowances.iter().any(|r| r.code == claim.code) {
            return Err(EngineError::UnknownAllowance(claim.code.clone()));
        }
    }

    // Joint ceilings, after the per-item caps.
    let mut group_adjustments = Vec::new();
    for g in &t.groups {
        let requested: Satang = applied
            .iter()
            .filter(|a| g.members.contains(&a.code))
            .map(|a| a.amount)
            .sum();
        let cap_applied = match g.cap_percent_of_income_bp {
            Some(bp) => g.cap.min(apply_bp(gross_income, bp)),
            None => g.cap,
        };
        if requested <= cap_applied {
            continue;
        }
        let mut budget = cap_applied;
        let mut members_trimmed = Vec::new();
        for code in &g.members {
            if let Some(a) = applied.iter_mut().find(|a| &a.code == code) {
                let kept = a.amount.min(budget);
                budget -= kept;
                if kept < a.amount {
                    members_trimmed.push(AppliedAllowance {
                        code: code.clone(),
                        amount: a.amount - kept,
                    });
                }
                a.amount = kept;
            }
        }
        applied.retain(|a| a.amount > 0);
        group_adjustments.push(GroupAdjustment {
            group: g.code.clone(),
            requested,
            cap_applied,
            allowed: cap_applied,
            trimmed: requested - cap_applied,
            members_trimmed,
        });
    }

    let sso_deduction = sso_paid_year.min(rules.social_security.employee_deduction_cap_per_year);
    if sso_deduction > 0 {
        applied.push(AppliedAllowance {
            code: "social_security".into(),
            amount: sso_deduction,
        });
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
        slabs.push(SlabRow {
            from: b.from,
            to: upper,
            rate_bp: b.rate_bp,
            tax: slab_tax,
        });
    }

    Ok(AnnualTax {
        gross_income,
        expense_deduction,
        allowances: applied,
        group_adjustments,
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
    Ok(Sso {
        base,
        employee: amount,
        employer: amount,
    })
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
    /// Whether the employee pays social security. Defaults to true; when false
    /// no contribution is taken or deducted from taxable income.
    #[serde(default = "default_true")]
    pub sso_enrolled: bool,
    #[serde(default)]
    pub claims: Vec<Claim>,
}

fn default_true() -> bool {
    true
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
    if [
        input.salary,
        input.other_income,
        input.ytd_income,
        input.ytd_withheld,
        input.ytd_sso,
    ]
    .iter()
    .any(|v| *v < 0)
    {
        return Err(EngineError::InvalidInput(
            "amounts cannot be negative".into(),
        ));
    }

    let months_left_after = (12 - input.month) as Satang;
    let periods = months_left_after + 1;

    let sso = if input.sso_enrolled {
        social_security(rules, input.salary)?
    } else {
        Sso {
            base: 0,
            employee: 0,
            employer: 0,
        }
    };
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

    Ok(Withholding {
        withhold_this_month,
        projected_annual_income,
        sso,
        annual,
    })
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
            Claim {
                code: "spouse".into(),
                units: Some(3),
                amount: None,
            },
            Claim {
                code: "child".into(),
                units: Some(2),
                amount: None,
            },
        ];
        let t = annual_tax(&rules(), 1_000_000 * BAHT, &claims, 0).unwrap();
        let get = |c: &str| t.allowances.iter().find(|a| a.code == c).map(|a| a.amount);
        assert_eq!(get("spouse"), Some(60_000 * BAHT)); // capped at 1 unit
        assert_eq!(get("child"), Some(60_000 * BAHT));
    }

    #[test]
    fn capped_allowance_stops_at_cap() {
        let claims = vec![Claim {
            code: "life_insurance".into(),
            units: None,
            amount: Some(250_000 * BAHT),
        }];
        let t = annual_tax(&rules(), 1_000_000 * BAHT, &claims, 0).unwrap();
        let v = t
            .allowances
            .iter()
            .find(|a| a.code == "life_insurance")
            .unwrap();
        assert_eq!(v.amount, 100_000 * BAHT);
    }

    #[test]
    fn unknown_claim_code_is_rejected_not_ignored() {
        let claims = vec![Claim {
            code: "yacht".into(),
            units: Some(1),
            amount: None,
        }];
        assert_eq!(
            annual_tax(&rules(), 500_000 * BAHT, &claims, 0),
            Err(EngineError::UnknownAllowance("yacht".into()))
        );
    }

    #[test]
    fn sso_is_clamped_to_floor_and_ceiling() {
        let r = rules();
        // Base ceiling 17,500 from 1 Jan 2026 (Ministerial Regulation B.E. 2568).
        assert_eq!(
            social_security(&r, 30_000 * BAHT).unwrap().employee,
            875 * BAHT
        );
        assert_eq!(
            social_security(&r, 17_500 * BAHT).unwrap().employee,
            875 * BAHT
        );
        assert_eq!(
            social_security(&r, 15_000 * BAHT).unwrap().employee,
            750 * BAHT
        );
        assert_eq!(
            social_security(&r, 10_000 * BAHT).unwrap().employee,
            500 * BAHT
        );
        assert_eq!(
            social_security(&r, 1_000 * BAHT).unwrap().base,
            1_650 * BAHT
        );
        assert_eq!(
            social_security(&r, 1_000 * BAHT).unwrap().employee,
            83 * BAHT
        ); // 82.50 -> 83
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
                    sso_enrolled: true,
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
            month: 3,
            salary: 50_000 * BAHT,
            other_income: 0,
            ytd_income: 100_000 * BAHT,
            ytd_withheld: 0,
            ytd_sso: 1_500 * BAHT,
            sso_enrolled: true,
            claims: vec![],
        };
        let with_bonus = WithholdingInput {
            other_income: 100_000 * BAHT,
            ..base.clone()
        };
        let a = monthly_withholding(&r, &base).unwrap().withhold_this_month;
        let b = monthly_withholding(&r, &with_bonus)
            .unwrap()
            .withhold_this_month;
        assert!(b > a);
    }

    #[test]
    fn invalid_month_is_rejected() {
        let r = rules();
        let input = WithholdingInput {
            month: 13,
            salary: 1,
            other_income: 0,
            ytd_income: 0,
            ytd_withheld: 0,
            ytd_sso: 0,
            sso_enrolled: true,
            claims: vec![],
        };
        assert!(matches!(
            monthly_withholding(&r, &input),
            Err(EngineError::InvalidInput(_))
        ));
    }

    #[test]
    fn broken_bracket_chain_is_rejected() {
        let mut v: serde_json::Value = serde_json::from_str(TH_2026_RULES).unwrap();
        v["tax"]["brackets"][1]["from"] = serde_json::json!(999);
        assert!(matches!(
            Rules::from_json(&v.to_string()),
            Err(EngineError::BadRules(_))
        ));
    }

    fn with_groups(groups: serde_json::Value) -> Rules {
        let mut v: serde_json::Value = serde_json::from_str(TH_2026_RULES).unwrap();
        v["tax"]["groups"] = groups;
        Rules::from_json(&v.to_string()).unwrap()
    }

    fn claim(code: &str, baht: i64) -> Claim {
        Claim {
            code: code.into(),
            units: None,
            amount: Some(baht * BAHT),
        }
    }

    #[test]
    fn group_trims_in_member_order_and_reports_it() {
        let r = with_groups(serde_json::json!([
            { "code": "g", "members": ["life_insurance", "health_insurance"], "cap": 10_000_000 }
        ]));
        // per-item: life 100k (of 150k), health 25k -> 125k requested, ceiling 100k.
        let claims = vec![
            claim("life_insurance", 150_000),
            claim("health_insurance", 30_000),
        ];
        let t = annual_tax(&r, 2_000_000 * BAHT, &claims, 0).unwrap();
        let get = |c: &str| t.allowances.iter().find(|a| a.code == c).map(|a| a.amount);
        assert_eq!(get("life_insurance"), Some(100_000 * BAHT));
        assert_eq!(get("health_insurance"), None);
        let adj = &t.group_adjustments[0];
        assert_eq!(
            (adj.requested, adj.allowed, adj.trimmed),
            (125_000 * BAHT, 100_000 * BAHT, 25_000 * BAHT)
        );
        assert_eq!(
            adj.members_trimmed,
            vec![AppliedAllowance {
                code: "health_insurance".into(),
                amount: 25_000 * BAHT
            }]
        );
    }

    #[test]
    fn group_percent_of_income_cap_applies_when_lower() {
        let r = with_groups(serde_json::json!([
            { "code": "g", "members": ["provident_fund", "rmf"], "cap": 50_000_000, "cap_percent_of_income_bp": 1000 }
        ]));
        let claims = vec![claim("provident_fund", 60_000), claim("rmf", 60_000)];
        let t = annual_tax(&r, 600_000 * BAHT, &claims, 0).unwrap(); // 10% = 60k
        assert_eq!(t.group_adjustments[0].cap_applied, 60_000 * BAHT);
        assert_eq!(t.group_adjustments[0].trimmed, 60_000 * BAHT);
        let sum: Satang = t
            .allowances
            .iter()
            .filter(|a| a.code == "provident_fund" || a.code == "rmf")
            .map(|a| a.amount)
            .sum();
        assert_eq!(sum, 60_000 * BAHT);
    }

    #[test]
    fn group_under_ceiling_reports_nothing() {
        let r = with_groups(serde_json::json!([
            { "code": "g", "members": ["life_insurance", "health_insurance"], "cap": 10_000_000 }
        ]));
        let claims = vec![
            claim("life_insurance", 50_000),
            claim("health_insurance", 20_000),
        ];
        let t = annual_tax(&r, 2_000_000 * BAHT, &claims, 0).unwrap();
        assert!(t.group_adjustments.is_empty());
    }

    #[test]
    fn group_validation_rejects_bad_members() {
        let mk = |g: serde_json::Value| {
            let mut v: serde_json::Value = serde_json::from_str(TH_2026_RULES).unwrap();
            v["tax"]["groups"] = g;
            Rules::from_json(&v.to_string())
        };
        assert!(matches!(
            mk(serde_json::json!([{ "code": "g", "members": ["nope"], "cap": 1 }])),
            Err(EngineError::BadRules(_))
        ));
        assert!(matches!(
            mk(serde_json::json!([{ "code": "g", "members": ["spouse"], "cap": 1 }])),
            Err(EngineError::BadRules(_))
        ));
        assert!(matches!(
            mk(serde_json::json!([{ "code": "g", "members": [], "cap": 1 }])),
            Err(EngineError::BadRules(_))
        ));
        assert!(matches!(
            mk(serde_json::json!([
                { "code": "a", "members": ["rmf"], "cap": 1 },
                { "code": "b", "members": ["rmf"], "cap": 1 }
            ])),
            Err(EngineError::BadRules(_))
        ));
    }

    #[test]
    fn date_parsing_is_strict() {
        assert!(parse_date("2026-02-28").is_ok());
        assert!(parse_date("2024-02-29").is_ok());
        for bad in [
            "2026-02-29",
            "2026-13-01",
            "2026-1-01",
            "26-01-01",
            "2026/01/01",
            "2026-01-0a",
            "",
            "2026-00-10",
        ] {
            assert!(parse_date(bad).is_err(), "{bad}");
        }
        assert!(parse_date(&today_utc()).is_ok());
    }

    fn book_with(dates: &[&str]) -> RuleBook {
        let sets = dates
            .iter()
            .map(|d| {
                let mut v: serde_json::Value = serde_json::from_str(TH_2026_RULES).unwrap();
                v["effective_from"] = serde_json::json!(d);
                v["year"] = serde_json::json!(d[0..4].parse::<i32>().unwrap());
                Rules::from_json(&v.to_string()).unwrap()
            })
            .collect();
        RuleBook::from_rules(sets).unwrap()
    }

    #[test]
    fn as_of_picks_latest_effective_set() {
        let b = book_with(&["2027-01-01", "2026-01-01"]);
        assert_eq!(b.select(Some("2026-12-31")).unwrap().year, 2026);
        assert_eq!(b.select(Some("2027-01-01")).unwrap().year, 2027);
        assert_eq!(b.select(Some("2030-06-01")).unwrap().year, 2027);
        assert!(matches!(
            b.select(Some("2025-12-31")),
            Err(EngineError::NoRuleSet(_))
        ));
        assert!(matches!(
            b.select(Some("garbage")),
            Err(EngineError::InvalidInput(_))
        ));
        assert!(b.select(None).is_ok());
    }

    #[test]
    fn duplicate_effective_dates_are_rejected() {
        let mut v: serde_json::Value = serde_json::from_str(TH_2026_RULES).unwrap();
        v["effective_from"] = serde_json::json!("2026-01-01");
        let r = Rules::from_json(&v.to_string()).unwrap();
        assert!(RuleBook::from_rules(vec![r.clone(), r]).is_err());
    }

    #[test]
    fn bad_effective_from_is_rejected() {
        let mut v: serde_json::Value = serde_json::from_str(TH_2026_RULES).unwrap();
        v["effective_from"] = serde_json::json!("1 Jan 2026");
        assert!(matches!(
            Rules::from_json(&v.to_string()),
            Err(EngineError::BadRules(_))
        ));
    }
}

#[cfg(test)]
mod sso_enrolment_tests {
    use super::*;

    fn input(enrolled: bool) -> WithholdingInput {
        WithholdingInput {
            month: 1,
            salary: 50_000 * 100,
            other_income: 0,
            ytd_income: 0,
            ytd_withheld: 0,
            ytd_sso: 0,
            sso_enrolled: enrolled,
            claims: vec![],
        }
    }

    #[test]
    fn not_enrolled_means_no_contribution_and_no_deduction() {
        let rules = Rules::th_2026();
        let out = monthly_withholding(&rules, &input(false)).unwrap();
        assert_eq!(
            out.sso,
            Sso {
                base: 0,
                employee: 0,
                employer: 0
            }
        );
        assert!(out
            .annual
            .allowances
            .iter()
            .all(|a| a.code != "social_security"));
    }

    #[test]
    fn enrolled_is_the_default_and_pays_less_tax_than_not_enrolled() {
        let rules = Rules::th_2026();
        let enrolled = monthly_withholding(&rules, &input(true)).unwrap();
        let not = monthly_withholding(&rules, &input(false)).unwrap();
        assert!(enrolled.sso.employee > 0);
        assert!(enrolled.withhold_this_month < not.withhold_this_month);
        let json = r#"{"month":1,"salary":100}"#;
        let parsed: WithholdingInput = serde_json::from_str(json).unwrap();
        assert!(
            parsed.sso_enrolled,
            "omitting the field keeps the old behaviour"
        );
    }
}
