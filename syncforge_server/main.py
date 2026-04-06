import uuid
import sqlite3
import shutil
import bcrypt
import os
import hashlib
import asyncio
import time
import math
import platform
import json
import csv
import io
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

active_observers = {}

if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR)

# ==========================================
# FUNÇÕES AUXILIARES
# ==========================================
def register_sync_log(user_id, username, folder_name, file_name, action):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO sync_logs (user_id, username, folder_name, file_name, action) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, folder_name, file_name, action)
    )
    conn.commit()
    conn.close()
# Função auxiliar para deixar o tamanho do arquivo legível
def format_size(size_bytes):
    if size_bytes == 0: return "0B"
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {['B', 'KB', 'MB', 'GB'][i]}"

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

# No topo do main.py
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}
        self.online_users = set() # 👈 Novo: Rastreia IDs de usuários online

    async def connect(self, websocket: WebSocket, folder_id: str, user_id: str):
        await websocket.accept()
        if folder_id not in self.active_connections:
            self.active_connections[folder_id] = []
        self.active_connections[folder_id].append(websocket)
        self.online_users.add(user_id) # 👈 Marca o usuário como online

    def disconnect(self, websocket: WebSocket, folder_id: str, user_id: str):
        if folder_id in self.active_connections:
            self.active_connections[folder_id].remove(websocket)
        # Se o usuário não tiver mais nenhuma pasta conectada, fica offline
        self.online_users.discard(user_id)

manager = ConnectionManager()

class ServerFolderSyncHandler(FileSystemEventHandler):
    def __init__(self, folder_id, user_id, username, folder_name, loop, server_path):
        self.folder_id = folder_id
        self.user_id = user_id
        self.username = username
        self.folder_name = folder_name
        self.loop = loop
        self.server_path = os.path.abspath(server_path)
        self._pending_updates = {} 

    def _get_rel_path(self, src_path):
        return os.path.relpath(src_path, self.server_path).replace("\\", "/")

    def on_moved(self, event):
        """Detecta renomeação e registra no log"""
        if event.is_directory: return
        old_rel = self._get_rel_path(event.src_path)
        new_rel = self._get_rel_path(event.dest_path)
        
        # 👈 REGISTRO DE LOG: RENAME
        register_sync_log(self.user_id, self.username, self.folder_name, f"{old_rel} -> {new_rel}", "RENAME")
        
        print(f"🔄 Watchdog: Arquivo renomeado no PC: {old_rel} -> {new_rel}")
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(f"RENAME:{old_rel}|{new_rel}", self.folder_id), 
            self.loop
        )

    def on_deleted(self, event):
        """Detecta exclusão e registra no log"""
        if event.is_directory: return
        rel_path = self._get_rel_path(event.src_path)
        
        # 👈 REGISTRO DE LOG: DELETE
        register_sync_log(self.user_id, self.username, self.folder_name, rel_path, "DELETE")
        
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(f"DELETE:{rel_path}", self.folder_id), 
            self.loop
        )

    def on_created(self, event):
        """Detecta novo arquivo e inicia debounce para log e broadcast"""
        if not event.is_directory:
            rel_path = self._get_rel_path(event.src_path)
            self.loop.create_task(self._debounce_broadcast(rel_path, "CREATE"))

    def on_modified(self, event):
        """Detecta alteração e inicia debounce para log e broadcast"""
        if not event.is_directory:
            # Ignora o arquivo de controle interno
            if ".syncforge_ledger" in event.src_path: return
            
            rel_path = self._get_rel_path(event.src_path)
            self.loop.create_task(self._debounce_broadcast(rel_path, "UPDATE"))

    async def _debounce_broadcast(self, rel_path, action_type):
        """Aguardar o arquivo estabilizar antes de logar e enviar ao celular"""
        self._pending_updates[rel_path] = time.time()
        await asyncio.sleep(2) 
        
        # Só executa se não houveram novas mudanças nos últimos 2 segundos
        if time.time() - self._pending_updates.get(rel_path, 0) >= 2:
            # 👈 REGISTRO DE LOG: CREATE ou UPDATE
            register_sync_log(self.user_id, self.username, self.folder_name, rel_path, action_type)
            
            await manager.broadcast(f"UPDATE:{rel_path}", self.folder_id)


