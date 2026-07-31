<script>
  import Search from './lib/Search.svelte'
  import Sessions from './lib/Sessions.svelte'
  import Stats from './lib/Stats.svelte'
  import SessionView from './lib/SessionView.svelte'
  import { fetchHosts } from './lib/api.js'

  const TABS = [
    { id: 'search', label: '検索' },
    { id: 'sessions', label: 'セッション' },
    { id: 'stats', label: '統計' },
  ]

  // ?q=… があれば検索タブを開いてそのまま実行する（リンク共有用）
  const initialQuery = new URLSearchParams(location.search).get('q') ?? ''
  const initialTab = location.hash.slice(1)
  let tab = $state(
    initialQuery ? 'search' : TABS.some((t) => t.id === initialTab) ? initialTab : 'search',
  )
  let openId = $state(null)
  let theme = $state(localStorage.getItem('chist-theme') || 'auto')
  // 送信元マシンの一覧は起動時に一度だけ取る（フィルタの選択肢に使う）
  let hosts = $state([])
  fetchHosts().then((h) => (hosts = h))

  // タブを URL に持たせる。リロードや共有で同じ画面に戻れる。
  $effect(() => {
    if (location.hash.slice(1) !== tab) history.replaceState(null, '', '#' + tab)
  })

  $effect(() => {
    const root = document.documentElement
    if (theme === 'auto') root.removeAttribute('data-theme')
    else root.dataset.theme = theme
    localStorage.setItem('chist-theme', theme)
  })

  function cycleTheme() {
    theme = theme === 'auto' ? 'light' : theme === 'light' ? 'dark' : 'auto'
  }

  const themeLabel = $derived(
    theme === 'auto' ? '自動' : theme === 'light' ? 'ライト' : 'ダーク',
  )
</script>

<header class="topbar">
  <div class="inner">
    <div class="brand">
      <strong>chist</strong>
      <span>CLI チャット履歴</span>
    </div>

    <nav>
      {#each TABS as t (t.id)}
        <button
          type="button"
          class="tab"
          class:on={tab === t.id}
          aria-current={tab === t.id ? 'page' : undefined}
          onclick={() => (tab = t.id)}
        >
          {t.label}
        </button>
      {/each}
    </nav>

    <button type="button" class="theme" onclick={cycleTheme} title="表示テーマを切り替える">
      テーマ: {themeLabel}
    </button>
  </div>
</header>

<main>
  {#if tab === 'search'}
    <Search {initialQuery} {hosts} onopen={(o) => (openId = o)} />
  {:else if tab === 'sessions'}
    <Sessions {hosts} onopen={(o) => (openId = o)} />
  {:else}
    <Stats />
  {/if}
</main>

{#if openId}
  <SessionView sessionId={openId.id} source={openId.source} onclose={() => (openId = null)} />
{/if}

<style>
  .topbar {
    position: sticky;
    top: 0;
    z-index: 5;
    background: var(--surface-1);
    border-bottom: 1px solid var(--gridline);
  }

  .inner {
    max-width: 1100px;
    margin: 0 auto;
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 18px;
    flex-wrap: wrap;
  }

  .brand {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }

  .brand strong {
    font-size: 17px;
    letter-spacing: 0.01em;
  }

  .brand span {
    font-size: 12px;
    color: var(--text-muted);
  }

  nav {
    display: flex;
    gap: 4px;
    margin-left: auto;
  }

  .tab {
    padding: 6px 14px;
    border-radius: 999px;
    border: 1px solid transparent;
    background: none;
    color: var(--text-secondary);
    font-size: 13px;
  }

  .tab:hover {
    background: var(--surface-3);
  }

  .tab.on {
    background: var(--surface-3);
    border-color: var(--border);
    color: var(--text-primary);
    font-weight: 600;
  }

  .theme {
    padding: 6px 12px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--surface-1);
    color: var(--text-secondary);
    font-size: 12px;
  }

  .theme:hover {
    background: var(--surface-3);
  }

  main {
    max-width: 1100px;
    margin: 0 auto;
    padding: 20px;
  }
</style>
