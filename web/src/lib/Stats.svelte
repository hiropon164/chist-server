<script>
  import { getStats, formatTs, formatNumber, sourceLabel } from './api.js'

  let data = $state(null)
  let loading = $state(true)
  let error = $state('')

  // A single series compared by magnitude, so one sequential blue.
  // Row labels carry identity; no categorical palette here.
  const maxOf = (rows) => Math.max(1, ...rows.map((r) => r.n))

  async function load() {
    loading = true
    error = ''
    try {
      data = await getStats({ projects: 15 })
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
  {#if error}
    <p class="msg error">{error}</p>
  {:else if loading}
    <p class="msg">Loading…</p>
  {:else if data}
    <div class="tiles">
      <div class="tile">
        <div class="tile-label">Messages</div>
        <div class="tile-value">{formatNumber(data.totals.messages)}</div>
      </div>
      <div class="tile">
        <div class="tile-label">Sessions</div>
        <div class="tile-value">{formatNumber(data.totals.sessions)}</div>
      </div>
      <div class="tile">
        <div class="tile-label">Machines</div>
        <div class="tile-value">{formatNumber(data.totals.hosts)}</div>
      </div>
    </div>

    <div class="card">
      <h3>Messages by source</h3>
      {#if data.sources.length}
        {@const max = maxOf(data.sources)}
        <div class="chart">
          {#each data.sources as row (row.source)}
            <div class="bar-row">
              <div class="bar-label" title={row.source}>{sourceLabel(row.source)}</div>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  style="width: {(row.n / max) * 100}%"
                  title="{formatNumber(row.n)}"
                ></div>
              </div>
              <div class="bar-value">{formatNumber(row.n)}</div>
              <div class="bar-sub">{formatTs(row.first)} – {formatTs(row.last)}</div>
            </div>
          {/each}
        </div>
      {:else}
        <p class="msg empty">No data.</p>
      {/if}
    </div>

    <div class="card">
      <h3>Messages by project (top {data.projects.length})</h3>
      {#if data.projects.length}
        {@const max = maxOf(data.projects)}
        <div class="chart">
          {#each data.projects as row (row.project_key)}
            <div class="bar-row wide">
              <div class="bar-label" title={row.project_key}>{row.project_key}</div>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  style="width: {(row.n / max) * 100}%"
                  title="{formatNumber(row.n)}"
                ></div>
              </div>
              <div class="bar-value">{formatNumber(row.n)}</div>
              <div class="bar-sub">{row.hosts} machines / {row.tools} tools</div>
            </div>
          {/each}
        </div>
      {:else}
        <p class="msg empty">
          project_key is not set. Run <code>chist backfill</code> on each machine.
        </p>
      {/if}
    </div>

    <div class="card">
      <h3>Machines</h3>
      {#if data.hosts.length}
        <div class="tablewrap">
          <table>
            <thead>
              <tr><th>Machine</th><th>OS</th><th>Last push</th></tr>
            </thead>
            <tbody>
              {#each data.hosts as h (h.host)}
                <tr>
                  <td>{h.host}</td>
                  <td>{h.os || '—'}</td>
                  <td class="ts">{formatTs(h.last_seen)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="msg empty">No machine has pushed yet.</p>
      {/if}
    </div>
  {/if}
</section>

<style>
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 10px;
    margin-bottom: 14px;
  }

  .tile {
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface-1);
  }

  .tile-label {
    font-size: 12px;
    color: var(--text-muted);
  }

  .tile-value {
    font-size: 30px;
    font-weight: 600;
    line-height: 1.2;
    margin-top: 2px;
  }

  .card {
    padding: 14px 16px 16px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface-1);
    margin-bottom: 14px;
  }

  h3 {
    margin: 0 0 12px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .chart {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .bar-row {
    display: grid;
    grid-template-columns: 150px 1fr auto;
    grid-template-areas: 'label track value' '. sub sub';
    align-items: center;
    gap: 2px 12px;
  }

  .bar-row.wide {
    grid-template-columns: 240px 1fr auto;
  }

  .bar-label {
    grid-area: label;
    font-size: 13px;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .bar-track {
    grid-area: track;
    height: 12px;
    border-radius: 2px;
    background: var(--bar-track);
    overflow: hidden;
  }

  /* Round only the data end at 4px; the baseline end stays square. */
  .bar-fill {
    height: 100%;
    background: var(--bar);
    border-radius: 0 4px 4px 0;
    min-width: 3px;
  }

  .bar-value {
    grid-area: value;
    font-size: 13px;
    font-variant-numeric: tabular-nums;
    color: var(--text-primary);
  }

  .bar-sub {
    grid-area: sub;
    font-size: 11px;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }

  .tablewrap {
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  th {
    text-align: left;
    padding: 7px 10px;
    color: var(--text-muted);
    font-weight: 500;
    font-size: 12px;
    border-bottom: 1px solid var(--gridline);
  }

  td {
    padding: 7px 10px;
    border-bottom: 1px solid var(--gridline);
  }

  tbody tr:last-child td {
    border-bottom: none;
  }

  .ts {
    font-variant-numeric: tabular-nums;
    color: var(--text-secondary);
  }

  code {
    font-family: var(--mono);
    background: var(--surface-3);
    padding: 1px 5px;
    border-radius: 4px;
  }

  .msg {
    color: var(--text-secondary);
    font-size: 13px;
  }

  .msg.error {
    color: #d03b3b;
  }

  .msg.empty {
    color: var(--text-muted);
  }

  @media (max-width: 640px) {
    .bar-row,
    .bar-row.wide {
      grid-template-columns: 1fr auto;
      grid-template-areas: 'label value' 'track track' 'sub sub';
    }
  }
</style>
