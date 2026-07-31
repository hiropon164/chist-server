// Authentication is handled by the proxy's basicauth. The browser resends the
// credentials, so there is nothing to build here.

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
    if (res.status === 401) throw new Error('Authentication required. Reload the page.')
    let message = `The server returned an error (HTTP ${res.status})`
    try {
      const body = await res.json()
      if (body?.error) message = body.error
    } catch {
      /* Non-JSON responses keep the default message */
    }
    throw new Error(message)
  }
  return res.json()
}

export const searchHistory = (params) => get('/api/search', params)
export const listSessions = (params) => get('/api/sessions', params)
export const getSession = (params) => get('/api/session', params)
export const getStats = (params) => get('/api/stats', params)

/** The machines that have pushed. Unlike sources this is not fixed, so ask the server. */
export async function fetchHosts() {
  try {
    const stats = await getStats({ projects: 1 })
    return (stats.hosts ?? []).map((h) => h.host).filter(Boolean)
  } catch {
    // Search still works even if the list cannot be fetched.
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
  return SOURCE_BY_ID.get(id)?.label ?? id ?? 'unknown'
}

export function sourceSlot(id) {
  return SOURCE_BY_ID.get(id)?.slot ?? 1
}

export const ROLES = [
  { id: '', label: 'All' },
  { id: 'user', label: 'user' },
  { id: 'assistant', label: 'assistant' },
  { id: 'system', label: 'system' },
  { id: 'tool', label: 'tool' },
]

/** Split on the \x02 / \x03 markers that snippet() puts around matches.
 *  Returned as an array so the view can wrap hits in <mark> without innerHTML. */
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
  return typeof n === 'number' ? n.toLocaleString('en-US') : '—'
}

/** Shorten cwd to its last two segments. The full path goes in a title attribute. */
export function shortPath(path) {
  if (!path) return ''
  const parts = path.split('/').filter(Boolean)
  return parts.length <= 2 ? path : '…/' + parts.slice(-2).join('/')
}
