import uuid
import sqlite3
import shutil
import bcrypt
import os
import hashlib
import asyncio
import time
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Request, Form, Cookie, Body, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import RedirectResponse, FileResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from database.db_setup import init_db, DB_PATH
from pathlib import Path

# ==========================================
# CONFIGURAÇÃO E AMBIENTE
# ==========================================

app = FastAPI(title="SyncForge API", version="1.6.5")
templates = Jinja2Templates(directory="templates")

CHUNKS_TEMP_DIR = "chunks_temp"
os.makedirs(CHUNKS_TEMP_DIR, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "sync_storage")

if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR)

# ==========================================
# FUNÇÕES AUXILIARES
# ==========================================
# 👈 CORREÇÃO: Função que estava faltando definida aqui
def get_password_hash(password: str) -> str:
    """Gera o hash da senha usando bcrypt para salvar no SQLite."""
    password_bytes = password.encode('utf-8')
    hashed_bytes = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed_bytes.decode('utf-8')

def get_file_metadata(full_path, base_path):
    try:
        if not os.path.exists(full_path): return None
        hash_md5 = hashlib.md5()
        with open(full_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        stat = os.stat(full_path)
        updated_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
        rel_path = os.path.relpath(full_path, base_path).replace("\\", "/")
        return {
            "path": rel_path,
            "hash": hash_md5.hexdigest(),
            "updated_at": updated_at,
            "size": stat.st_size
        }
    except Exception as e:
        print(f"❌ Erro ao ler metadados: {e}")
        return None

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def check_session(syncforge_session: Optional[str]):
    """Validador central de acesso Web."""
    if not syncforge_session or syncforge_session != "admin_logado":
        return False
    return True

@app.on_event("startup")
def startup_event():
    init_db()
    conn = get_db_connection()
    try:
        conn.execute("ALTER TABLE folders ADD COLUMN server_path TEXT DEFAULT '/var/syncforge'")
        conn.commit()
    except: pass
    finally: conn.close()

# ==========================================
# MOTOR DE COMUNICAÇÃO (WEBSOCKET)
# ==========================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, folder_id: str):
        await ws.accept()
        if folder_id not in self.active_connections:
            self.active_connections[folder_id] = []
        self.active_connections[folder_id].append(ws)

    def disconnect(self, ws: WebSocket, folder_id: str):
        if folder_id in self.active_connections:
            self.active_connections[folder_id].remove(ws)

    async def broadcast(self, message: str, folder_id: str):
        if folder_id in self.active_connections:
            for connection in self.active_connections[folder_id]:
                try:
                    await connection.send_text(message)
                except: pass

manager = ConnectionManager()

class ServerFolderSyncHandler(FileSystemEventHandler):
    def __init__(self, folder_id, loop, server_path):
        self.folder_id = folder_id
        self.loop = loop
        self.server_path = os.path.abspath(server_path)
        self._pending_updates = {}

    def _get_rel_path(self, src_path):
        return os.path.relpath(src_path, self.server_path).replace("\\", "/")

    def on_deleted(self, event):
        if event.is_directory: return
        rel_path = self._get_rel_path(event.src_path)
        asyncio.run_coroutine_threadsafe(manager.broadcast(f"DELETE:{rel_path}", self.folder_id), self.loop)

    def on_created(self, event):
        if not event.is_directory:
            self.loop.create_task(self._debounce_broadcast(self._get_rel_path(event.src_path)))

    def on_modified(self, event):
        if not event.is_directory:
            self.loop.create_task(self._debounce_broadcast(self._get_rel_path(event.src_path)))

    async def _debounce_broadcast(self, rel_path):
        self._pending_updates[rel_path] = time.time()
        await asyncio.sleep(2)
        if time.time() - self._pending_updates.get(rel_path, 0) >= 2:
            await manager.broadcast(f"UPDATE:{rel_path}", self.folder_id)

active_observers = {}

# ==========================================
# ROTAS WEB (DASHBOARD)
# ==========================================

@app.get("/login")
def render_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login")
def process_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == "admin" and password == "sync2026":
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(key="syncforge_session", value="admin_logado", httponly=True)
        return resp
    return templates.TemplateResponse(request=request, name="login.html", context={"erro": "Credenciais inválidas!"})

@app.get("/")
def painel_web(request: Request, syncforge_session: str = Cookie(None)):
    if not check_session(syncforge_session): return RedirectResponse(url="/login", status_code=303)
    
    conn = get_db_connection()
    try:
        # 1. Busca lista de pastas
        pastas = [dict(row) for row in conn.execute("SELECT * FROM folders").fetchall()]
        
        # 2. Contador de Cofres Ativos
        qtd_pastas = len(pastas)
        
        # 3. Contador de Dispositivos Autorizados (Users com device_token)
        cursor = conn.execute("SELECT COUNT(*) as total FROM users WHERE device_token IS NOT NULL")
        qtd_dispositivos = cursor.fetchone()["total"]
        
        # 4. Contador de Usuários Totais
        cursor = conn.execute("SELECT COUNT(*) as total FROM users")
        qtd_usuarios = cursor.fetchone()["total"]
        
        disco = shutil.disk_usage("/")
        
        # 👈 INJEÇÃO DE DADOS: Agora passamos os contadores para o HTML
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "pastas": pastas,
            "qtd_pastas": qtd_pastas,
            "qtd_dispositivos": qtd_dispositivos,
            "qtd_usuarios": qtd_usuarios,
            "disco_percent": int((disco.used / disco.total) * 100)
        })
    finally: conn.close()

