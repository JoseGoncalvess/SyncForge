from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime


# O que o cliente envia ao criar conta
class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "client" # Pode ser 'admin' ou 'client'

# O que o cliente envia para fazer login
class UserLogin(BaseModel):
    email: str
    password: str

# O que o servidor devolve após o login (sem revelar a senha, claro)
class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    device_token: Optional[str] = None


class FolderCreate(BaseModel):
    name: str
    sync_type: str # 'mirror' ou 'storage'

class FolderResponse(BaseModel):
    id: str
    name: str
    sync_type: str
    created_at: datetime

class FileMetadataResponse(BaseModel):
    id: str
    folder_id: str
    relative_path: str
    file_hash: str
    size_bytes: int
    is_ignored: bool
    is_deleted: bool

class FileUploadInit(BaseModel):
    folder_id: str
    relative_path: str # Ex: "projetos/ideia.md"
    file_hash: str     # O Hash SHA-256 final que o celular calculou
    size_bytes: int
    total_chunks: int

class FileUploadInitResponse(BaseModel):
    file_id: str
    queue_id: str
    status: str
    uploaded_chunks: int