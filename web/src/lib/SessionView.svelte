<script>
  import { getSession, formatTs, formatNumber } from './api.js'
  import SourceBadge from './SourceBadge.svelte'

  let { sessionId, source = null, onclose } = $props()

  let data = $state(null)
  let loading = $state(true)
  let error = $state('')
  let thinking = $state(false)

  async function load() {
    loading = true
    error = ''
    try {
      data = await getSession({ id: sessionId, source, limit: 500, thinking: thinking ? 1 : '' })
    } catch (e) {
      error = e.message
      data = null
    } finally {
      loading = false
    }
  }

  function onkeydown(e) {
    if (e.key === 'Escape') onclose()
  }

  $effect(() => {
    if (sessionId) load()
  })
</script>

<svelte:window on:keydown={onkeydown} />

<div
  class="backdrop"
  role="button"
  tabindex="-1"
  aria-label="閉じる"
  onclick={onclose}
  onkeydown={() => {}}
></div>

<aside class="panel" aria-label="会話の本文">
  <header>
    <div class="head">
      {#if data}
        <SourceBadge source={data.session.source} />
        <h2>{data.session.title || '（無題）'}</h2>
      {:else}
        <h2>読み込み中…</h2>
      {/if}
    </div>
    <button type="button" class="close" onclick={onclose} aria-label="閉じる">✕</button>
  </header>

  {#if data}
    <dl class="info">
      {#if data.session.cwd}
        <div><dt>パス</dt><dd class="mono">{data.session.cwd}</dd></div>
      {/if}
      {#if data.session.git_branch}
        <div><dt>ブランチ</dt><dd class="mono">{data.session.git_branch}</dd></div>
      {/if}
      {#if data.session.host}
        <div><dt>ホスト</dt><dd>{data.session.host}</dd></div>
      {/if}
      <div><dt>期間</dt><dd>{formatTs(data.session.started_at)} 〜 {formatTs(data.session.ended_at)}</dd></div>
      <div><dt>件数</dt><dd>{formatNumber(data.total)} 件を表示中（全 {formatNumber(data.session.messages)} 件）</dd></div>
    </dl>

    <label class="toggle">
      <input type="checkbox" bind:checked={thinking} onchange={load} />
      思考ブロックも表示する
    </label>
  {/if}

  <div class="body">
    {#if error}
      <p class="msg error">{error}</p>
    {:else if loading}
      <p class="msg">読み込み中…</p>
    {:else if data?.messages.length}
      {#each data.messages as m, i (i)}
        <article class="turn role-{m.role}" class:thinking={m.kind === 'thinking'}>
          <div class="turnmeta">
            <span class="role">{m.role}</span>
            {#if m.kind !== 'message'}<span class="kind">{m.kind}</span>{/if}
            {#if m.model}<span class="model">{m.model}</span>{/if}
            <span class="ts">{formatTs(m.ts)}</span>
          </div>
          <div class="text">{m.text}</div>
        </article>
      {/each}
      {#if data.has_more}
        <p class="msg">この先は省略されています（表示上限に達しました）。</p>
      {/if}
    {:else}
      <p class="msg empty">本文がありません。</p>
    {/if}
  </div>
</aside>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    border: none;
    z-index: 10;
  }

  .panel {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(880px, 100vw);
    display: flex;
    flex-direction: column;
    background: var(--surface-1);
    border-left: 1px solid var(--border);
    z-index: 11;
  }

  header {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--gridline);
  }

  .head {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    flex: 1;
    min-width: 0;
  }

  h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .close {
    border: 1px solid var(--border);
    background: var(--surface-1);
    color: var(--text-secondary);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    flex: none;
  }

  .close:hover {
    background: var(--surface-3);
  }

  .info {
    margin: 0;
    padding: 12px 16px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px 22px;
    border-bottom: 1px solid var(--gridline);
    font-size: 12px;
  }

  .info > div {
    display: flex;
    gap: 7px;
    min-width: 0;
  }

  dt {
    color: var(--text-muted);
    flex: none;
  }

  dd {
    margin: 0;
    color: var(--text-secondary);
    overflow-wrap: anywhere;
  }

  .mono {
    font-family: var(--mono);
  }

  .toggle {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 9px 16px;
    font-size: 12px;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--gridline);
  }

  .body {
    flex: 1;
    overflow-y: auto;
    padding: 14px 16px 40px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .turn {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 13px;
    background: var(--surface-1);
  }

  .turn.role-user {
    background: var(--surface-3);
  }

  .turn.thinking {
    border-style: dashed;
    opacity: 0.85;
  }

  .turnmeta {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    font-size: 11px;
    color: var(--text-muted);
    margin-bottom: 6px;
  }

  .role {
    font-family: var(--mono);
    font-weight: 600;
    color: var(--text-secondary);
  }

  .kind,
  .model {
    font-family: var(--mono);
  }

  .ts {
    margin-left: auto;
    font-variant-numeric: tabular-nums;
  }

  .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-primary);
  }

  .msg {
    color: var(--text-secondary);
    font-size: 13px;
  }

  .msg.error {
    color: #d03b3b;
  }

  .msg.empty {
    text-align: center;
    padding: 40px 0;
    color: var(--text-muted);
  }
</style>
