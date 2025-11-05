(function () {
  // ====== Config / refs ======
  const QID   = Number(window.CURRENT_QUESTION_ID || 0);
  const UID   = Number(window.CURRENT_USER_ID || 0);
  const UNAME = (window.CURRENT_USERNAME || '').toString();

  const chatBtn     = document.getElementById('btn-open-chat');
  const historyBtn  = document.getElementById('btn-open-history');
  const chatModal   = document.getElementById('chat-modal');
  const historyModal= document.getElementById('history-modal');
  const closeChat   = chatModal?.querySelector('[data-close-chat]');
  const closeHist   = historyModal?.querySelector('[data-close-history]');
  const chatList    = document.getElementById('chat-list');
  const chatText    = document.getElementById('chat-text');
  const chatSend    = document.getElementById('chat-send');
  const badgeMe     = document.getElementById('reaction-badge-me');
  const badgeThem   = document.getElementById('reaction-badge-them');

  if (!chatList) return; // nada que hacer

  // ====== Socket (WS -> fallback polling) ======
  const socket = io('/chat', {
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 500,
    reconnectionDelayMax: 5000,
  });

  let currentQid   = QID;
  let myLastSeenId = 0;
  let seenTimer    = null;
  const seenIds    = new Set(); // evita dibujar dos veces mismo id

  // ====== Helpers ======
  const open  = el => el && el.classList.remove('hidden');
  const close = el => el && el.classList.add('hidden');

  function escapeHtml(s){
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function atBottom(node, tolerance=60){
    return (node.scrollTop + node.clientHeight) >= (node.scrollHeight - tolerance);
  }
  function scrollBottom(force=false){
    if (!chatList) return;
    if (force || atBottom(chatList)) {
      chatList.scrollTop = chatList.scrollHeight;
    }
  }

  function scheduleSeen(){
    if (seenTimer) clearTimeout(seenTimer);
    if (myLastSeenId <= 0) return;
    // Debounce corto para agrupar avisos
    seenTimer = setTimeout(() => {
      socket.emit('mark_seen', {
        question_id: currentQid,
        user_id: UID,
        username: UNAME,
        last_message_id: myLastSeenId
      });
    }, 220);
  }

  // ====== Render ======
  function renderMessage(m){
    if (!m || seenIds.has(m.id)) return; // evita duplicados
    seenIds.add(m.id);

    const mine = Number(m.sender_id) === UID;
    const wrap = document.createElement('div');
    wrap.className = `msg ${mine? 'mine':'other'} ${m.is_deleted? 'deleted':''}`;
    wrap.dataset.id = String(m.id);

    const textHtml = m.is_deleted ? '<i>Mensaje eliminado</i>' : escapeHtml(m.text || '');
    const edited   = m.edited_at ? ' <span class="edited" aria-label="editado">(editado)</span>' : '';
    const whenMs   = (typeof m.created_at === 'number')
      ? m.created_at
      : Date.parse(m.created_at || '') || Date.now();
    const time = new Date(whenMs).toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });

    wrap.innerHTML = `
      <div class="msg-bubble" role="group" aria-label="${mine?'Mensaje enviado':'Mensaje recibido'}">
        <div class="msg-text">${textHtml}${edited}</div>
        <div class="msg-meta">
          <span class="time">${time}</span>
          <span class="ticks" data-ticks aria-label="${mine?'Estado de entrega':''}"></span>
          ${mine && !m.is_deleted ? `
            <div class="msg-actions">
              <button class="edit" aria-label="Editar">Editar</button>
              <button class="del"  aria-label="Borrar">Borrar</button>
            </div>` : ``}
        </div>
      </div>
    `;

    // Ticks iniciales: ✓ si es mío (✓✓ llegará por seen:update del OTRO)
    const ticks = wrap.querySelector('[data-ticks]');
    if (mine) ticks.textContent = '✓';

    // Acciones (solo míos no borrados)
    if (mine && !m.is_deleted) {
      wrap.querySelector('.edit')?.addEventListener('click', ()=>{
        const oldText = m.text || '';
        const newText = prompt('Editar mensaje:', oldText);
        if (!newText || newText.trim() === oldText) return;
        socket.emit('edit_message', {
          message_id: m.id, user_id: UID, username: UNAME, text: newText.trim()
        });
      });
      wrap.querySelector('.del')?.addEventListener('click', ()=>{
        if (!confirm('¿Eliminar este mensaje?')) return;
        socket.emit('delete_message', {
          message_id: m.id, user_id: UID, username: UNAME
        });
      });
    }

    const shouldStick = atBottom(chatList); // respeta lectura actual
    chatList.appendChild(wrap);
    if (shouldStick) scrollBottom(true);
  }

  async function fetchInitial(){
    try {
      const r = await fetch(`/api/chat/${currentQid}`, { headers:{'Accept':'application/json'} });
      const data = await r.json().catch(()=>({messages:[]}));
      chatList.innerHTML = '';
      seenIds.clear();
      (data.messages || []).forEach(renderMessage);
      myLastSeenId = Math.max(0, ...((data.messages || []).map(m=>m.id)));
      scheduleSeen();
      scrollBottom(true);
    } catch (e) {
      chatList.innerHTML = '<p style="opacity:.7">No se pudo cargar el chat.</p>';
    }
  }

  // ====== Socket events ======
  socket.on('connect', ()=>{
    socket.emit('join', { question_id: currentQid, user_id: UID, username: UNAME });
  });
  socket.on('reconnect', ()=>{
    socket.emit('join', { question_id: currentQid, user_id: UID, username: UNAME });
    scheduleSeen();
  });
  socket.on('connect_error', ()=>{ /* opcional: showToast?.('error','Chat sin conexión'); */ });

  socket.on('message:new', (m)=>{
    if (m.question_id !== currentQid) return;
    renderMessage(m);
    myLastSeenId = Math.max(myLastSeenId, m.id || 0);
    scheduleSeen();
  });

  socket.on('message:edited', ({id, text, edited_at})=>{
    const el = chatList.querySelector(`.msg[data-id="${id}"]`);
    if (!el) return;
    const txt = el.querySelector('.msg-text');
    if (txt) {
      txt.textContent = text || '';
      const edited = el.querySelector('.edited');
      if (edited) edited.textContent = '(editado)';
      else txt.insertAdjacentHTML('beforeend',' <span class="edited">(editado)</span>');
    }
  });

  socket.on('message:deleted', ({id})=>{
    const el = chatList.querySelector(`.msg[data-id="${id}"]`);
    if (!el) return;
    el.classList.add('deleted');
    const t = el.querySelector('.msg-text');
    if (t) t.innerHTML = '<i>Mensaje eliminado</i>';
    el.querySelector('.msg-actions')?.remove();
  });

  socket.on('seen:update', ({user_id, last_message_id})=>{
    if (Number(user_id) === UID) return; // solo me interesa lo visto por la otra persona
    chatList.querySelectorAll('.msg.mine').forEach(el=>{
      const mid = Number(el.dataset.id || 0);
      if (mid && mid <= (last_message_id || 0)) {
        const ticks = el.querySelector('[data-ticks]');
        if (ticks) ticks.textContent = '✓✓';
      }
    });
  });

  socket.on('reaction:update', ({question_id, user_id, emoji})=>{
    if (question_id !== currentQid) return;
    if (Number(user_id) === UID) {
      if (badgeThem) { badgeThem.textContent = emoji; badgeThem.classList.add('show'); }
      document.querySelector('.dq-reaction-current .big-reaction')?.textContent = emoji;
    } else {
      if (badgeMe) { badgeMe.textContent = emoji; badgeMe.classList.add('show'); }
      document.querySelector('.dq-reaction-partner .big-reaction')?.textContent = emoji;
    }
  });

  // ====== UI ======
  function openModal(m){
    open(m);
    // rejoin por si cambió el QID en historia
    socket.emit('join', { question_id: currentQid, user_id: UID, username: UNAME });
    fetchInitial().then(()=> chatText?.focus());
  }
  function closeModal(m){ close(m); }

  chatBtn?.addEventListener('click', ()=> openModal(chatModal));
  closeChat?.addEventListener('click', ()=> closeModal(chatModal));
  chatModal?.addEventListener('click', (e)=>{
    if (e.target.classList.contains('modal-backdrop')) closeModal(chatModal);
  });

  historyBtn?.addEventListener('click', ()=>{
    open(historyModal);
    const hx = document.getElementById('history-list');
    if (!hx) return;
    hx.innerHTML = '<div class="loading">Cargando…</div>';
    fetch(`/api/history?days=30`).then(r=>r.json()).then(data=>{
      hx.innerHTML = (data.questions || []).map(q=>`
        <div class="hist-item">
          <div class="day">${q.day ? new Date(q.day).toLocaleDateString() : ''}</div>
          <div class="title">${escapeHtml(q.title || q.body || '')}</div>
          <button class="open-chat" data-qid="${q.id}">Abrir chat</button>
        </div>
      `).join('');
      hx.querySelectorAll('.open-chat').forEach(b=>{
        b.addEventListener('click', ()=>{
          currentQid = Number(b.dataset.qid);
          close(historyModal);
          openModal(chatModal);
        });
      });
    }).catch(()=>{ hx.innerHTML = '<p style="opacity:.7">No se pudo cargar el historial.</p>'; });
  });
  closeHist?.addEventListener('click', ()=> close(historyModal));

  // Enviar mensaje
  function sendNow(){
    const t = (chatText.value || '').trim();
    if (!t) return;
    chatSend.disabled = true;
    socket.emit('send_message', {
      question_id: currentQid,
      user_id: UID,
      username: UNAME,
      text: t
    });
    chatText.value = '';
    chatSend.disabled = false;
  }
  chatSend?.addEventListener('click', sendNow);
  chatText?.addEventListener('keydown', (e)=>{
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendNow(); }
  });

  // Exponer reacción para los botones que ya tienes en el HTML
  window.setQuestionReaction = function(emoji){
    fetch(`/api/reaction/${currentQid}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({emoji})
    }).catch(()=>{ /* sin ruido */ });
  };

  // Carga inicial si ya está visible
  if (!chatModal?.classList.contains('hidden')) fetchInitial();
})();
