import { expect, test } from 'bun:test'
import { formatNationalId, isValidNationalId, maskNationalId, nationalIdCheckDigit } from './national-id'

test('check digit and validation', () => {
  expect(nationalIdCheckDigit('110170020345')).toBe(0)
  expect(isValidNationalId('1101700203450')).toBe(true)
  expect(isValidNationalId('1-1017-00203-45-0')).toBe(true)
  expect(isValidNationalId('1101700203451')).toBe(false)
  expect(isValidNationalId('110170020345')).toBe(false)
  expect(isValidNationalId('11017002034500')).toBe(false)
  expect(isValidNationalId('abc')).toBe(false)
  expect(isValidNationalId('')).toBe(false)
})
test('format', () => {
  expect(formatNationalId('1101700203450')).toBe('1-1017-00203-45-0')
  expect(formatNationalId('123')).toBe('123')
})
test('mask', () => {
  expect(maskNationalId('1101700203450')).toBe('1-XXXX-XXXXX-45-0')
  expect(maskNationalId('bad')).toBe('X-XXXX-XXXXX-XX-X')
})
