<script lang="ts">
  import TaxSimulator from './TaxSimulator.svelte'
  import Payslip from './Payslip.svelte'

  const tabs = [
    { id: 'tax', label: 'เครื่องคำนวณภาษี' },
    { id: 'payslip', label: 'ตัวอย่างสลิปเงินเดือน' },
  ] as const
  let tab = $state<(typeof tabs)[number]['id']>('tax')
</script>

<main class="mx-auto max-w-3xl p-6 font-sans text-slate-800">
  <img src="/orvahr-logo.png" alt="OrvaHR" class="mb-6 h-10 w-auto" />
  <div class="mb-6 flex gap-2 border-b border-slate-200" role="tablist">
    {#each tabs as t (t.id)}
      <button
        type="button"
        role="tab"
        aria-selected={tab === t.id}
        class="-mb-px border-b-2 px-3 py-2 text-sm {tab === t.id ? 'border-brand-700 font-medium text-brand-700' : 'border-transparent text-slate-500'}"
        onclick={() => (tab = t.id)}
      >{t.label}</button>
    {/each}
  </div>
  {#if tab === 'tax'}<TaxSimulator />{:else}<Payslip />{/if}
</main>
