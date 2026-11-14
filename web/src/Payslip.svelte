<script lang="ts">
  import { toSatang, formatBaht } from './lib/money'
  import { bahtText } from './lib/baht-text'
  import { formatThaiMonthYear, formatThaiDate } from './lib/thai-date'
  import { formatNationalId, maskNationalId, isValidNationalId } from './lib/national-id'

  // Placeholder data: the real payslip will be fed from the payroll run.
  const name = 'ชื่อ นามสกุล (ตัวอย่าง)'
  const nationalId = '1101700203450'
  const now = new Date()
  const period = formatThaiMonthYear(now.getFullYear(), now.getMonth() + 1)
  const payDate = formatThaiDate(now)

  let showId = $state(false)
  let salary = $state(50000)
  let overtime = $state(0)
  let tax = $state(1250)
  let sso = $state(750)
  let other = $state(0)

  const earnings = $derived(toSatang(salary) + toSatang(overtime))
  const deductions = $derived(toSatang(tax) + toSatang(sso) + toSatang(other))
  const net = $derived(earnings - deductions)
</script>

<section class="grid gap-4 rounded-xl border border-slate-200 p-4 sm:grid-cols-2">
  <label class="block text-sm">เงินเดือน (บาท)
    <input type="number" min="0" bind:value={salary} class="mt-1 w-full rounded border border-slate-300 p-2" />
  </label>
  <label class="block text-sm">ค่าล่วงเวลา (บาท)
    <input type="number" min="0" bind:value={overtime} class="mt-1 w-full rounded border border-slate-300 p-2" />
  </label>
  <label class="block text-sm">ภาษีหัก ณ ที่จ่าย (บาท)
    <input type="number" min="0" bind:value={tax} class="mt-1 w-full rounded border border-slate-300 p-2" />
  </label>
  <label class="block text-sm">ประกันสังคม (บาท)
    <input type="number" min="0" bind:value={sso} class="mt-1 w-full rounded border border-slate-300 p-2" />
  </label>
  <label class="block text-sm">หักอื่น ๆ (บาท)
    <input type="number" min="0" bind:value={other} class="mt-1 w-full rounded border border-slate-300 p-2" />
  </label>
</section>

<article class="mt-4 rounded-xl border border-slate-200 p-4">
  <h2 class="text-lg font-semibold text-brand-700">สลิปเงินเดือน · {period}</h2>
  <p class="text-sm text-slate-600">{name}</p>
  <p class="text-sm text-slate-600">
    เลขบัตรประชาชน
    {showId ? formatNationalId(nationalId) : maskNationalId(nationalId)}
    {#if !isValidNationalId(nationalId)}<span class="text-red-700">(ไม่ถูกต้อง)</span>{/if}
    <button type="button" class="ml-2 text-brand-700 underline" onclick={() => (showId = !showId)}>
      {showId ? 'ซ่อน' : 'แสดง'}
    </button>
  </p>
  <p class="text-sm text-slate-600">วันที่จ่าย {payDate}</p>

  <div class="mt-4 grid gap-4 sm:grid-cols-2">
    <div>
      <h3 class="mb-1 text-sm font-medium">รายได้</h3>
      <dl class="grid grid-cols-2 gap-y-1 text-sm">
        <dt>เงินเดือน</dt><dd class="text-right">{formatBaht(toSatang(salary))}</dd>
        <dt>ค่าล่วงเวลา</dt><dd class="text-right">{formatBaht(toSatang(overtime))}</dd>
        <dt class="font-medium">รวมรายได้</dt><dd class="text-right font-medium">{formatBaht(earnings)}</dd>
      </dl>
    </div>
    <div>
      <h3 class="mb-1 text-sm font-medium">รายการหัก</h3>
      <dl class="grid grid-cols-2 gap-y-1 text-sm">
        <dt>ภาษีหัก ณ ที่จ่าย</dt><dd class="text-right">{formatBaht(toSatang(tax))}</dd>
        <dt>ประกันสังคม</dt><dd class="text-right">{formatBaht(toSatang(sso))}</dd>
        <dt>อื่น ๆ</dt><dd class="text-right">{formatBaht(toSatang(other))}</dd>
        <dt class="font-medium">รวมรายการหัก</dt><dd class="text-right font-medium">{formatBaht(deductions)}</dd>
      </dl>
    </div>
  </div>

  <div class="mt-4 rounded-lg bg-brand-50 p-3">
    <p class="text-sm text-slate-600">เงินได้สุทธิ</p>
    <p class="text-3xl font-semibold text-brand-700">฿{formatBaht(net)}</p>
    <p class="text-sm text-slate-700">({bahtText(net)})</p>
  </div>
</article>