@app.post("/web/folders/create")
def web_create_folder(name: str = Form(...), server_path: str = Form(...), sync_type: str = Form(...), syncforge_session: str = Cookie(None)):
    if not check_session(syncforge_session): return RedirectResponse(url="/login", status_code=303)
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO folders (id, user_id, name, sync_type, server_path) VALUES (?, ?, ?, ?, ?)",
                   (str(uuid.uuid4()), "admin", name, sync_type, server_path))
    conn.commit(); conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/web/folders/delete")
def web_delete_folder(folder_id: str = Form(...), syncforge_session: str = Cookie(None)):
    if not check_session(syncforge_session): return RedirectResponse(url="/login", status_code=303)
    
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    conn.commit(); conn.close()
    return RedirectResponse(url="/", status_code=303)

# ==========================================
# API DE SINCRONIZAÇÃO (MOBILE)
# ==========================================

@app.post("/api/login")
async def api_login(data: dict = Body(...)):
    email = data.get("email")
    password = data.get("password")
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 👈 Mudança: Agora selecionamos explicitamente o device_token
    cursor.execute("SELECT id, email, password_hash, role, device_token FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    conn.close()
    
    if user and bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
        # 👈 Mudança: Retornamos o dicionário com a chave que o seu UserModel.dart espera
        return {
            "status": "success", 
            "user": {
                "id": user["id"], 
                "email": user["email"], 
                "role": user["role"],
                "device_token": user["device_token"] # Se for None, o FastAPI envia como null
            }
        }
    raise HTTPException(status_code=401, detail="E-mail ou senha incorretos")

# 👈 NOVA ROTA: Listagem de Usuários Web
@app.get("/web/users")
def painel_usuarios(request: Request, syncforge_session: str = Cookie(None)):
    if not check_session(syncforge_session): return RedirectResponse(url="/login", status_code=303)
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT id, email, role, device_token FROM users")
    usuarios = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="users.html", context={"usuarios": usuarios})

@app.get("/logout")
def logout():
    """Rota para encerrar a sessão web (agora corrigida)."""
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("syncforge_session")
    return resp

# 👈 NOVA ROTA: Criação de Usuário via Web
@app.post("/web/users/create")
def web_create_user(email: str = Form(...), password: str = Form(...), role: str = Form("user"), syncforge_session: str = Cookie(None)):
    if not check_session(syncforge_session): return RedirectResponse(url="/login", status_code=303)
    
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO users (id, email, password_hash, role) VALUES (?, ?, ?, ?)",
                   (str(uuid.uuid4()), email, get_password_hash(password), role))
    conn.commit(); conn.close()
    return RedirectResponse(url="/web/users", status_code=303)

# 👈 NOVA ROTA: Deleção de Usuário via Web
@app.post("/web/users/delete")
def web_delete_user(user_id: str = Form(...), syncforge_session: str = Cookie(None)):
    if not check_session(syncforge_session): return RedirectResponse(url="/login", status_code=303)
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit(); conn.close()
    return RedirectResponse(url="/web/users", status_code=303)

@app.get("/api/folders")
async def api_list_folders():
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT id, name, sync_type, server_path FROM folders")
    rows = cursor.fetchall(); conn.close()
    return [dict(row) for row in rows]