# ==========================================
# ROTAS WEB (DASHBOARD)
# ==========================================
@app.get("/api/web/browse")
async def browse_server_folders(path: str = None):
    # Se não enviar nada, começa na raiz do usuário ou C:
    if not path or path == "null":
        path = os.path.expanduser("~") if platform.system() != "Windows" else "C:\\"
    
    try:
        # Lista apenas diretórios, ignora arquivos e pastas ocultas
        items = []
        for name in os.listdir(path):
            full_path = os.path.join(path, name)
            if os.path.isdir(full_path) and not name.startswith('.'):
                items.append(name)
        
        return {
            "current_path": path.replace("\\", "/"),
            "folders": sorted(items),
            "parent": os.path.dirname(path).replace("\\", "/")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    
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
async def web_create_folder(
    name: str = Form(...), 
    server_path: str = Form(...), 
    user_id: str = Form(...),
    sync_type: str = Form(...) # 👈 1. Captura o valor do Dropdown do HTML
):
    try:
        conn = get_db_connection()
        new_folder_id = str(uuid.uuid4())
        
        # 👈 2. Adiciona a coluna 'sync_type' e o ponto de interrogação '?'
        # 👈 3. Passa a variável 'sync_type' na tupla de valores
        conn.execute(
            "INSERT INTO folders (id, name, server_path, user_id, sync_type) VALUES (?, ?, ?, ?, ?)",
            (new_folder_id, name, server_path, user_id, sync_type) 
        )
        conn.commit()
        conn.close()
        
        if not os.path.exists(server_path):
            os.makedirs(server_path, exist_ok=True)
            
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        print(f"❌ Erro ao criar cofre: {e}")
        return RedirectResponse(url="/?error=true", status_code=303)

@app.get("/web/folders/{folder_id}/explorer")
async def folder_explorer(request: Request, folder_id: str):
    conn = get_db_connection()
    folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    conn.close()

    if not folder:
        return RedirectResponse(url="/?error=folder_not_found")

    base_path = os.path.abspath(folder['server_path'])
    files_list = []

    print(f"\n--- 📂 LISTANDO CONTEÚDO DE: {base_path} ---")

    if os.path.exists(base_path):
        # Usamos listdir primeiro para um check rápido do que tem na "cara" da pasta
        conteudo_bruto = os.listdir(base_path)
        print(f"Conteúdo bruto na raiz: {conteudo_bruto}")

        for root, dirs, files in os.walk(base_path):
            for name in files:
                if name == ".syncforge_ledger" or name.startswith("~$"):
                    continue
                
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, base_path).replace("\\", "/")
                
                print(f"📄 Arquivo encontrado: {rel_path}") # Isso vai sair no seu terminal

                try:
                    stats = os.stat(full_path)
                    files_list.append({
                        "name": name,
                        "rel_path": rel_path,
                        "is_dir": False,
                        "size": format_size(stats.st_size),
                        "modified": datetime.fromtimestamp(stats.st_mtime).strftime('%d/%m/%Y %H:%M')
                    })
                except Exception as e:
                    print(f"❌ Erro ao ler stats de {name}: {e}")
    else:
        print(f"❌ Erro crítico: O caminho {base_path} desapareceu?")

    print(f"--- ✅ TOTAL DE ARQUIVOS ENCONTRADOS: {len(files_list)} ---\n")

    return templates.TemplateResponse(
        request=request,
        name="explorer.html",
        context={
            "folder": folder,
            "files": files_list
        }
    )

