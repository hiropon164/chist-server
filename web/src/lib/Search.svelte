<script>
  import { searchHistory, segments, formatTs, shortPath } from './api.js'
  import SourceBadge from './SourceBadge.svelte'
  import Filters from './Filters.svelte'

  let { onopen, initialQuery = '', hosts = [] } = $props()

  let query = $state(initialQuery)
  let filters = $state({ sources: [], hosts: [], cwd: '', role: '', since: '', until: '', limit: 30, target: '' })
  let result = $state(null)
  let loading = $state(false)
  let error = $state('')
  let lastQuery = $state('')

  async function run() {
    const q = query.trim()
    if (!q) {
      result = null
      error = ''
      return
    }
    loading = true
    error = ''
    try {
      result = await searchHistory({
        q,
        source: filters.sources,
        host: filters.hosts,
        cwd: filters.cwd,
        role: filters.role,
        since: filters.since,
        until: filters.until,
        limit: filters.limit,
        target: filters.target,
      })
      lastQuery = q
      // Make the result shareable as a link
      const url = new URL(location.href)
      url.searchParams.set('q', q)
      history.replaceState(null, '', url)
    } catch (e) {
      error = e.message
      result = null
    } finally {
      loading = false
    }
  }

  function onkeydown(e) {
    if (e.key === 'Enter') run()
  }

  if (initialQuery) run()
</script>

<section>
  <div class="searchbar">
    <input
      type="search"
      placeholder="Search history (3+ characters uses the full-text index)"
      bind:value={query}
      {onkeydown}
      autocomplete="off"
    />
    <button type="button" class="go" onclick={run} disabled={loading || !query.trim()}>
      {loading ? 'Searching…' : 'Search'}
    </button>
  </div>

  <Filters bind:filters showRole showTarget {hosts} onapply={() => lastQuery && run()} />

  {#if error}
    <p class="msg error">{error}</p>
  {:else if loading}
    <p class="msg">Searching…</p>
  {:else if result}
    <p class="msg">
      “{lastQuery}” — {result.count} results
      {#if result.mode === 'like'}<span class="tag">substring</span>{/if}
      {#if result.target === 'record'}<span class="tag">master index</span>{/if}
      {#if result.note}<span class="note">{result.note}</span>{/if}
    </p>

    <ul class="hits">
      {#each result.hits as hit (hit.id)}
        <li>
          <button type="button" class="hit" onclick={() => onopen({ id: hit.session_id, source: hit.source })}>
            <div class="meta">
              <SourceBadge source={hit.source} />
              <span class="role">{hit.role}</span>
              <span class="ts">{formatTs(hit.ts)}</span>
              {#if hit.cwd}
                <span class="cwd" title={hit.cwd}>{shortPath(hit.cwd)}</span>
              {/if}
            </div>
            {#if hit.session_title}
              <div class="title">{hit.session_title}</div>
            {/if}
            <p class="excerpt">
              {#each segments(hit.excerpt) as seg}
                {#if seg.hit}<mark>{seg.text}</mark>{:else}{seg.text}{/if}
              {/each}
            </p>
          </button>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="msg empty">
      Enter a query to search across your Claude Code, Codex, Cursor CLI and Antigravity CLI history.
    </p>
  {/if}
</section>

<style>
  .searchbar {
    display: flex;
    gap: 8px;
    margin-bottom: 14px;
  }

  .searchbar input {
    flex: 1;
    padding: 10px 14px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: var(--surface-1);
    font-size: 15px;
  }

  .go {
    padding: 10px 20px;
    border-radius: var(--radius);
    border: none;
    background: var(--accent);
    color: #fff;
    font-weight: 600;
  }

  .go:disabled {
    opacity: 0.45;
    cursor: default;
  }

  .msg {
    color: var(--text-secondary);
    margin: 16px 0 10px;
    font-size: 13px;
  }

  .msg.error {
    color: #d03b3b;
  }

  .msg.empty {
    padding: 40px 0;
    text-align: center;
    color: var(--text-muted);
  }

  .tag {
    display: inline-block;
    margin-left: 6px;
    padding: 0 7px;
    border-radius: 999px;
    background: var(--surface-3);
    border: 1px solid var(--border);
    font-size: 11px;
  }

  .note {
    margin-left: 8px;
    color: var(--text-muted);
  }

  .hits {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .hit {
    display: block;
    width: 100%;
    text-align: left;
    padding: 12px 14px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: var(--surface-1);
    transition: border-color 0.12s ease;
  }

  .hit:hover {
    border-color: var(--baseline);
  }

  .meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--text-muted);
  }

  .role {
    font-family: var(--mono);
  }

  .ts {
    font-variant-numeric: tabular-nums;
  }

  .cwd {
    font-family: var(--mono);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 320px;
  }

  .title {
    margin-top: 6px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .excerpt {
    margin: 6px 0 0;
    color: var(--text-secondary);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  mark {
    background: var(--mark-bg);
    color: var(--mark-fg);
    border-radius: 3px;
    padding: 0 2px;
  }
</style>
