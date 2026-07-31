<script>
  import { sourceLabel, sourceSlot } from './api.js'

  let { source } = $props()
</script>

<!-- 色は補助。ラベルを必ず併記して、色単独で識別させない -->
<span class="badge slot-{sourceSlot(source)}">
  <span class="dot" aria-hidden="true"></span>{sourceLabel(source)}
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 1px 8px 1px 6px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface-3);
    color: var(--text-secondary);
    font-size: 12px;
    white-space: nowrap;
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: none;
    background: var(--series-1);
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
</style>
