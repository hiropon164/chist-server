<script>
  import { listSessions, formatTs, formatNumber, shortPath } from './api.js'
  import SourceBadge from './SourceBadge.svelte'
  import Filters from './Filters.svelte'

  let { onopen, hosts = [] } = $props()

  let filters = $state({ sources: [], hosts: [], cwd: '', role: '', since: '', until: '', limit: 50 })
  let data = $state(null)
  let loading = $state(true)
  let error = $state('')

  async function load() {
    loading = true
    error = ''
    try {
      data = await listSessions({
        source: filters.sources,
        host: filters.hosts,
        cwd: filters.cwd,
        since: filters.since,
        limit: filters.limit,
      })
    } catch (e) {
      error = e.message
      data = null
    } finally {
      loading = false
    }
  }

  load()
</script>

<section>
  <Filters bind:filters {hosts} onapply={load} />

  {#if error}
    <p class="msg error">{error}</p>
  {:else if loading}
    <p class="msg">Loading…</p>
  {:else if data?.sessions.length}
    <p class="msg">{data.count} sessions (newest first)</p>
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th>Source</th>
            <th>Title</th>
            <th>Project</th>
            {#if hosts.length > 1}<th>Machine</th>{/if}
            <th class="num">Msgs</th>
            <th>Last</th>
          </tr>
        </thead>
        <tbody>
          {#each data.sessions as s (s.source + s.id)}
            <tr onclick={() => onopen({ id: s.id, source: s.source })} tabindex="0" role="button">
              <td><SourceBadge source={s.source} /></td>
              <td class="title">{s.title || '(untitled)'}</td>
              <td class="cwd" title={s.cwd}>{shortPath(s.cwd) || '—'}</td>
              {#if hosts.length > 1}<td class="cwd">{s.host || '—'}</td>{/if}
              <td class="num">{formatNumber(s.messages)}</td>
              <td class="ts">{formatTs(s.ended_at || s.started_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="msg empty">No sessions match.</p>
  {/if}
</section>

<style>
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

  .tablewrap {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface-1);
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  th {
    text-align: left;
    padding: 9px 12px;
    color: var(--text-muted);
    font-weight: 500;
    font-size: 12px;
    border-bottom: 1px solid var(--gridline);
    white-space: nowrap;
  }

  td {
    padding: 9px 12px;
    border-bottom: 1px solid var(--gridline);
    vertical-align: middle;
  }

  tbody tr:last-child td {
    border-bottom: none;
  }

  tbody tr {
    cursor: pointer;
  }

  tbody tr:hover {
    background: var(--surface-3);
  }

  .title {
    color: var(--text-primary);
    max-width: 420px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cwd {
    font-family: var(--mono);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .num,
  .ts {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .num {
    text-align: right;
  }

  .ts {
    color: var(--text-secondary);
  }
</style>
