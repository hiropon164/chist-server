// 認証は Traefik の basicauth が受け持つ。ブラウザが資格情報を再送するので、
// ここでヘッダを組み立てる必要はない。

async function get(path, params = {}) {
  const url = new URL(path, location.origin)
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) continue
    if (Array.isArray(value)) {
      for (const v of value) url.searchParams.append(key, v)
    } else {
      url.searchParams.set(key, value)
    }
  }

  const res = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!res.ok) {
    if (res.status === 401) throw new Error('認証が必要です。ページを再読み込みしてください。')
    let message = `サーバがエラーを返しました (HTTP ${res.status})`
    try {
      const body = await res.json()
      if (body?.error) message = body.error
    } catch {
      /* JSON でない応答はそのまま既定メッセージにする */
    }
    throw new Error(message)
  }
  return res.json()
}

export const searchHistory = (params) => get('/api/search', params)
export const listSessions = (params) => get('/api/sessions', params)
export const getSession = (params) => get('/api/session', params)
export const getStats = (params) => get('/api/stats', params)

/** 送信元マシンの一覧。ソースと違って固定ではないのでサーバから取る。 */
export async function fetchHosts() {
  try {
    const stats = await getStats({ projects: 1 })
    return (stats.hosts ?? []).map((h) => h.host).filter(Boolean)
  } catch {
    // 一覧が取れなくても検索自体は使えるようにする
    return []
  }
}

export const SOURCES = [
  { id: 'claude-code', label: 'Claude Code', slot: 1 },
  { id: 'codex', label: 'Codex', slot: 2 },
  { id: 'cursor-cli', label: 'Cursor CLI', slot: 3 },
  { id: 'antigravity-cli', label: 'Antigravity', slot: 4 },
]

const SOURCE_BY_ID = new Map(SOURCES.map((s) => [s.id, s]))

export function sourceLabel(id) {
  return SOURCE_BY_ID.get(id)?.label ?? id ?? '不明'
}

export function sourceSlot(id) {
  return SOURCE_BY_ID.get(id)?.slot ?? 1
}

export const ROLES = [
  { id: '', label: 'すべて' },
  { id: 'user', label: 'user' },
  { id: 'assistant', label: 'assistant' },
  { id: 'system', label: 'system' },
  { id: 'tool', label: 'tool' },
]

/** snippet() が返す \x02 / \x03 で囲まれた一致箇所を切り分ける。
 *  innerHTML を使わずに済ませるため、配列にして描画側で <mark> を当てる。 */
export function segments(text) {
  const out = []
  let rest = text ?? ''
  while (rest) {
    const start = rest.indexOf('\u0002')
    if (start < 0) {
      out.push({ hit: false, text: rest })
      break
    }
    if (start > 0) out.push({ hit: false, text: rest.slice(0, start) })
    const end = rest.indexOf('\u0003', start)
    if (end < 0) {
      out.push({ hit: true, text: rest.slice(start + 1) })
      break
    }
    out.push({ hit: true, text: rest.slice(start + 1, end) })
    rest = rest.slice(end + 1)
  }
  return out
}

export function formatTs(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function formatNumber(n) {
  return typeof n === 'number' ? n.toLocaleString('ja-JP') : '—'
}

/** cwd を末尾 2 階層まで縮める。全体は title 属性で見せる。 */
export function shortPath(path) {
  if (!path) return ''
  const parts = path.split('/').filter(Boolean)
  return parts.length <= 2 ? path : '…/' + parts.slice(-2).join('/')
}
