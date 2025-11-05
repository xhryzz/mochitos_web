(function(){
  const qid = window.CURRENT_QUESTION_ID;
  const uid = window.CURRENT_USER_ID;
  const uname = (window.CURRENT_USERNAME || '').toString();

  const chatBtn = document.getElementById('btn-open-chat');
  const historyBtn = document.getElementById('btn-open-history');
  const chatModal = document.getElementById('chat-modal');
  const historyModal = document.getElementById('history-modal');
  const closeChat = chatModal?.querySelector('[data-close-chat]');
  const closeHistory = historyModal?.querySelector('[data-close-history]');
  const chatList = document.getElementById('chat-list');
  const chatText = document.getElementById('chat-text');
  const chatSend = document.getElementById('chat-send');
  const badgeMe = document.getElementById('reaction-badge-me');
  const badgeThem = document.getElementById('reaction-badge-them');

  // Helpers UI
  const open = el => el?.classList.remove('hidden');
  const close = el => el?.classList.add('hidden');

  // Socket
  const socket = io('/chat', { transports: ['websocket', 'polling'] });

  // Último ID que YO he visto (sirve para notificar al servidor; los ✓✓ los marca cuando el OTRO manda seen:update)
  let myLastSeenId = 0;
  let currentQid = qid;

  function fetchInitial() {
    fetch(`/api/chat/${currentQid}`)
      .then(r=>r.json())
      .then(data=>{
        chatList.innerHTML = '';
        (data.messages || []).forEach(m=>renderMessage(m));
        // Lo que yo he visto hasta ahora no lo necesitamos para los ticks (los ✓✓ son del OTRO),
        // pero lo guardamos para enviar al servidor el "mark_seen".
        myLastSeenId = Math.max(0, ...((data.messages || []).map(m=>m.id)));
        markSeen();
        scrollBottom();
      });
  }

  function renderMessage(m){
    const mine = m.sender_id === uid;
    const li = document.createElement('div');
    li.className = `msg ${mine? 'mine':'other'} ${m.is_deleted? 'deleted':''}`;
    li.dataset.id = m.id;

    const textHtml = m.is_deleted ? '<i>Mensaje eliminado</i>' : escapeHtml(m.text || '');
    const edited = m.edited_at ? ' <span class="edited">(editado)</span>' : '';
    const time = new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

    li.innerHTML = `
      <div class="msg-bubble">
        <div class="msg-text">${textHtml}${edited}</div>
        <div class="msg-meta">
          <span class="time">${time}</span>
          <span class="ticks" data-ticks></span>
          ${mine && !m.is_deleted ? `
            <div class="msg-actions">
              <button class="edit" aria-label="Editar">Editar</button>
              <button class="del" aria-label="Borrar">Borrar</button>
            </div>` : ``}
        </div>
      </div>
    `;

    // Ticks:
    // - Al render inicial: si es mío, muestro ✓ (enviado). El ✓✓ lo pondremos cuando llegue seen:update del OTRO.
    // - Si no es mío, no muestro ticks.
    const ticks = li.querySelector('[data-ticks]');
    if (mine) {
      ticks.textContent = '✓';
    } else {
      ticks.textContent = '';
    }

    // Acciones
    if (mine && !m.is_deleted) {
      li.querySelector('.edit')?.addEventListener('click', ()=>{
        const oldText = m.text || '';
        const newText = prompt('Editar mensaje:', oldText);
        if (!newText || newText.trim()===oldText) return;
        socket.emit('edit_message', {
          message_id: m.id,
          user_id: uid,
          username: uname,
          text: newText.trim()
        });
      });
      li.querySelector('.del')?.addEventListener('click', ()=>{
        if (confirm('¿Eliminar este mensaje?')) {
          socket.emit('delete_message', {
            message_id: m.id,
            user_id: uid,
            username: uname
          });
        }
      });
    }

    chatList.appendChild(li);
  }

  function escapeHtml(s){
    const d = document.createElement('div');
    d.innerText = s; return d.innerHTML;
  }

  function scrollBottom(){
    chatList.scrollTop = chatList.scrollHeight;
  }

  function markSeen(){
    if (myLastSeenId > 0) {
      socket.emit('mark_seen', {
        question_id: currentQid,
        user_id: uid,
        username: uname,
        last_message_id: myLastSeenId
      });
    }
  }

  // Eventos socket
  socket.on('connect', ()=>{
    socket.emit('join', { question_id: currentQid, user_id: uid, username: uname });
  });

  socket.on('message:new', (m)=>{
    if (m.question_id !== currentQid) return;
    renderMessage(m);
    // Yo acabo de ver el nuevo mensaje -> actualizo mi lastSeen local y notifico seen
    myLastSeenId = Math.max(myLastSeenId, m.id);
    scrollBottom();
    markSeen();
  });

  socket.on('message:edited', ({id, text, edited_at})=>{
    const wrap = chatList.querySelector(`.msg[data-id="${id}"]`);
    if (!wrap) return;
    const txt = wrap.querySelector('.msg-text');
    if (txt) {
      txt.innerText = text;
      const edited = wrap.querySelector('.edited');
      if (edited) edited.textContent = '(editado)';
      else txt.insertAdjacentHTML('beforeend',' <span class="edited">(editado)</span>');
    }
  });

  socket.on('message:deleted', ({id})=>{
    const el = chatList.querySelector(`.msg[data-id="${id}"]`);
    if (el) {
      el.classList.add('deleted');
      el.querySelector('.msg-text').innerHTML = '<i>Mensaje eliminado</i>';
      const acts = el.querySelector('.msg-actions'); if (acts) acts.remove();
    }
  });

  socket.on('seen:update', ({user_id, last_message_id})=>{
    // Solo me interesa si QUIEN ha visto NO soy yo (o sea, lo ha visto la otra persona)
    if (user_id === uid) return;
    // Marca ✓✓ en MIS mensajes con id <= last_message_id
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
    // Si la reacción viene de mí => se pinta en el avatar de la otra persona
    // Si viene de la otra persona => se pinta en mi avatar
    if (user_id === uid) {
      if (badgeThem) { badgeThem.textContent = emoji; badgeThem.classList.add('show'); }
      const cur = document.querySelector('.dq-reaction-current .big-reaction'); if (cur) cur.textContent = emoji;
    } else {
      if (badgeMe) { badgeMe.textContent = emoji; badgeMe.classList.add('show'); }
      const part = document.querySelector('.dq-reaction-partner .big-reaction'); if (part) part.textContent = emoji;
    }
  });

  // UI handlers
  chatBtn?.addEventListener('click', ()=>{
    open(chatModal);
    // Rejoin por si el historial cambió currentQid
    socket.emit('join', { question_id: currentQid, user_id: uid, username: uname });
    fetchInitial();
    setTimeout(scrollBottom, 50);
  });
  closeChat?.addEventListener('click', ()=>close(chatModal));

  historyBtn?.addEventListener('click', ()=>{
    open(historyModal);
    const hx = document.getElementById('history-list');
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
          open(chatModal);
          socket.emit('join', { question_id: currentQid, user_id: uid, username: uname });
          fetch(`/api/chat/${currentQid}`).then(r=>r.json()).then(d=>{
            chatList.innerHTML = '';
            (d.messages||[]).forEach(m=>renderMessage(m));
            myLastSeenId = Math.max(0, ...((d.messages||[]).map(m=>m.id)));
            setTimeout(()=>{ scrollBottom(); markSeen(); }, 50);
          });
        });
      });
    });
  });
  closeHistory?.addEventListener('click', ()=>close(historyModal));

  // Enviar mensaje
  function sendNow(){
    const t = (chatText.value || '').trim();
    if (!t) return;
    socket.emit('send_message', {
      question_id: currentQid,
      user_id: uid,
      username: uname,
      text: t
    });
    chatText.value = '';
  }
  chatSend?.addEventListener('click', sendNow);
  chatText?.addEventListener('keydown', (e)=>{
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendNow(); }
  });

  // Reacción (expuesto globalmente)
  window.setQuestionReaction = function(emoji){
    fetch(`/api/reaction/${currentQid}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({emoji})
    });
  };
})();
