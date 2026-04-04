from fastapi import APIRouter, HTTPException, File, UploadFile, Form
from schemas import FolderCreate, FolderResponse, FileMetadataResponse
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from schemas import FileUploadInit, FileUploadInitResponse # (Adicione isso lá nos imports do topo)
from utils.storage import TEMP_DIR, get_safe_file_path, merge_chunks
import os
from database.db_setup import DB_PATH
import sqlite3
import uuid
from typing import List

router = APIRouter(prefix="/sync", tags=["Sincronização e Metadados"])

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@router.post("/folders", response_model=FolderResponse)
def create_folder(folder: FolderCreate):
    """Cria um novo diretório monitorado (Cofre)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    folder_id = str(uuid.uuid4())
    
    cursor.execute(
        "INSERT INTO folders (id, name, sync_type) VALUES (?, ?, ?)",
        (folder_id, folder.name, folder.sync_type)
    )
    conn.commit()
    
    cursor.execute("SELECT * FROM folders WHERE id = ?", (folder_id,))
    new_folder = cursor.fetchone()
    conn.close()
    
    return dict(new_folder)

@router.get("/folders/{folder_id}/metadata", response_model=List[FileMetadataResponse])
def get_folder_metadata(folder_id: str):
    """Retorna a Árvore de Hashes para o cliente resolver o Cold Start."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verifica se a pasta existe
    cursor.execute("SELECT id FROM folders WHERE id = ?", (folder_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Pasta não encontrada.")
    
    # Busca todos os arquivos que não foram deletados permanentemente
    cursor.execute("SELECT * FROM files_metadata WHERE folder_id = ?", (folder_id,))
    metadata = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in metadata]


@router.post("/files/init-upload", response_model=FileUploadInitResponse)
def init_file_upload(upload_req: FileUploadInit):
    """O cliente avisa que vai começar a mandar um arquivo fracionado."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Verifica se a pasta existe
    cursor.execute("SELECT id FROM folders WHERE id = ?", (upload_req.folder_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Pasta não encontrada.")
    
    # 2. Registra o arquivo na tabela de Metadados (se não existir)
    cursor.execute(
        "SELECT id, file_hash FROM files_metadata WHERE folder_id = ? AND relative_path = ?", 
        (upload_req.folder_id, upload_req.relative_path)
    )
    existing_file = cursor.fetchone()
    
    if existing_file:
        file_id = existing_file["id"]
        # Se o hash for o mesmo, o arquivo já está sincronizado!
        if existing_file["file_hash"] == upload_req.file_hash:
            conn.close()
            return {"file_id": file_id, "queue_id": "", "status": "already_synced", "uploaded_chunks": upload_req.total_chunks}
        
        # Atualiza os dados do arquivo existente para o novo hash/tamanho
        cursor.execute(
            "UPDATE files_metadata SET file_hash = ?, size_bytes = ? WHERE id = ?",
            (upload_req.file_hash, upload_req.size_bytes, file_id)
        )
    else:
        # É um arquivo novo
        file_id = str(uuid.uuid4())
        cursor.execute(
            """INSERT INTO files_metadata (id, folder_id, relative_path, file_hash, size_bytes) 
               VALUES (?, ?, ?, ?, ?)""",
            (file_id, upload_req.folder_id, upload_req.relative_path, upload_req.file_hash, upload_req.size_bytes)
        )
    
    # 3. Cria a Fila de Sincronização (Chunks)
    queue_id = str(uuid.uuid4())
    # Limpa filas antigas desse arquivo que possam ter travado
    cursor.execute("DELETE FROM sync_queue WHERE file_id = ?", (file_id,))
    
    cursor.execute(
        """INSERT INTO sync_queue (id, file_id, total_chunks, uploaded_chunks, status) 
           VALUES (?, ?, ?, 0, 'pending')""",
        (queue_id, file_id, upload_req.total_chunks)
    )
    
    conn.commit()
    conn.close()
    
    return {
        "file_id": file_id,
        "queue_id": queue_id,
        "status": "ready_for_chunks",
        "uploaded_chunks": 0
    }
@router.post("/files/upload-chunk")
def upload_chunk(
    queue_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk_file: UploadFile = File(...)
):
    """Recebe um pedaço do arquivo, salva no disco e junta tudo se for o último."""
    # 1. Salva o pedaço recebido na pasta temporária
    chunk_path = os.path.join(TEMP_DIR, f"{queue_id}_{chunk_index}")
    with open(chunk_path, "wb") as buffer:
        buffer.write(chunk_file.file.read())
    
    # 2. Atualiza o banco de dados
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM sync_queue WHERE id = ?", (queue_id,))
    queue = cursor.fetchone()
    
    if not queue:
        conn.close()
        raise HTTPException(status_code=404, detail="Fila de sincronização não encontrada.")
    
    # Incrementa a contagem de pedaços recebidos
    new_count = queue["uploaded_chunks"] + 1
    cursor.execute("UPDATE sync_queue SET uploaded_chunks = ? WHERE id = ?", (new_count, queue_id))
    
    # 3. Verifica se este era o último pedaço
    if new_count == queue["total_chunks"]:
        cursor.execute("UPDATE sync_queue SET status = 'merging' WHERE id = ?", (queue_id,))
        conn.commit()
        
        # Busca os dados do arquivo para saber onde salvar a versão final
        cursor.execute("SELECT folder_id, relative_path FROM files_metadata WHERE id = ?", (queue["file_id"],))
        file_meta = cursor.fetchone()
        
        final_path = get_safe_file_path(file_meta["folder_id"], file_meta["relative_path"])
        
        # Chama a função de costura que criamos no storage.py
        merge_chunks(queue_id, queue["total_chunks"], final_path)
        
        cursor.execute("UPDATE sync_queue SET status = 'completed' WHERE id = ?", (queue_id,))
        conn.commit()
        conn.close()
        
        return {"status": "completed", "message": "Arquivo sincronizado com sucesso!"}
    
    conn.commit()
    conn.close()
    
    return {"status": "transferring", "uploaded_chunks": new_count}

@router.get("/files/{file_id}/download")
def download_file(file_id: str):
    """Permite ao cliente baixar o arquivo físico. Suporta streaming nativo."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Busca os metadados do arquivo
    cursor.execute("SELECT folder_id, relative_path, is_deleted FROM files_metadata WHERE id = ?", (file_id,))
    file_meta = cursor.fetchone()
    conn.close()
    
    if not file_meta:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no banco de dados.")
    
    if file_meta["is_deleted"]:
        raise HTTPException(status_code=410, detail="Este arquivo foi marcado como deletado.")
    
    # 2. Descobre o caminho físico do arquivo
    final_path = get_safe_file_path(file_meta["folder_id"], file_meta["relative_path"])
    
    # 3. Verifica se o arquivo realmente existe no disco
    if not os.path.exists(final_path):
        raise HTTPException(status_code=404, detail="O arquivo físico não foi encontrado no servidor.")
    
    # O nome original do arquivo para o navegador/cliente saber como salvar
    file_name = os.path.basename(final_path)
    
    # 4. Inicia o streaming do arquivo para o cliente
    return FileResponse(
        path=final_path, 
        filename=file_name,
        media_type="application/octet-stream" # Indica que é um download binário
    )