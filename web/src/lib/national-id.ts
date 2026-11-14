// Thai national ID: 13 digits, last is a mod-11 check digit.
export const digitsOnly = (s: string): string => s.replace(/\D/g, '')

export function nationalIdCheckDigit(first12: string): number {
  let sum = 0
  for (let i = 0; i < 12; i++) sum += Number(first12[i]) * (13 - i)
  return (11 - (sum % 11)) % 10
}

export function isValidNationalId(input: string): boolean {
  if (!/^[\d\s-]*$/.test(input)) return false
  const id = digitsOnly(input)
  return id.length === 13 && nationalIdCheckDigit(id.slice(0, 12)) === Number(id[12])
}

/** 1-2345-67890-12-1. Returns the input unchanged unless it holds 13 digits. */
export function formatNationalId(input: string): string {
  const id = digitsOnly(input)
  if (id.length !== 13) return input
  return `${id[0]}-${id.slice(1, 5)}-${id.slice(5, 10)}-${id.slice(10, 12)}-${id[12]}`
}

/** Shows first digit and last two: 1-XXXX-XXXXX-X2-1 style is avoided; we mask the middle. */
export function maskNationalId(input: string): string {
  const id = digitsOnly(input)
  if (id.length !== 13) return 'X-XXXX-XXXXX-XX-X'
  return `${id[0]}-XXXX-XXXXX-${id.slice(10, 12)}-${id[12]}`
}