@app.get("/api/folders/{identifier}")
async def get_folders_api(identifier: str):
    conn = get_db_connection()
    
    # 1. Tenta buscar a pasta específica
    folder = conn.execute("SELECT id, name, sync_type, server_path, user_id FROM folders WHERE id = ?", (identifier,)).fetchone()
    
    if folder:
        conn.close()
        return dict(folder)
    
    # 2. Se não achou (ou se o ID for de usuário), retorna TODAS as pastas
    # Isso evita o 404 no MI 8 Lite e já resolve a listagem global
    all_folders = conn.execute("SELECT id, name, sync_type, server_path, user_id FROM folders").fetchall()
    conn.close()
    
    return [dict(f) for f in all_folders]

@app.get("/api/list-files/{folder_id}")
async def list_server_files(folder_id: str):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,))
    folder = cursor.fetchone(); conn.close()
    if not folder: raise HTTPException(status_code=404)
    base = folder['server_path']
    files = []
    if os.path.exists(base):
        for root, _, fs in os.walk(base):
            for f in fs:
                meta = get_file_metadata(os.path.join(root, f), base)
                if meta: files.append(meta)
    return files

@app.get("/api/download/{folder_id}")
async def download_file(folder_id: str, file_path: str, range_header: Optional[str] = Header(None, alias="Range")):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,))
    folder = cursor.fetchone(); conn.close()
    full_path = os.path.join(folder['server_path'], file_path)
    if range_header:
        start = int(range_header.replace("bytes=", "").split("-")[0])
        file_size = os.path.getsize(full_path)
        async def iter_f():
            with open(full_path, "rb") as f:
                f.seek(start)
                while c := f.read(1024*1024): yield c
        return StreamingResponse(iter_f(), status_code=206, headers={
            "Content-Range": f"bytes {start}-{file_size-1}/{file_size}",
            "Content-Length": str(file_size - start)
        })
    return FileResponse(full_path)

@app.post("/api/upload-chunk/{folder_id}")
async def upload_chunk(folder_id: str, file_id: str = Form(...), chunk_index: int = Form(...), 
                       total_chunks: int = Form(...), relative_path: str = Form(...), chunk: UploadFile = File(...)):
    temp = os.path.join(CHUNKS_TEMP_DIR, file_id)
    os.makedirs(temp, exist_ok=True)
    with open(os.path.join(temp, f"part_{chunk_index}"), "wb") as f: f.write(await chunk.read())
    if len(os.listdir(temp)) == total_chunks:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,))
        folder = cursor.fetchone(); conn.close()
        final = os.path.join(folder['server_path'], relative_path)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        with open(final, "wb") as f:
            for i in range(total_chunks):
                with open(os.path.join(temp, f"part_{i}"), "rb") as p: f.write(p.read())
        shutil.rmtree(temp)
        await manager.broadcast(f"UPDATE:{relative_path}", folder_id)
        return {"status": "completed"}
    return {"status": "received"}

# ==========================================
# 🗑️ ROTA DE DELETE REFORÇADA (CORRIGIDA)
# ==========================================

@app.delete("/api/file/{folder_id}")
async def delete_server_file(folder_id: str, file_path: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,))
    folder = cursor.fetchone()
    conn.close()
    
    if folder:
        # Normaliza o caminho para o SO (importante no Windows)
        full_path = os.path.normpath(os.path.join(folder['server_path'], file_path))
        print(f"🗑️ Tentando deletar: {full_path}")
        
        try:
            if os.path.exists(full_path):
                os.remove(full_path)
                print(f"✅ Arquivo deletado com sucesso: {file_path}")
                await manager.broadcast(f"DELETE:{file_path}", folder_id)
                return {"status": "success"}
            else:
                print(f"⚠️ Arquivo não encontrado para deletar: {full_path}")
                return {"status": "file_not_found"}
        except PermissionError:
            print(f"❌ Erro de Permissão: O arquivo está aberto ou sendo usado: {full_path}")
            return {"status": "permission_denied"}
        except Exception as e:
            print(f"❌ Erro inesperado ao deletar: {e}")
            return {"status": "error", "message": str(e)}
            
    return {"status": "folder_not_found"}

# ==========================================
# 📡 WEBSOCKET
# ==========================================

@app.websocket("/api/ws/sync/{folder_id}")
async def websocket_endpoint(websocket: WebSocket, folder_id: str):
    await manager.connect(websocket, folder_id)
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,))
    folder = cursor.fetchone(); conn.close()
    if folder:
        path = os.path.abspath(folder['server_path'])
        if os.path.exists(path) and folder_id not in active_observers:
            handler = ServerFolderSyncHandler(folder_id, asyncio.get_running_loop(), path)
            obs = Observer(); obs.schedule(handler, path, recursive=True); obs.start()
            active_observers[folder_id] = obs
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, folder_id)