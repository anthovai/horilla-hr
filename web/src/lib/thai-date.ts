// Buddhist Era (พ.ศ.) = CE + 543. No Intl calendar dependency, so output is stable.
export const BE_OFFSET = 543

export const THAI_MONTHS = [
  'มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน', 'พฤษภาคม', 'มิถุนายน',
  'กรกฎาคม', 'สิงหาคม', 'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม',
]
export const THAI_MONTHS_SHORT = [
  'ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.',
  'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.',
]

export const toBuddhistYear = (ce: number): number => ce + BE_OFFSET
export const toGregorianYear = (be: number): number => be - BE_OFFSET

function parts(d: Date | string): { y: number; m: number; day: number } {
  if (typeof d === 'string') {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d)
    if (!m) throw new RangeError(`bad date: ${d}`)
    return { y: +m[1], m: +m[2] - 1, day: +m[3] }
  }
  if (Number.isNaN(d.getTime())) throw new RangeError('invalid date')
  return { y: d.getFullYear(), m: d.getMonth(), day: d.getDate() }
}

/** "5 มีนาคม 2569" (long) or "5 มี.ค. 2569" (short). ISO strings are read as calendar dates, no timezone shift. */
export function formatThaiDate(d: Date | string, style: 'long' | 'short' = 'long'): string {
  const { y, m, day } = parts(d)
  const name = (style === 'long' ? THAI_MONTHS : THAI_MONTHS_SHORT)[m]
  return `${day} ${name} ${toBuddhistYear(y)}`
}

/** "มีนาคม 2569" for a 1-based month. */
export function formatThaiMonthYear(ceYear: number, month: number): string {
  if (!Number.isInteger(month) || month < 1 || month > 12) throw new RangeError('month must be 1-12')
  return `${THAI_MONTHS[month - 1]} ${toBuddhistYear(ceYear)}`
}
