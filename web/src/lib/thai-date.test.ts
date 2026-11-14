import { expect, test } from 'bun:test'
import { formatThaiDate, formatThaiMonthYear, toBuddhistYear, toGregorianYear } from './thai-date'

test('year conversion', () => {
  expect(toBuddhistYear(2026)).toBe(2569)
  expect(toGregorianYear(2569)).toBe(2026)
})
test('long and short', () => {
  expect(formatThaiDate('2026-03-05')).toBe('5 มีนาคม 2569')
  expect(formatThaiDate('2026-12-31', 'short')).toBe('31 ธ.ค. 2569')
  expect(formatThaiDate(new Date(2024, 1, 29))).toBe('29 กุมภาพันธ์ 2567')
})
test('month-year', () => {
  expect(formatThaiMonthYear(2026, 1)).toBe('มกราคม 2569')
  expect(() => formatThaiMonthYear(2026, 13)).toThrow()
})
test('bad input', () => {
  expect(() => formatThaiDate('nope')).toThrow()
  expect(() => formatThaiDate(new Date('x'))).toThrow()
})
