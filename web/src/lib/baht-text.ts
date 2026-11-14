// Thai baht text (บาทถ้วน) from integer satang.
const DIGITS = ['', 'หนึ่ง', 'สอง', 'สาม', 'สี่', 'ห้า', 'หก', 'เจ็ด', 'แปด', 'เก้า']
const PLACES = ['', 'สิบ', 'ร้อย', 'พัน', 'หมื่น', 'แสน']

// Reads 0 < n < 1_000_000. `continued` means higher digits precede this chunk
// (so a trailing 1 is เอ็ด even when the chunk is just "1", e.g. 1,000,001).
function readChunk(n: number, continued: boolean): string {
  const s = String(n)
  let out = ''
  for (let i = 0; i < s.length; i++) {
    const d = Number(s[i])
    const pos = s.length - 1 - i
    if (d === 0) continue
    if (pos === 0) {
      out += d === 1 && (s.length > 1 || continued) ? 'เอ็ด' : DIGITS[d]
    } else if (pos === 1) {
      out += (d === 1 ? '' : d === 2 ? 'ยี่' : DIGITS[d]) + 'สิบ'
    } else {
      out += DIGITS[d] + PLACES[pos]
    }
  }
  return out
}

function readInt(n: number): string {
  if (n === 0) return 'ศูนย์'
  const chunks: number[] = []
  for (let v = n; v > 0; v = Math.floor(v / 1_000_000)) chunks.unshift(v % 1_000_000)
  let out = ''
  chunks.forEach((c, i) => {
    const last = i === chunks.length - 1
    if (c > 0) out += readChunk(c, i > 0)
    if (!last) out += 'ล้าน'
  })
  return out
}

export function bahtText(satang: number): string {
  if (!Number.isSafeInteger(satang)) throw new RangeError('satang must be a safe integer')
  const neg = satang < 0
  const abs = Math.abs(satang)
  const baht = Math.floor(abs / 100)
  const sat = abs % 100
  let out = readInt(baht) + 'บาท'
  out += sat === 0 ? 'ถ้วน' : readInt(sat) + 'สตางค์'
  return (neg ? 'ลบ' : '') + out
}
