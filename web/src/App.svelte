<script lang="ts">
  import { withholding, type Withholding, type Claim } from './lib/engine'
  import { toSatang, formatBaht } from './lib/money'

  let month = $state(1)
  let salary = $state(50000)
  let bonus = $state(0)
  let spouse = $state(false)
  let children = $state(0)
  let parents = $state(0)
  let lifeInsurance = $state(0)

  let result = $state<Withholding | null>(null)
  let error = $state('')

  // Projection assumes a steady salary since January; the real payroll run
  // passes actual year-to-date figures.
  const claims = $derived.by<Claim[]>(() => {
    const c: Claim[] = []
    if (spouse) c.push({ code: 'spouse', units: 1 })
    if (children > 0) c.push({ code: 'child', units: children })
    if (parents > 0) c.push({ code: 'parent', units: parents })
    if (lifeInsurance > 0) c.push({ code: 'life_insurance', amount: toSatang(lifeInsurance) })
    return c
  })

  $effect(() => {
    const s = toSatang(salary)
    const before = month - 1
    const input = {
      month,
      salary: s,
      other_income: toSatang(bonus),
      ytd_income: s * before,
      ytd_withheld: 0,
      ytd_sso: 0,
      claims,
    }
    let stale = false
    withholding(input)
      .then((r) => { if (!stale) { result = r; error = '' } })
      .catch((e) => { if (!stale) { result = null; error = String(e.message ?? e) } })
    return () => { stale = true }
  })
</script>

<main class="mx-auto max-w-3xl p-6 font-sans text-slate-800">
  <header class="mb-6">
    <h1 class="text-2xl font-semibold text-brand-700">OrvaHR · เครื่องคำนวณภาษีหัก ณ ที่จ่าย</h1>
    <p class="text-sm text-slate-500">
      ตัวเลขเพื่อการจำลองเท่านั้น อัตราและเพดานอ่านจากไฟล์กฎที่มีวันที่มีผลบังคับ ต้องให้นักบัญชีตรวจก่อนใช้จริง
    </p>
  </header>

  <section class="grid gap-4 rounded-xl border border-slate-200 p-4 sm:grid-cols-2">
    <label class="block text-sm">เงินเดือน (บาท)
      <input type="number" min="0" bind:value={salary} class="mt-1 w-full rounded border border-slate-300 p-2" />
    </label>
    <label class="block text-sm">เดือนที่จ่าย (1–12)
      <input type="number" min="1" max="12" bind:value={month} class="mt-1 w-full rounded border border-slate-300 p-2" />
    </label>
    <label class="block text-sm">โบนัสเดือนนี้ (บาท)
      <input type="number" min="0" bind:value={bonus} class="mt-1 w-full rounded border border-slate-300 p-2" />
    </label>
    <label class="block text-sm">เบี้ยประกันชีวิตต่อปี (บาท)
      <input type="number" min="0" bind:value={lifeInsurance} class="mt-1 w-full rounded border border-slate-300 p-2" />
    </label>
    <label class="block text-sm">บุตร (คน)
      <input type="number" min="0" bind:value={children} class="mt-1 w-full rounded border border-slate-300 p-2" />
    </label>
    <label class="block text-sm">บิดามารดา (คน, สูงสุด 4)
      <input type="number" min="0" max="4" bind:value={parents} class="mt-1 w-full rounded border border-slate-300 p-2" />
    </label>
    <label class="flex items-center gap-2 text-sm sm:col-span-2">
      <input type="checkbox" bind:checked={spouse} /> คู่สมรสไม่มีเงินได้
    </label>
  </section>

  {#if error}
    <p class="mt-4 rounded bg-red-50 p-3 text-sm text-red-700" role="alert">{error}</p>
  {:else if result}
    <section class="mt-4 rounded-xl bg-brand-50 p-4">
      <p class="text-sm text-slate-600">ภาษีที่ต้องหักเดือนนี้</p>
      <p class="text-3xl font-semibold text-brand-700">฿{formatBaht(result.withhold_this_month)}</p>
      <dl class="mt-4 grid grid-cols-2 gap-y-1 text-sm">
        <dt>เงินได้รวมทั้งปี (ประมาณการ)</dt><dd class="text-right">{formatBaht(result.annual.gross_income)}</dd>
        <dt>หักค่าใช้จ่าย</dt><dd class="text-right">{formatBaht(result.annual.expense_deduction)}</dd>
        <dt>หักค่าลดหย่อนรวม</dt><dd class="text-right">{formatBaht(result.annual.total_allowances)}</dd>
        <dt>เงินได้สุทธิ</dt><dd class="text-right">{formatBaht(result.annual.taxable_income)}</dd>
        <dt>ภาษีทั้งปี</dt><dd class="text-right">{formatBaht(result.annual.tax)}</dd>
        <dt>ประกันสังคม (ลูกจ้าง / นายจ้าง)</dt>
        <dd class="text-right">{formatBaht(result.sso.employee)} / {formatBaht(result.sso.employer)}</dd>
      </dl>
    </section>
  {/if}
</main>
