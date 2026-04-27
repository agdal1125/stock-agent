"""공통 앱 쉘 — 사이드바 + 레이아웃 스타일.

기존 HTML 응답에 최소 침투 방식으로 주입한다 (`inject_shell(html, active)`).
개별 페이지는 `<head>` + `<body>` 구조를 유지하고, 본 모듈이
  1) `<head>` 끝에 쉘 CSS 삽입
  2) `<body>` 시작 직후에 사이드바 + (Explorer 탭이면) 파일 트리 + 뷰 컨테이너 열기
  3) `</body>` 앞에 뷰 컨테이너 닫기
를 수행한다.
"""
from __future__ import annotations


TAB_CHAT = "chat"
TAB_EXPLORER = "explorer"
TAB_HOW = "how"
TAB_ADMIN = "admin"


_SHELL_STYLE = """<style>
  body { margin: 0; }
  .app-shell { display: flex; min-height: 100vh; background: #0f172a; }
  .sidebar {
    width: 232px; flex-shrink: 0;
    background: #0b1220; border-right: 1px solid #334155;
    padding: 22px 16px 18px;
    position: sticky; top: 0; height: 100vh;
    display: flex; flex-direction: column;
    font-family: -apple-system, "Pretendard", "Segoe UI", "Malgun Gothic",
                 "Apple SD Gothic Neo", sans-serif;
    z-index: 10;
  }
  .sidebar .brand {
    color: #e2e8f0; font-weight: 700; font-size: 15px;
    letter-spacing: -0.01em; margin-bottom: 4px;
  }
  .sidebar .brand a { color: inherit; text-decoration: none; }
  .sidebar .brand-sub {
    color: #64748b; font-size: 11px; margin-bottom: 22px; line-height: 1.5;
  }
  .sidebar nav { display: flex; flex-direction: column; gap: 2px; }
  .sidebar .nav-item {
    display: flex; align-items: center; gap: 10px;
    color: #cbd5e1; text-decoration: none; font-size: 13px;
    padding: 10px 12px; border-radius: 8px;
    transition: background .12s, color .12s;
    border-left: 2px solid transparent;
  }
  .sidebar .nav-item:hover { background: #1e293b; color: #fff; }
  .sidebar .nav-item.active {
    background: rgba(56,189,248,.12); color: #7dd3fc;
    border-left-color: #38bdf8;
  }
  .sidebar .nav-item .icon { font-size: 16px; line-height: 1; width: 20px; text-align: center; }
  .sidebar .nav-group-label {
    color: #475569; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.08em; padding: 14px 12px 6px;
  }
  .sidebar .spacer { flex: 1; }
  .sidebar .meter-box {
    color: #64748b; font-size: 11px;
    padding: 10px 12px; margin-top: 10px;
    border-top: 1px solid #1e293b;
  }
  .sidebar .meter-box .meter-line {
    color: #94a3b8; margin-bottom: 3px;
    display: flex; justify-content: space-between;
  }
  .sidebar .meter-box .meter-strong { color: #38bdf8; font-weight: 600; }
  .sidebar .meter-bar {
    height: 6px; background: #1e293b; border-radius: 999px; margin-top: 6px;
    overflow: hidden; position: relative;
  }
  .sidebar .meter-bar-fill {
    height: 100%; background: #22c55e;
    transition: width .4s ease, background-color .2s;
  }
  .sidebar .meter-bar-fill[data-state="warn"] { background: #fbbf24; }
  .sidebar .meter-bar-fill[data-state="over"] { background: #ef4444; }
  .sidebar .meter-bar-label {
    position: absolute; top: -15px; right: 0;
    font-size: 9.5px; color: #64748b;
  }
  .sidebar .kill-state {
    margin-top: 10px; font-size: 10.5px; padding: 4px 8px;
    border-radius: 6px; text-align: center; font-weight: 600;
  }
  .sidebar .kill-state.manual {
    background: rgba(251,191,36,.12); color: #fbbf24;
    border: 1px solid rgba(251,191,36,.35);
  }
  .sidebar .kill-state.over {
    background: rgba(239,68,68,.15); color: #fca5a5;
    border: 1px solid rgba(239,68,68,.4);
  }
  .sidebar .footer-links {
    margin-top: 8px; display: flex; gap: 8px; font-size: 10px;
  }
  .sidebar .footer-links a { color: #475569; text-decoration: none; }
  .sidebar .footer-links a:hover { color: #94a3b8; }

  .view { flex: 1; min-width: 0; }

  /* ---------- Explorer 전용 파일 트리 (2nd sidebar) ---------- */
  .explorer-tree {
    width: 260px; flex-shrink: 0;
    background: #0c1527; border-right: 1px solid #1e293b;
    padding: 18px 10px 18px 14px;
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
    font-family: -apple-system, "Pretendard", "Segoe UI", sans-serif;
    font-size: 13px;
  }
  .explorer-tree .tree-header {
    color: #64748b; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.1em; padding: 4px 8px 10px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .explorer-tree .tree-legend {
    font-size: 10px; color: #475569;
    padding: 4px 8px 10px; display: flex; gap: 8px;
  }
  .explorer-tree .tree-legend .dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    margin-right: 4px; vertical-align: 1px;
  }
  .explorer-tree .tree-legend .dot.ok { background: #22c55e; }
  .explorer-tree .tree-legend .dot.ghost { background: #475569; }
  .explorer-tree .tree-root {
    color: #64748b; font-size: 11px; padding: 2px 8px;
  }
  .explorer-tree details.ticker {
    margin: 1px 0; border-radius: 6px;
  }
  .explorer-tree details.ticker > summary {
    list-style: none; cursor: pointer; padding: 5px 8px;
    display: flex; align-items: center; gap: 8px;
    border-radius: 6px; color: #cbd5e1;
    border-left: 2px solid transparent;
  }
  .explorer-tree details.ticker > summary::-webkit-details-marker { display: none; }
  .explorer-tree details.ticker > summary::before {
    content: "▸"; color: #475569; font-size: 10px; width: 10px;
    transition: transform .1s;
  }
  .explorer-tree details.ticker[open] > summary::before { transform: rotate(90deg); }
  .explorer-tree details.ticker > summary:hover { background: #1e293b; color: #fff; }
  .explorer-tree details.ticker.active > summary {
    background: rgba(56,189,248,.12); color: #7dd3fc;
    border-left-color: #38bdf8;
  }
  .explorer-tree .ghost > summary {
    color: #64748b; font-style: italic;
  }
  .explorer-tree .ghost > summary:hover {
    background: rgba(251,191,36,.06); color: #fbbf24;
  }
  .explorer-tree .t-code {
    font-family: Consolas, Menlo, monospace; font-size: 11px;
    color: #38bdf8;
  }
  .explorer-tree .ghost .t-code { color: #64748b; }
  .explorer-tree .t-name { flex: 1; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .explorer-tree .t-status {
    font-size: 9px; color: #475569;
  }
  .explorer-tree .ghost .t-status {
    color: #f59e0b; background: rgba(245,158,11,.08);
    border-radius: 3px; padding: 1px 5px;
  }
  .explorer-tree .sec-list {
    list-style: none; margin: 0; padding: 0 0 4px 24px;
  }
  .explorer-tree .sec-list li a {
    display: block; padding: 3px 8px; margin: 1px 0;
    color: #94a3b8; text-decoration: none; border-radius: 4px;
    font-size: 11px; font-family: Consolas, Menlo, monospace;
  }
  .explorer-tree .sec-list li a:hover {
    background: #1e293b; color: #38bdf8;
  }
  .explorer-tree .tree-footer {
    padding: 10px 8px; margin-top: 10px;
    border-top: 1px solid #1e293b;
    font-size: 10px; color: #475569;
  }
  .explorer-tree .tree-footer a { color: #94a3b8; text-decoration: none; }
  .explorer-tree .tree-footer a:hover { color: #38bdf8; }
  .explorer-tree .tree-loading {
    padding: 10px; color: #64748b; font-size: 11px; text-align: center;
  }

  @media (max-width: 960px) {
    .explorer-tree {
      width: 200px; padding: 12px 6px 12px 10px;
    }
  }
  @media (max-width: 720px) {
    .app-shell { flex-direction: column; }
    .sidebar {
      width: auto; height: auto; position: relative; flex-direction: row;
      padding: 8px; align-items: center;
    }
    .sidebar .brand, .sidebar .brand-sub, .sidebar .meter-box,
    .sidebar .nav-group-label, .sidebar .footer-links { display: none; }
    .sidebar nav { flex-direction: row; gap: 2px; flex: 1; }
    .sidebar .nav-item { flex: 1; justify-content: center; font-size: 11px; padding: 8px 6px; }
    .sidebar .nav-item .icon { width: auto; }
    .explorer-tree {
      width: auto; height: auto; position: relative; max-height: 320px;
      border-right: 0; border-bottom: 1px solid #1e293b;
    }
  }
</style>"""


