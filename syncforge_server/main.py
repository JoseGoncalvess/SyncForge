import uuid
import sqlite3
import shutil
import bcrypt
from fastapi import FastAPI, Request, Form, Cookie
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from database.db_setup import init_db, DB_PATH

app = FastAPI(title="SyncForge API", version="1.1.0")
templates = Jinja2Templates(directory="templates")

def get_password_hash(password: str) -> str:
    password_bytes = password.encode('utf-8')
    hashed_bytes = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed_bytes.decode('utf-8')

def auto_migrate_db():
    """Adiciona novas colunas ao banco sem precisar deletar o arquivo .db"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        # Tenta adicionar a coluna do caminho do servidor na tabela de pastas
        cursor.execute("ALTER TABLE folders ADD COLUMN server_path TEXT DEFAULT '/var/syncforge'")
        conn.commit()
        print("Migração: Coluna 'server_path' adicionada com sucesso.")
    except sqlite3.OperationalError:
        # Se a coluna já existir, ele ignora e segue a vida
        pass
    finally:
        conn.close()

@app.on_event("startup")
def startup_event():
    init_db()
    auto_migrate_db() # Roda a migração de segurança

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# 1. SISTEMA DE LOGIN
# ==========================================
@app.get("/login")
def render_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})

@app.post("/login")
def process_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == "admin" and password == "sync2026":
        resposta = RedirectResponse(url="/", status_code=303)
        resposta.set_cookie(key="syncforge_session", value="admin_logado", httponly=True)
        return resposta
    else:
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "erro": "Credenciais inválidas!"})

@app.get("/logout")
def logout():
    resposta = RedirectResponse(url="/login", status_code=303)
    resposta.delete_cookie("syncforge_session")
    return resposta

# ==========================================
# 2. DASHBOARD WEB (COFRES)
# ==========================================
@app.get("/")
def painel_web(request: Request, syncforge_session: str = Cookie(None)):
    if not syncforge_session:
        return RedirectResponse(url="/login", status_code=303)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        disco = shutil.disk_usage("/") 
        disco_percent = int((disco.used / disco.total) * 100)
        disco_livre_gb = round(disco.free / (1024**3), 1)

        # Atualizado para buscar o server_path também
        cursor.execute("SELECT id, name, sync_type, server_path FROM folders")
        pastas = [dict(row) for row in cursor.fetchall()]
        
        cursor.execute("SELECT COUNT(*) as total FROM users")
        total_usuarios = cursor.fetchone()["total"]
        
        cursor.execute("""
            SELECT f.name as folder_name, m.relative_path, m.is_deleted 
            FROM files_metadata m
            JOIN folders f ON m.folder_id = f.id
            ORDER BY m.rowid DESC LIMIT 10
        """)
        arquivos = [dict(row) for row in cursor.fetchall()]
        
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html", 
            context={
                "request": request,
                "status_servidor": "Online",
                "qtd_usuarios": total_usuarios,
                "qtd_pastas": len(pastas),
                "pastas": pastas,
                "arquivos": arquivos,
                "disco_percent": disco_percent,
                "disco_livre_gb": disco_livre_gb
            }
        )
    finally:
        conn.close()

@app.post("/web/folders/create")
def web_create_folder(
    request: Request, 
    syncforge_session: str = Cookie(None), 
    name: str = Form(...), 
    server_path: str = Form(...), # <-- NOVO CAMPO RECEBIDO AQUI
    sync_type: str = Form(...)
):
    if not syncforge_session:
        return RedirectResponse(url="/login", status_code=303)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    folder_id = str(uuid.uuid4())
    
    cursor.execute("SELECT id FROM users LIMIT 1")
    user = cursor.fetchone()
    user_id = user["id"] if user else "admin_padrao"
    
    try:
        # Atualizado para salvar o server_path no banco
        cursor.execute(
            "INSERT INTO folders (id, user_id, name, sync_type, server_path) VALUES (?, ?, ?, ?, ?)",
            (folder_id, user_id, name, sync_type, server_path)
        )
        conn.commit()
    finally:
        conn.close()
        
    return RedirectResponse(url="/", status_code=303)

@app.post("/web/folders/delete")
def web_delete_folder(request: Request, syncforge_session: str = Cookie(None), folder_id: str = Form(...)):
    if not syncforge_session:
        return RedirectResponse(url="/login", status_code=303)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM files_metadata WHERE folder_id = ?", (folder_id,))
        cursor.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()
    finally:
        conn.close()
        
    return RedirectResponse(url="/", status_code=303)

# ==========================================
# 3. GESTÃO DE USUÁRIOS
# ==========================================
@app.get("/web/users")
def painel_usuarios(request: Request, syncforge_session: str = Cookie(None)):
    if not syncforge_session:
        return RedirectResponse(url="/login", status_code=303)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, email, role FROM users")
        usuarios = [dict(row) for row in cursor.fetchall()]
        return templates.TemplateResponse(request=request, name="users.html", context={"request": request, "usuarios": usuarios})
    finally:
        conn.close()

@app.post("/web/users/create")
def web_create_user(request: Request, syncforge_session: str = Cookie(None), email: str = Form(...), password: str = Form(...)):
    if not syncforge_session:
        return RedirectResponse(url="/login", status_code=303)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    user_id = str(uuid.uuid4())
    hashed_password = get_password_hash(password)
    
    try:
        cursor.execute("INSERT INTO users (id, email, password_hash, role) VALUES (?, ?, ?, ?)", (user_id, email, hashed_password, "mobile_device"))
        conn.commit()
    except Exception as e:
        print(f"Erro: {e}")
    finally:
        conn.close()
        
    return RedirectResponse(url="/web/users", status_code=303)

@app.post("/web/users/delete")
def web_delete_user(request: Request, syncforge_session: str = Cookie(None), user_id: str = Form(...)):
    if not syncforge_session:
        return RedirectResponse(url="/login", status_code=303)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM folders WHERE user_id = ?", (user_id,))
        pastas = cursor.fetchall()
        for pasta in pastas:
            cursor.execute("DELETE FROM files_metadata WHERE folder_id = ?", (pasta["id"],))
        cursor.execute("DELETE FROM folders WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/web/users", status_code=303)