@app.get("/web/files/download")
async def download_file(folder_id: str, path: str):
    conn = get_db_connection()
    folder = conn.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,)).fetchone()
    conn.close()
    
    file_path = os.path.join(folder['server_path'], path)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=os.path.basename(file_path))
    raise HTTPException(status_code=404)


@app.post("/web/files/delete")
async def delete_file(
    folder_id: str = Form(...), 
    path: str = Form(...) # Mantemos 'path' para bater com seu HTML
):
    conn = get_db_connection()
    # 1. Buscamos os dados completos para o Log e para o Caminho
    query = """
        SELECT f.server_path, f.name as folder_name, u.id as user_id, u.username 
        FROM folders f 
        JOIN users u ON f.user_id = u.id 
        WHERE f.id = ?
    """
    data = conn.execute(query, (folder_id,)).fetchone()

    if not data:
        conn.close()
        raise HTTPException(status_code=404, detail="Cofre não encontrado")

    # 2. Montamos o caminho físico (path aqui é o nome do arquivo)
    full_path = os.path.join(data['server_path'], path)
    
    if os.path.exists(full_path):
        os.remove(full_path)
        
        # 3. REGISTRAMOS O LOG (Agora com os dados reais)
        register_sync_log(
            user_id=data['user_id'],
            username=data['username'],
            folder_name=data['folder_name'],
            file_name=path, # O 'path' aqui é o nome do arquivo que foi deletado
            action="DELETE (WEB)"
        )
        
    conn.commit()
    conn.close()
    
    return RedirectResponse(url=f"/web/folders/{folder_id}/explorer", status_code=303)


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
async def api_login(request: Request, data: dict = Body(...)): # 👈 Adicionamos 'request' para pegar o IP
    email = data.get("email")
    password = data.get("password")
    device_model = data.get("device_model", "Aparelho Desconhecido") # 👈 Captura o modelo enviado pelo Flutter
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Busca o usuário
    cursor.execute("SELECT id, username, email, password_hash, role, device_token FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    
    if user and bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
        user_id = user["id"]
        client_ip = request.client.host # 👈 Pega o IP real de conexão (Arcoverde ou externa)

        # 2. ⚡ O PULO DO GATO: Atualiza os dados de monitoramento automaticamente
        cursor.execute("""
            UPDATE users 
            SET device_model = ?, last_ip = ?, last_seen = CURRENT_TIMESTAMP 
            WHERE id = ?
        """, (device_model, client_ip, user_id))
        
        conn.commit()
        conn.close()
        
        # 3. Retorna exatamente o que seu UserModel.dart espera (sem quebrar o App)
        return {
            "status": "success", 
            "user": {
                "id": user_id, 
                "username": user["username"], # Adicionado para garantir
                "email": user["email"], 
                "role": user["role"],
                "device_token": user["device_token"],
                "device_model": device_model # O App também recebe de volta o que enviou
            }
        }
    
    conn.close()
    raise HTTPException(status_code=401, detail="E-mail ou senha incorretos")

# 👈 NOVA ROTA: Listagem de Usuários Web
@app.get("/web/users")
async def list_users_page(request: Request):
    conn = get_db_connection()
    usuarios = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "usuarios": usuarios,
            "online_users": manager.online_users # 👈 Passamos o set de usuários ativos
        }
    )

@app.get("/logout")
def logout():
    """Rota para encerrar a sessão web (agora corrigida)."""
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("syncforge_session")
    return resp

# 👈 NOVA ROTA: Criação de Usuário via Web
@app.post("/web/users/create")
def web_create_user(
    username: str = Form(...), # 👈 Captura o novo campo do formulário
    email: str = Form(...), 
    password: str = Form(...), 
    role: str = Form("user")
):
    conn = get_db_connection()
    try:
        # 👈 Adicionamos 'username' tanto na lista de colunas quanto nos valores
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), username, email, get_password_hash(password), role)
        )
        conn.commit()
    except Exception as e:
        print(f"❌ Erro ao criar usuário: {e}")
    finally:
        conn.close() # 👈 VITAL: Isso destrava o banco mesmo se der erro!
        
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
async def list_all_folders(): # 👈 O app precisa enviar isso
    conn = get_db_connection()
    # Filtra as pastas que pertencem EXATAMENTE ao ID enviado pelo celular
    folders = conn.execute(
        "SELECT id, name, server_path, sync_type FROM folders", 
        
    ).fetchall()
    conn.close()
    
    # Se folders vier vazio, o retorno será [] com status 200
    return [dict(f) for f in folders]