_EXPLORER_TREE_HTML = """<aside class="explorer-tree" id="explorerTree">
  <div class="tree-header">
    <span>📁 Wiki 탐색기</span>
  </div>
  <div class="tree-legend">
    <span><span class="dot ok"></span>컴파일됨</span>
    <span><span class="dot ghost"></span>ghost (클릭 시 생성)</span>
  </div>
  <div class="tree-root">wiki/</div>
  <div id="treeRoot" class="tree-body">
    <div class="tree-loading">불러오는 중…</div>
  </div>
  <div class="tree-footer">
    <a href="/wiki/">🏠 index.md</a> ·
    <a href="/wiki/AGENTS">AGENTS.md</a> ·
    <a href="/wiki/tags/">🏷️ 태그</a>
  </div>
</aside>
<script>
(function () {
  const root = document.getElementById('treeRoot');
  if (!root) return;
  const path = window.location.pathname;
  const m = path.match(/^\\/wiki\\/(\\d{6})/);
  const activeTicker = m ? m[1] : null;
  const activeHash = window.location.hash.replace('#sec-', '');

  function esc(s) {
    return String(s).replace(/[&<>"']/g,
      c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderNode(t) {
    const compiled = !!t.compiled;
    const isActive = activeTicker === t.ticker;
    const cls = 'ticker' + (compiled ? '' : ' ghost') + (isActive ? ' active' : '');
    const status = compiled
      ? `<span class="t-status">${t.sections.length}</span>`
      : `<span class="t-status" title="아직 컴파일되지 않음">ghost</span>`;
    let html = `<details class="${cls}" ${isActive ? 'open' : ''} data-ticker="${t.ticker}">
      <summary>
        <span class="t-code">${esc(t.ticker)}</span>
        <span class="t-name">${esc(t.name_ko)}</span>
        ${status}
      </summary>`;
    if (compiled && t.sections.length) {
      html += `<ul class="sec-list">`;
      for (const s of t.sections) {
        const selected = isActive && activeHash === s.section_type ? 'style="color:#38bdf8;background:#1e293b"' : '';
        html += `<li><a href="/wiki/${t.ticker}#sec-${s.section_type}" ${selected}>${esc(s.filename)}</a></li>`;
      }
      html += `</ul>`;
    } else {
      html += `<ul class="sec-list"><li><a href="/wiki/${t.ticker}" style="color:#f59e0b">→ 클릭해서 자동 생성</a></li></ul>`;
    }
    html += `</details>`;
    return html;
  }

  fetch('/wiki/api/tree.json')
    .then(r => r.json())
    .then(data => {
      const tickers = data.tickers || [];
      const compiled = tickers.filter(t => t.compiled);
      const ghosts = tickers.filter(t => !t.compiled);
      let html = '';
      if (compiled.length) {
        html += compiled.map(renderNode).join('');
      }
      if (ghosts.length) {
        html += `<div class="tree-header" style="padding-top:14px">미생성</div>`;
        html += ghosts.map(renderNode).join('');
      }
      root.innerHTML = html || '<div class="tree-loading">종목이 없습니다.</div>';

      // active 종목으로 자동 스크롤
      const act = root.querySelector('details.active');
      if (act) act.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    })
    .catch(err => {
      root.innerHTML = `<div class="tree-loading">트리 로드 실패: ${esc(err.message)}</div>`;
    });
})();
</script>"""


