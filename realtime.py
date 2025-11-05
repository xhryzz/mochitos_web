# realtime.py
from flask_socketio import SocketIO, join_room, emit
from datetime import datetime, timezone
import psycopg2.extras

socketio = None
pool = None

def _to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def init_realtime(app, db_pool):
    """
    Inicializa Socket.IO y registra los eventos del chat.
    Requisitos de BD (compatibles):
      - chat_messages(id, question_id, sender_id NULLABLE, sender_username TEXT NOT NULL, text, created_at, edited_at, is_deleted)
      - chat_reads(message_id, user_id NULLABLE, username TEXT NOT NULL, seen_at, PK(message_id, username))
    """
    global socketio, pool
    pool = db_pool  # SimpleConnectionPool
    # Si en Render usas worker-class eventlet, esto sigue funcionando.
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

    # --- JOIN A LA SALA DE LA PREGUNTA ---
    @socketio.on("join", namespace="/chat")
    def on_join(data):
        # data: { question_id, user_id, username }
        qid = _to_int(data.get("question_id"))
        uid = _to_int(data.get("user_id"))
        uname = (data.get("username") or (f"user_{uid}" if uid else "user")).strip()

        if not qid:
            return

        room = f"q:{qid}"
        join_room(room)
        # Notificamos presencia (opcional)
        emit("presence", {"user_id": uid, "username": uname, "status": "joined"},
             to=room, include_self=False)

    # --- ENVIAR MENSAJE ---
    @socketio.on("send_message", namespace="/chat")
    def on_send(data):
        # data: { question_id, user_id, username, text }
        qid = _to_int(data.get("question_id"))
        uid = _to_int(data.get("user_id"))
        uname = (data.get("username") or (f"user_{uid}" if uid else "user")).strip()
        text = (data.get("text") or "").strip()

        if not qid or not text or not uname:
            return

        with pool.getconn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        INSERT INTO chat_messages (question_id, sender_id, sender_username, text)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id, created_at
                    """, (qid, uid if uid else None, uname, text))
                    row = cur.fetchone()
                    conn.commit()
                    payload = {
                        "id": row["id"],
                        "question_id": qid,
                        "sender_id": uid,
                        "sender_username": uname,
                        "text": text,
                        "created_at": row["created_at"].isoformat(),
                        "edited_at": None,
                        "is_deleted": False,
                    }
            finally:
                pool.putconn(conn)

        emit("message:new", payload, to=f"q:{qid}")

    # --- EDITAR MENSAJE ---
    @socketio.on("edit_message", namespace="/chat")
    def on_edit(data):
        # data: { message_id, user_id, username, text }
        mid = _to_int(data.get("message_id"))
        uid = _to_int(data.get("user_id"))
        uname = (data.get("username") or (f"user_{uid}" if uid else "user")).strip()
        text = (data.get("text") or "").strip()
        if not mid or not text:
            return

        qid = None
        edited_at_iso = None

        with pool.getconn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Comprobamos autoría por sender_id o por sender_username
                    cur.execute("""
                        SELECT question_id, sender_id, sender_username
                          FROM chat_messages
                         WHERE id=%s
                    """, (mid,))
                    m = cur.fetchone()
                    if not m:
                        return
                    is_author = False
                    if m["sender_id"] is not None and uid and m["sender_id"] == uid:
                        is_author = True
                    if m["sender_username"] and m["sender_username"] == uname:
                        is_author = True
                    if not is_author:
                        return

                    cur.execute("""
                        UPDATE chat_messages
                           SET text=%s, edited_at=NOW()
                         WHERE id=%s
                     RETURNING question_id, edited_at
                    """, (text, mid))
                    row = cur.fetchone()
                    conn.commit()
                    qid = row["question_id"]
                    edited_at_iso = row["edited_at"].isoformat()
            finally:
                pool.putconn(conn)

        emit("message:edited", {"id": mid, "text": text, "edited_at": edited_at_iso},
             to=f"q:{qid}")

    # --- BORRAR MENSAJE (soft-delete) ---
    @socketio.on("delete_message", namespace="/chat")
    def on_delete(data):
        # data: { message_id, user_id, username }
        mid = _to_int(data.get("message_id"))
        uid = _to_int(data.get("user_id"))
        uname = (data.get("username") or (f"user_{uid}" if uid else "user")).strip()
        if not mid:
            return

        qid = None

        with pool.getconn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT question_id, sender_id, sender_username
                          FROM chat_messages
                         WHERE id=%s
                    """, (mid,))
                    m = cur.fetchone()
                    if not m:
                        return

                    is_author = False
                    if m["sender_id"] is not None and uid and m["sender_id"] == uid:
                        is_author = True
                    if m["sender_username"] and m["sender_username"] == uname:
                        is_author = True
                    if not is_author:
                        return

                    cur.execute("""
                        UPDATE chat_messages
                           SET is_deleted=TRUE
                         WHERE id=%s
                     RETURNING question_id
                    """, (mid,))
                    row = cur.fetchone()
                    conn.commit()
                    qid = row["question_id"]
            finally:
                pool.putconn(conn)

        emit("message:deleted", {"id": mid}, to=f"q:{qid}")

    # --- MARCAR VISTOS ---
    @socketio.on("mark_seen", namespace="/chat")
    def on_seen(data):
        # data: { question_id, user_id, username, last_message_id }
        qid = _to_int(data.get("question_id"))
        uid = _to_int(data.get("user_id"))
        uname = (data.get("username") or (f"user_{uid}" if uid else "user")).strip()
        last_mid = _to_int(data.get("last_message_id"))
        if not qid or not last_mid or not uname:
            return

        with pool.getconn() as conn:
            try:
                with conn.cursor() as cur:
                    # Guardamos visto por username (PK (message_id, username))
                    cur.execute("""
                        INSERT INTO chat_reads (message_id, user_id, username)
                        SELECT id, %s, %s
                          FROM chat_messages
                         WHERE question_id=%s AND id<=%s
                        ON CONFLICT (message_id, username) DO NOTHING
                    """, (uid if uid else None, uname, qid, last_mid))
                    conn.commit()
            finally:
                pool.putconn(conn)

        emit("seen:update", {"user_id": uid, "last_message_id": last_mid}, to=f"q:{qid}")

    return socketio