@app.get("/api/folders/{identifier}")
async def get_folders_api(identifier: str):
    conn = get_db_connection()
    
    # 1. Tenta buscar UMA pasta específica pelo ID dela
    folder = conn.execute(
        "SELECT id, name, sync_type, server_path, user_id FROM folders WHERE id = ?", 
        (identifier,)
    ).fetchone()
    
    if folder:
        conn.close()
        return dict(folder) # Retorna um objeto único: { id: ... }
    
    # 2. Se não achou a pasta, vamos assumir que o 'identifier' é um USER_ID
    # E vamos retornar apenas as pastas que pertencem a ESSE USUÁRIO
    user_folders = conn.execute(
        "SELECT id, name, sync_type, server_path, user_id FROM folders WHERE user_id = ?", 
        (identifier,)
    ).fetchall()
    conn.close()
    
    # Retorna a lista de pastas do usuário (pode vir vazia [] se o ID não existir)
    return [dict(f) for f in user_folders]


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

@app.post("/api/rename/{folder_id}")
async def rename_file(folder_id: str, old_path: str = Form(...), new_path: str = Form(...)):
    conn = get_db_connection()
    folder = conn.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,)).fetchone()
    conn.close()
    
    if folder:
        old_full = os.path.normpath(os.path.join(folder['server_path'], old_path))
        new_full = os.path.normpath(os.path.join(folder['server_path'], new_path))
        
        if os.path.exists(old_full):
            os.makedirs(os.path.dirname(new_full), exist_ok=True)
            
            # 🛑 TRAVA: Renomeia no disco
            os.rename(old_full, new_full) 
            
            # O aviso de BROADCAST deve ser enviado, mas o celular que enviou 
            # o POST já sabe da mudança. 
            await manager.broadcast(f"RENAME:{old_path}|{new_path}", folder_id)
            return {"status": "success"}
            
    return {"status": "error"}

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

@app.get("/api/web/users")
async def get_users_list():
    conn = get_db_connection()
    # Buscamos apenas ID e Username para o seletor
    users = conn.execute("SELECT id, username FROM users").fetchall()
    conn.close()
    return [{"id": u["id"], "username": u["username"]} for u in users]

# 👈 ADICIONE ESTA ROTA (Certifique-se de que está acima de /api/folders/{identifier})
@app.get("/api/folders/{folder_id}/content")
async def get_file_text_content(folder_id: str, file_path: str):
    """Retorna o conteúdo de texto para o preview de conflitos."""
    conn = get_db_connection()
    folder = conn.execute("SELECT server_path FROM folders WHERE id = ?", (folder_id,)).fetchone()
    conn.close()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Cofre não encontrado")
    
    # Caminho absoluto no Windows/Debian
    full_path = os.path.normpath(os.path.join(folder['server_path'], file_path))
    
    if not os.path.exists(full_path):
        print(f"❌ Arquivo não encontrado no disco: {full_path}")
        raise HTTPException(status_code=404, detail="Arquivo físico não encontrado")

    try:
        # Lemos como texto UTF-8 para o preview do Flutter
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
            return content
    except Exception as e:
        print(f"❌ Erro ao ler texto: {e}")
        raise HTTPException(status_code=400, detail="O arquivo não é um texto válido")
    