def _nav_item(tab: str, href: str, icon: str, label: str, active: str) -> str:
    cls = "nav-item active" if tab == active else "nav-item"
    return (f'<a class="{cls}" href="{href}" data-tab="{tab}">'
            f'<span class="icon">{icon}</span> {label}</a>')


_METER_SCRIPT = """<script>
(function() {
  function fmt(v) {
    if (v == null || isNaN(v)) return '—';
    return '$' + Number(v).toFixed(4);
  }
  async function refresh() {
    try {
      const r = await fetch('/cost');
      if (!r.ok) return;
      const j = await r.json();
      const tEl = document.getElementById('cost-today');
      const mEl = document.getElementById('cost-month');
      if (tEl) tEl.textContent = fmt(j.today_usd);
      if (mEl) mEl.textContent = fmt(j.month_usd);
      const bar = document.getElementById('costBar');
      const fill = document.getElementById('costBarFill');
      const label = document.getElementById('costBarLabel');
      if (bar && fill && label) {
        if (j.daily_cap_usd > 0) {
          const pct = Math.min(100, j.daily_pct_used || 0);
          bar.style.display = 'block';
          fill.style.width = pct + '%';
          fill.dataset.state = pct >= 100 ? 'over' : (pct >= 80 ? 'warn' : 'ok');
          label.textContent = 'cap $' + j.daily_cap_usd.toFixed(2) +
            ' · ' + pct.toFixed(0) + '%';
        } else {
          bar.style.display = 'none';
        }
      }
      const ks = document.getElementById('killState');
      if (ks) {
        if (j.over_cap) {
          ks.style.display = 'block';
          ks.className = 'kill-state over';
          ks.textContent = '🛑 일일 상한 초과 — LLM 차단';
        } else if (j.kill_switch) {
          ks.style.display = 'block';
          ks.className = 'kill-state manual';
          ks.textContent = '🔒 KILL_SWITCH on';
        } else {
          ks.style.display = 'none';
        }
      }
    } catch (_) { /* 무시 */ }
  }
  refresh();
  setInterval(refresh, 15000);
})();
</script>"""


