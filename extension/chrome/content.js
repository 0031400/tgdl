// 在 Telegram Web 每条消息下面加一行 `chat_id:message_id` + 复制按钮。
//
// 位置刻意放在**气泡外面**，而不是塞进气泡里：
//   /a（新版 React）
//     <div class="Message" data-message-id="6733">
//       <div class="message-content-wrapper">      ← 挂这里，气泡的下方
//         <div class="message-content">…气泡…</div>
//       </div>
//     </div>
//   /k（旧版 webk）
//     <div class="bubble" data-mid="6733">…</div>  ← 用 insertAdjacentElement 插到它后面
//
// chat_id 从 URL hash 取：`#-1001234567890`（频道/群）、`#123456789`（私聊）。
(function () {
  'use strict';

  const ROW = 'tgdl-idrow';
  const MSG_SEL = '.Message[data-message-id], .message[data-mid], .bubble[data-mid]';

  // ---------- 取值 ----------
  function chatIdFromHash() {
    const raw = decodeURIComponent(location.hash.replace(/^#/, '')).trim();
    if (!raw) return null;
    const head = raw.split('/')[0].split('?')[0];   // 兼容 #-100xxx/123 这种写法
    if (/^-?\d+$/.test(head)) return head;          // 数字 id，tgdl 直接能用
    if (head.startsWith('@')) return head;          // @用户名，tgdl 用不了，标黄提示
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
      wrap.appendChild(row);                    // /a：气泡下方
      return row;
    }
    if (el.classList.contains('bubble') && el.insertAdjacentElement) {
      el.insertAdjacentElement('afterend', row); // /k：气泡后面
      return row;
    }
    el.appendChild(row);                        // 兜底
    return row;
  }

  // ---------- 复制 ----------
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      /* 有些情况下 clipboard API 会被拒，退回老办法 */
    }
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

    // 点这一行不要冒泡到 Telegram 的消息处理器（否则会触发选中/回复）
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
    scan();
    // 新消息、切换会话、滚动加载都会改 DOM
    new MutationObserver(schedule).observe(document.body, {
      childList: true,
      subtree: true,
    });
    // 兜底：有些更新只改属性，MutationObserver 不一定覆盖到
    setInterval(schedule, 1500);
    window.addEventListener('hashchange', schedule);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
