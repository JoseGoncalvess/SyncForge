import bcrypt
import uuid

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica se a senha digitada bate com o hash do banco."""
    # O bcrypt exige que as strings sejam convertidas para bytes
    password_bytes = plain_password.encode('utf-8')
    hash_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)

def get_password_hash(password: str) -> str:
    """Gera o hash irreversível da senha para salvar no banco."""
    pwd_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(pwd_bytes, salt)
    # Retornamos como string normal para salvar no SQLite
    return hashed_password.decode('utf-8')

def generate_device_token() -> str:
    """Gera um token único para a sessão do dispositivo logado."""
    return str(uuid.uuid4())