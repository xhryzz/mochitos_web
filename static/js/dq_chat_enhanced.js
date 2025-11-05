
/*! mochitos dq chat & reactions enhancement */
(function(){
  const qInput = document.querySelector('input[name="question_id"]');
  const QID = qInput ? parseInt(qInput.value, 10) : null;
  const USER = document.querySelector('meta[name="mochitos-user"]')?.getAttribute('content') || document.querySelector('#nav-username')?.textContent?.trim() || null;

  // Inject minimal CSS upgrades (scoped)
  const style = document.createElement('style');
  style.textContent = `
  .dq-chat-messages{scroll-behavior:smooth}
  .dq-chat-msg{animation:popIn .12s ease-out}
  .dq-typing{font-size:.85rem;color:#6b7280;margin-top:6px;display:none}
  .dq-typing.active{display:block}
  .dq-chat-bubble{position:relative}
  .dq-chat-bubble.me::after{content:"";position:absolute;right:-6px;top:10px;border:7px solid transparent;border-left-color:#dbeafe}
  .dq-chat-bubble.them::after{content:"";position:absolute;left:-6px;top:10px;border:7px solid transparent;border-right-color:#f3e8ff}
  .dq-chat-form button[disabled]{opacity:.6;cursor:not-allowed}
  .dq-reactions-buttons button{transition:transform .08s ease}
  .dq-reactions-buttons button:active{transform:scale(.9)}
  .dq-reactions-picker{display:none;position:absolute;z-index:10;background:#fff;border:1px solid #eee;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.12);padding:8px;gap:6px}
  .dq-reactions-picker.open{display:flex;flex-wrap:wrap;max-width:260px}
  @keyframes popIn{from{transform:translateY(6px);opacity:0}to{transform:none;opacity:1}}
  `;
  document.head.appendChild(style);

  const msgsWrap = document.querySelector('.dq-chat-messages');
  const form = document.querySelector('form.dq-chat-form');
  const input = form?.querySelector('input[name="msg"]');
  const reactForm = document.querySelector('form.dq-reactions-form');
  const typingEl = document.createElement('div');
  typingEl.className = 'dq-typing';
  typingEl.textContent = 'Escribiendo…';
  if (msgsWrap && msgsWrap.parentElement) msgsWrap.parentElement.appendChild(typingEl);

  // SSE
  let es;
  function sseConnect(){
    try{
      es = new EventSource('/events');
      es.addEventListener('dq_chat', ev => {
        try{
          const d = JSON.parse(ev.data || '{}');
          if (!d || d.q !== QID) return;
          if (d.user === USER) return; // my optimistic render already added it
          appendMessage(d.user, d.msg, d.ts, /*mine*/false);
          autoscroll();
        }catch(e){}
      });
      es.addEventListener('dq_reaction', ev => {
        try{
          const d = JSON.parse(ev.data || '{}');
          if (!d || d.q !== QID) return;
          syncReactionUI(d);
        }catch(e){}
      });
      es.addEventListener('dq_typing', ev => {
        try{
          const d = JSON.parse(ev.data || '{}');
          if (!d || d.q !== QID || d.from === USER) return;
          typingEl.classList.add('active');
          clearTimeout(typingEl._t);
          typingEl._t = setTimeout(()=> typingEl.classList.remove('active'), 1800);
        }catch(e){}
      });
      es.addEventListener('dq_seen', ev => {
        // Could show "visto" indicators if needed
      });
      es.onerror = () => {}; // keep it quiet
    }catch(e){}
  }
  if (!!window.EventSource) sseConnect();

  // Helpers
  function escapeHtml(s){ return s.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
  function fmt(ts){ return ts || new Date().toLocaleString(); }
  function msgEl(user, msg, ts, mine){
    const wrap = document.createElement('div');
    wrap.className = 'dq-chat-msg ' + (mine?'me':'them');
    wrap.innerHTML = `
      <div class="dq-chat-meta"><b>${escapeHtml(user)}</b> <span class="ts">${escapeHtml(fmt(ts))}</span></div>
      <div class="dq-chat-bubble ${mine?'me':'them'}">${escapeHtml(msg)}</div>
    `;
    return wrap;
  }
  function appendMessage(user, msg, ts, mine){
    if (!msgsWrap) return;
    const atBottom = (msgsWrap.scrollTop + msgsWrap.clientHeight) >= (msgsWrap.scrollHeight - 8);
    msgsWrap.appendChild(msgEl(user, msg, ts, mine));
    if (atBottom) msgsWrap.scrollTop = msgsWrap.scrollHeight;
  }
  function autoscroll(){
    if (!msgsWrap) return;
    msgsWrap.scrollTop = msgsWrap.scrollHeight;
  }

  // Intercept chat submit (AJAX + optimistic UI)
  if (form && input){
    form.addEventListener('submit', async (ev)=>{
      ev.preventDefault();
      const text = (input.value || '').trim();
      if (!text) return;
      form.querySelector('button[type="submit"]').disabled = true;
      appendMessage(USER || 'Yo', text, new Date().toLocaleString(), true);
      autoscroll();
      try{
        const fd = new FormData(form);
        const res = await fetch('/dq/chat/send', {
          method:'POST',
          headers: {'Accept':'application/json'},
          body: fd
        });
        if (!res.ok){
          throw new Error('fail');
        }
        const j = await res.json();
        // ok
      }catch(e){
        // rollback visual? keep as-is but notify
        window.showToast?.('error','No se pudo enviar el mensaje');
      }finally{
        input.value = '';
        form.querySelector('button[type="submit"]').disabled = false;
        // mark seen after sending
        pingSeen();
      }
    });

    // Typing pings (throttled)
    let lastType = 0;
    input.addEventListener('input', ()=>{
      const now = Date.now();
      if (now - lastType < 1200) return;
      lastType = now;
      if (!QID) return;
      fetch('/dq/chat/typing', {
        method:'POST',
        headers:{'Content-Type':'application/json','Accept':'application/json'},
        body: JSON.stringify({question_id: QID})
      }).catch(()=>{});
    });
  }

  // Seen ping when chat comes into view or on scroll end
  function pingSeen(){
    if (!QID) return;
    fetch('/dq/chat/seen', {method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json'}, body: JSON.stringify({question_id: QID})}).catch(()=>{});
  }
  if (msgsWrap){
    const obs = new IntersectionObserver((entries)=>{
      if (entries.some(e=>e.isIntersecting)) pingSeen();
    }, {threshold:.3});
    obs.observe(msgsWrap);
    msgsWrap.addEventListener('scroll', ()=>{
      const atBottom = (msgsWrap.scrollTop + msgsWrap.clientHeight) >= (msgsWrap.scrollHeight - 6);
      if (atBottom) pingSeen();
    });
  }

  // Reactions: intercept buttons; add "más" picker
  if (reactForm){
    // add "más" button + picker
    const btnBar = reactForm.querySelector('.dq-reactions-buttons');
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'dq-react-more';
    more.textContent = '➕';
    more.setAttribute('aria-label','Más reacciones');
    const picker = document.createElement('div');
    picker.className = 'dq-reactions-picker';
    const emojis = ['😍','🥹','🥰','🙌','🔥','✨','😮','🤔','😴','👀','👏','🤍','💙','💜','💯','🥳'];
    emojis.forEach(e=>{
      const b = document.createElement('button');
      b.type='button'; b.textContent = e; b.style.fontSize='1.15rem'; b.style.lineHeight='1';
      b.addEventListener('click', ()=> submitReaction(e));
      picker.appendChild(b);
    });
    btnBar.appendChild(more);
    reactForm.style.position='relative';
    reactForm.appendChild(picker);
    more.addEventListener('click', ()=>{
      picker.classList.toggle('open');
      const r = more.getBoundingClientRect();
      picker.style.left = (more.offsetLeft-4)+'px';
      picker.style.top = (more.offsetTop + more.offsetHeight + 6)+'px';
    });

    // existing buttons
    btnBar.querySelectorAll('button[name="reaction"]').forEach(b=>{
      b.addEventListener('click', (ev)=>{
        ev.preventDefault();
        submitReaction(b.value);
      });
    });

    async function submitReaction(emoji){
      try{
        const fd = new FormData(reactForm);
        fd.set('reaction', emoji);
        const res = await fetch('/dq/react', {method:'POST', headers:{'Accept':'application/json'}, body: fd});
        if (!res.ok) throw new Error('fail');
        const j = await res.json();
        // Visual feedback
        highlightReaction(emoji);
        window.showToast?.('success','¡Reacción guardada!');
      }catch(e){
        window.showToast?.('error','No se pudo guardar la reacción');
      }finally{
        picker.classList.remove('open');
      }
    }

    function highlightReaction(emoji){
      btnBar.querySelectorAll('button[name="reaction"]').forEach(b=>{
        b.style.outline = (b.value === emoji) ? '2px solid var(--accent-color)' : 'none';
      });
    }

    function syncReactionUI(d){
      // Could show other user's reaction somewhere if there's an element for it
      // This keeps local highlight in sync when my reaction comes from other device
      if (d && d.from === USER) highlightReaction(d.reaction);
    }
    window.syncReactionUI = syncReactionUI;
  }

})();