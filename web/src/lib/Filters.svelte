<script>
  import { SOURCES } from './api.js'

  let { filters = $bindable(), showRole = false, showTarget = false, hosts = [], onapply } = $props()

  function toggleSource(id) {
    filters.sources = filters.sources.includes(id)
      ? filters.sources.filter((s) => s !== id)
      : [...filters.sources, id]
    onapply?.()
  }

  function toggleHost(name) {
    filters.hosts = filters.hosts.includes(name)
      ? filters.hosts.filter((h) => h !== name)
      : [...filters.hosts, name]
    onapply?.()
  }
</script>

<div class="filters">
  <div class="sources" role="group" aria-label="Filter by source">
    {#each SOURCES as s (s.id)}
      <button
        type="button"
        class="chip slot-{s.slot}"
        class:on={filters.sources.includes(s.id)}
        aria-pressed={filters.sources.includes(s.id)}
        onclick={() => toggleSource(s.id)}
      >
        <span class="dot" aria-hidden="true"></span>{s.label}
      </button>
    {/each}
  </div>

  <!-- Pointless to filter with only one machine, so hide it -->
  {#if hosts.length > 1}
    <div class="sources" role="group" aria-label="Filter by machine">
      {#each hosts as h (h)}
        <button
          type="button"
          class="chip host"
          class:on={filters.hosts.includes(h)}
          aria-pressed={filters.hosts.includes(h)}
          onclick={() => toggleHost(h)}
        >
          {h}
        </button>
      {/each}
    </div>
  {/if}

  <div class="row">
    <label>
      <span>Project</span>
      <input
        type="search"
        placeholder="part of the path"
        bind:value={filters.cwd}
        onchange={() => onapply?.()}
      />
    </label>

    {#if showRole}
      <label>
        <span>Role</span>
        <select bind:value={filters.role} onchange={() => onapply?.()}>
          <option value="">All</option>
          <option value="user">user</option>
          <option value="assistant">assistant</option>
          <option value="system">system</option>
          <option value="tool">tool</option>
        </select>
      </label>
    {/if}

    {#if showTarget}
      <label>
        <span>Search in</span>
        <select bind:value={filters.target} onchange={() => onapply?.()}>
          <option value="">Search database</option>
          <option value="record">Master index</option>
        </select>
      </label>
    {/if}

    <label>
      <span>From</span>
      <input type="date" bind:value={filters.since} onchange={() => onapply?.()} />
    </label>

    {#if showRole}
      <label>
        <span>To</span>
        <input type="date" bind:value={filters.until} onchange={() => onapply?.()} />
      </label>
    {/if}

    <label>
      <span>Limit</span>
      <select bind:value={filters.limit} onchange={() => onapply?.()}>
        <option value={20}>20</option>
        <option value={30}>30</option>
        <option value={50}>50</option>
        <option value={100}>100</option>
        <option value={200}>200</option>
      </select>
    </label>
  </div>
</div>

<style>
  .filters {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .sources {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px 4px 9px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface-1);
    color: var(--text-secondary);
    font-size: 13px;
    transition: background 0.12s ease;
  }

  .chip:hover {
    background: var(--surface-3);
  }

  .chip.on {
    background: var(--surface-3);
    border-color: var(--baseline);
    color: var(--text-primary);
    font-weight: 600;
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: none;
    background: var(--series-1);
    opacity: 0.35;
  }

  .chip.on .dot {
    opacity: 1;
  }

  .slot-2 .dot {
    background: var(--series-2);
  }
  .slot-3 .dot {
    background: var(--series-3);
  }
  .slot-4 .dot {
    background: var(--series-4);
  }

  /* Machines are not colour-coded: the set changes. Distinguish them by typeface. */
  .chip.host {
    padding: 4px 12px;
    font-family: var(--mono);
    font-size: 12px;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }

  label {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 12px;
    color: var(--text-muted);
  }

  input,
  select {
    padding: 6px 9px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--surface-1);
    color: var(--text-primary);
    min-width: 130px;
  }
</style>
