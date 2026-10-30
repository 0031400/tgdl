// ==UserScript==
// @name         tgdl 消息 ID
// @namespace    tgdl
// @version      1.1.0
// @description  在 Telegram Web 每条消息下面（气泡外）显示 chat_id:message_id，右边一个复制按钮
// @match        https://web.telegram.org/a/*
// @match        https://web.telegram.org/k/*
// @match        https://web.telegram.org/z/*
// @run-at       document-idle
// @noframes
// ==/UserScript==

// 位置刻意放在**气泡外面**：
//   /a  挂到 .message-content-wrapper（气泡的下方）
//   /k  用 insertAdjacentElement 插到 .bubble 后面
// chat_id 从 URL hash 取：`#-1001234567890`（频道/群）、`#123456789`（私聊）。

(function () {
  'use strict';

  const ROW = 'tgdl-idrow';
  const MSG_SEL = '.Message[data-message-id], .message[data-mid], .bubble[data-mid]';

  // ---------- 样式（油猴没有 content.css，只能自己插） ----------
  const CSS = `
  .tgdl-idrow {
    display: inline-flex; align-items: center; gap: 6px;
    margin: 3px 2px 0; padding: 0; max-width: 100%; box-sizing: border-box;
    font: 11px/1.5 ui-monospace, SFMono-Regular, Consolas, "Courier New", monospace;
    color: currentColor; opacity: .55; user-select: text; vertical-align: top;
  }
  .tgdl-idrow:hover { opacity: 1; }
  .tgdl-idrow code {
    padding: 1px 5px; border-radius: 4px;
    background: rgba(127,127,127,.16); white-space: nowrap; user-select: all;
  }
  .tgdl-idrow.warn code { background: rgba(220,140,0,.2); }
  .tgdl-idrow button {
    flex: none; padding: 1px 7px; border: 1px solid rgba(127,127,127,.45);
    border-radius: 4px; background: transparent; color: inherit;
    font: inherit; line-height: 1.4; cursor: pointer; opacity: .9;
  }
  .tgdl-idrow button:hover { background: rgba(127,127,127,.18); }
  .tgdl-idrow button.ok { border-color: rgba(30,160,90,.8); color: rgb(30,160,90); opacity: 1; }
  .tgdl-idrow button.bad { border-color: rgba(210,75,75,.8); color: rgb(210,75,75); opacity: 1; }
  `;

  function injectStyle() {
    if (document.getElementById('tgdl-id-style')) return;
    const style = document.createElement('style');
    style.id = 'tgdl-id-style';
    style.textContent = CSS;
    (document.head || document.documentElement).appendChild(style);
  }

  // ---------- 取值 ----------
  function chatIdFromHash() {
    const raw = decodeURIComponent(location.hash.replace(/^#/, '')).trim();
    if (!raw) return null;
    const head = raw.split('/')[0].split('?')[0];
    if (/^-?\d+$/.test(head)) return head;
    if (head.startsWith('@')) return head;
    return null;
  }

  function messageIdOf(el) {
    const v = el.getAttribute('data-message-id') || el.getAttribute('data-mid');
    return v && /^\d+$/.test(v) ? v : null;
  }

  // ---------- 找位置 ----------
  function wrapperOf(el) {
    return el.querySelector('.message-content-wrapper');
  }

  function findRow(el) {
    const wrap = wrapperOf(el);
    if (wrap) return wrap.querySelector(':scope > .' + ROW);
    const next = el.nextElementSibling;
    if (next && next.classList.contains(ROW)) return next;
    return el.querySelector(':scope > .' + ROW);
  }

  function ensureRow(el) {
    const exist = findRow(el);
    if (exist) return exist;

    const row = buildRow();
    const wrap = wrapperOf(el);
    if (wrap) {
      wrap.appendChild(row);                      // /a：气泡下方
      return row;
    }
    if (el.classList.contains('bubble') && el.insertAdjacentElement) {
      el.insertAdjacentElement('afterend', row);  // /k：气泡后面
      return row;
    }
    el.appendChild(row);                          // 兜底
    return row;
  }

  // ---------- 复制 ----------
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) { /* 退回老办法 */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:-2000px;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch (_) {
      return false;
    }
  }

  // ---------- 造这一行 ----------
  function buildRow() {
    const row = document.createElement('div');
    row.className = ROW;

    const code = document.createElement('code');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '复制';

    const swallow = (e) => e.stopPropagation();
    row.addEventListener('mousedown', swallow);
    row.addEventListener('click', swallow);
    row.addEventListener('dblclick', swallow);

    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const ok = await copyText(code.textContent);
      btn.textContent = ok ? '已复制' : '失败';
      btn.classList.toggle('ok', ok);
      btn.classList.toggle('bad', !ok);
      clearTimeout(btn._reset);
      btn._reset = setTimeout(() => {
        btn.textContent = '复制';
        btn.classList.remove('ok', 'bad');
      }, 1200);
    });

    row.append(code, btn);
    return row;
  }

  function paint(row, chat, mid) {
    const text = `${chat}:${mid}`;
    const code = row.querySelector('code');
    if (code.textContent !== text) code.textContent = text;
    const warn = chat.startsWith('@');
    row.classList.toggle('warn', warn);
    row.title = warn
      ? '当前 URL 是 @用户名 形式，拿不到数字 chat_id。点开左侧列表里的这个会话再复制。'
      : 'chat_id:message_id，点「复制」直接喂给 tgdl';
  }

  // ---------- 扫描注入 ----------
  function scan() {
    const chat = chatIdFromHash();
    if (!chat) return;

    for (const el of document.querySelectorAll(MSG_SEL)) {
      const mid = messageIdOf(el);
      if (!mid) continue;
      paint(ensureRow(el), chat, mid);
    }
  }

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      scan();
    });
  }

  // ---------- 启动 ----------
  function boot() {
    injectStyle();
    scan();
    new MutationObserver(schedule).observe(document.body, {
      childList: true,
      subtree: true,
    });
    setInterval(schedule, 1500);
    window.addEventListener('hashchange', schedule);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
