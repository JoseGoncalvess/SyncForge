import os

# Define a pasta raiz onde os cofres ficarão guardados no servidor
BASE_STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server_data")
TEMP_DIR = os.path.join(BASE_STORAGE_DIR, "temp_chunks")

# Garante que as pastas físicas existam assim que o servidor rodar
os.makedirs(BASE_STORAGE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

def get_safe_file_path(folder_id: str, relative_path: str) -> str:
    """Evita ataques de Path Traversal e gera o caminho físico final do arquivo, criando subpastas se necessário."""
    # Remove barras iniciais para evitar caminhos absolutos acidentais
    safe_relative_path = relative_path.lstrip("/\\")
    
    # Monta o caminho completo de onde o arquivo deve ficar
    final_path = os.path.join(BASE_STORAGE_DIR, folder_id, safe_relative_path)
    
    # Extrai apenas a parte da "pasta" desse caminho completo
    final_dir = os.path.dirname(final_path)
    
    # Cria TODAS as pastas e subpastas necessárias no caminho (ex: /folder_id/projetos/)
    os.makedirs(final_dir, exist_ok=True)
    
    return final_path

def merge_chunks(queue_id: str, total_chunks: int, final_path: str):
    """Lê todos os pedaços temporários em ordem e os une no arquivo final."""
    # Abre o arquivo final no modo 'append binary' (ab) ou 'write binary' (wb)
    with open(final_path, 'wb') as final_file:
        for i in range(total_chunks):
            # O nome do chunk temporário será algo como: "queue_id_0", "queue_id_1"
            chunk_path = os.path.join(TEMP_DIR, f"{queue_id}_{i}")
            
            with open(chunk_path, 'rb') as chunk_file:
                final_file.write(chunk_file.read())
            
            # Após escrever, apaga o pedaço temporário para limpar o disco do servidor
            os.remove(chunk_path)