def _sidebar_html(active: str) -> str:
    html = f"""<aside class="sidebar">
  <div class="brand"><a href="/">NH Stock-Agent</a></div>
  <div class="brand-sub">종목 Wiki를 활용한 범용 질의응답 서비스</div>
  <nav>
    {_nav_item(TAB_CHAT,     "/",            "💬", "질문",       active)}
    {_nav_item(TAB_EXPLORER, "/wiki/",       "📂", "Wiki 탐색",  active)}
    {_nav_item(TAB_HOW,      "/how",         "⚙️", "작동 원리",  active)}
    {_nav_item(TAB_ADMIN,    "/wiki/admin",  "🔐", "편집",       active)}
  </nav>
  <div class="spacer"></div>
  <div class="meter-box" id="meterBox">
    <div class="meter-line"><span>💸 today</span> <span class="meter-strong" id="cost-today">—</span></div>
    <div class="meter-line"><span>this month</span> <span class="meter-strong" id="cost-month">—</span></div>
    <div class="meter-bar" id="costBar" style="display:none">
      <div class="meter-bar-fill" id="costBarFill"></div>
      <span class="meter-bar-label" id="costBarLabel"></span>
    </div>
    <div class="kill-state" id="killState" style="display:none"></div>
    <div class="footer-links">
      <a href="/health">status</a>
      <a href="/cost">cost</a>
      <a href="/wiki/AGENTS">정책</a>
    </div>
  </div>
</aside>"""
    return html + _METER_SCRIPT


def inject_shell(html: str, active: str) -> str:
    """페이지의 전체 HTML 응답에 사이드바와 뷰 컨테이너를 주입."""
    sidebar = _sidebar_html(active)
    explorer_tree = _EXPLORER_TREE_HTML if active == TAB_EXPLORER else ""
    html = html.replace("</head>", _SHELL_STYLE + "</head>", 1)
    html = html.replace(
        "<body>",
        f'<body><div class="app-shell">{sidebar}{explorer_tree}<div class="view">',
        1,
    )
    html = html.replace("</body>", "</div></div></body>", 1)
    return html