@app.get("/web/logs")
async def view_logs(
    request: Request, 
    user_id: str = None, 
    start_date: str = None, 
    end_date: str = None
):
    conn = get_db_connection()
    query = "SELECT * FROM sync_logs WHERE 1=1"
    params = []

    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    if start_date:
        query += " AND timestamp >= ?"
        params.append(f"{start_date} 00:00:00")
    if end_date:
        query += " AND timestamp <= ?"
        params.append(f"{end_date} 23:59:59")

    query += " ORDER BY timestamp DESC LIMIT 500"
    logs = conn.execute(query, params).fetchall()
    usuarios = conn.execute("SELECT id, username FROM users").fetchall()
    conn.close()

    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={"logs": logs, "usuarios": usuarios, "filters": {"user_id": user_id, "start": start_date, "end": end_date}}
    )

@app.get("/web/logs/export/{format}")
async def export_logs(format: str):
    conn = get_db_connection()
    logs = conn.execute("SELECT * FROM sync_logs ORDER BY timestamp DESC").fetchall()
    conn.close()

    if format == "json":
        data = [dict(log) for log in logs]
        return StreamingResponse(
            io.StringIO(json.dumps(data, indent=4)),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=syncforge_logs.json"}
        )
    
    # Exportação CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "User ID", "User", "Vault", "File", "Action", "Timestamp"])
    for log in logs:
        writer.writerow([log['id'], log['user_id'], log['username'], log['folder_name'], log['file_name'], log['action'], log['timestamp']])
    
    output.seek(0)
    return StreamingResponse(
        io.StringIO(output.read()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=syncforge_logs.csv"}
    )

@app.post("/web/logs/clear")
async def clear_logs():
    conn = get_db_connection()
    conn.execute("DELETE FROM sync_logs")
    conn.commit()
    conn.close()
    return RedirectResponse(url="/web/logs", status_code=303)

# ==========================================
# 📡 WEBSOCKET
# ==========================================

@app.websocket("/api/ws/sync/{folder_id}")
async def websocket_endpoint(websocket: WebSocket, folder_id: str):
    # 1. Busca os dados necessários para o Log e o Monitor
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Fazemos um JOIN para pegar o nome do usuário e da pasta de uma vez só
    query = """
        SELECT f.server_path, f.name as folder_name, u.id as user_id, u.username 
        FROM folders f 
        JOIN users u ON f.user_id = u.id 
        WHERE f.id = ?
    """
    data = cursor.execute(query, (folder_id,)).fetchone()
    
    if not data:
        conn.close()
        await websocket.close(code=1008) # Pasta não encontrada
        return

    user_id = data['user_id']
    username = data['username']
    folder_name = data['folder_name']
    server_path = os.path.abspath(data['server_path'])
    client_ip = websocket.client.host

    # 2. Atualiza o Live Status (IP e Visto por último)
    cursor.execute(
        "UPDATE users SET last_ip = ?, last_seen = CURRENT_TIMESTAMP WHERE id = ?",
        (client_ip, user_id)
    )
    conn.commit()
    conn.close()

    # 3. Registra a conexão no Manager (Marca como ONLINE)
    await manager.connect(websocket, folder_id, user_id)

    # 4. Inicia o Watchdog com os novos parâmetros para Log
    if os.path.exists(server_path) and folder_id not in active_observers:
        # Criamos o Handler passando tudo o que ele precisa para registrar os Logs
        handler = ServerFolderSyncHandler(
            folder_id=folder_id,
            user_id=user_id,
            username=username,
            folder_name=folder_name,
            loop=asyncio.get_running_loop(),
            server_path=server_path
        )
        
        obs = Observer()
        obs.schedule(handler, server_path, recursive=True)
        obs.start()
        active_observers[folder_id] = obs
        print(f"👀 Monitorando: {folder_name} para {username}")

    try:
        # Mantém a conexão aberta aguardando mensagens (ou apenas o keep-alive)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        # Remove do Manager e marca como OFFLINE
        manager.disconnect(websocket, folder_id, user_id)
        print(f"🔌 Desconectado: {username} ({folder_name})")
