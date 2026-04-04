from fastapi import APIRouter, HTTPException, status
from schemas import UserCreate, UserLogin, UserResponse
from utils.security import get_password_hash, verify_password, generate_device_token
from database.db_setup import DB_PATH
import sqlite3
import uuid

router = APIRouter(prefix="/auth", tags=["Autenticação"])

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row # Retorna os resultados como dicionários
    return conn

@router.post("/register", response_model=UserResponse)
def register_user(user: UserCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verifica se o email já existe
    cursor.execute("SELECT * FROM users WHERE email = ?", (user.email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="E-mail já cadastrado.")
    
    user_id = str(uuid.uuid4())
    hashed_pwd = get_password_hash(user.password)
    
    cursor.execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES (?, ?, ?, ?)",
        (user_id, user.email, hashed_pwd, user.role)
    )
    conn.commit()
    conn.close()
    
    return {"id": user_id, "email": user.email, "role": user.role, "device_token": None}

@router.post("/login", response_model=UserResponse)
def login(user: UserLogin):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE email = ?", (user.email,))
    db_user = cursor.fetchone()
    
    if not db_user or not verify_password(user.password, db_user["password_hash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    
    # Gera um novo token para este dispositivo e salva no banco
    new_token = generate_device_token()
    cursor.execute("UPDATE users SET device_token = ? WHERE id = ?", (new_token, db_user["id"]))
    conn.commit()
    conn.close()
    
    return {"id": db_user["id"], "email": db_user["email"], "role": db_user["role"], "device_token": new_token}