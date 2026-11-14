// Money crosses the wire as integer satang. Baht in the UI, satang everywhere else.
export const toSatang = (baht: number): number => Math.round(baht * 100)
export const fromSatang = (satang: number): number => satang / 100

const fmt = new Intl.NumberFormat('th-TH', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})
export const formatBaht = (satang: number): string => fmt.format(fromSatang(satang))
