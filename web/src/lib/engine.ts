export type Claim = { code: string; units?: number; amount?: number }

export type AnnualTax = {
  gross_income: number
  expense_deduction: number
  allowances: { code: string; amount: number }[]
  total_allowances: number
  taxable_income: number
  tax: number
  slabs: { from: number; to: number; rate_bp: number; tax: number }[]
}

export type Withholding = {
  withhold_this_month: number
  projected_annual_income: number
  sso: { base: number; employee: number; employer: number }
  annual: AnnualTax
}

export type WithholdingInput = {
  month: number
  salary: number
  other_income: number
  ytd_income: number
  ytd_withheld: number
  ytd_sso: number
  claims: Claim[]
}

export async function withholding(input: WithholdingInput): Promise<Withholding> {
  const res = await fetch('/engine/v1/withholding/monthly', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(input),
  })
  const body = await res.json()
  if (!res.ok) throw new Error(body.error ?? `engine returned ${res.status}`)
  return body as Withholding
}
