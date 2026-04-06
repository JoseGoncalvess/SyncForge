import sqlite3
import os

# O banco será criado na raiz do projeto
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "syncforge.db")

def delete_folder_from_db(folder_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        # Remove metadados primeiro por causa da Foreign Key
        cursor.execute("DELETE FROM files_metadata WHERE folder_id = ?", (folder_id,))
        cursor.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Erro ao excluir pasta: {e}")
        return False
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Tabela de Usuários (Mantida exatamente como a sua)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        device_token TEXT
    )
    ''')

    # 2. Tabela de Pastas (Folders) - Adicionada apenas a coluna server_path
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            name TEXT,
            sync_type TEXT,
            server_path TEXT DEFAULT '/var/syncforge',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    # 3. Tabela de Metadados de Arquivos (Mantida exatamente como a sua)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS files_metadata (
        id TEXT PRIMARY KEY,
        folder_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        file_hash TEXT NOT NULL,
        size_bytes INTEGER,
        is_ignored BOOLEAN DEFAULT 0,
        is_deleted BOOLEAN DEFAULT 0,
        FOREIGN KEY (folder_id) REFERENCES folders (id)
    )
    ''')

    # 4. Tabela de Histórico (Mantida exatamente como a sua)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS file_history (
        id TEXT PRIMARY KEY,
        file_id TEXT NOT NULL,
        version_hash TEXT NOT NULL,
        saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        storage_path TEXT NOT NULL,
        FOREIGN KEY (file_id) REFERENCES files_metadata (id)
    )
    ''')

    # 5. Tabela de Fila de Sincronização (Mantida exatamente como a sua)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sync_queue (
        id TEXT PRIMARY KEY,
        file_id TEXT NOT NULL,
        total_chunks INTEGER NOT NULL,
        uploaded_chunks INTEGER DEFAULT 0,
        status TEXT NOT NULL,
        FOREIGN KEY (file_id) REFERENCES files_metadata (id)
    )
    ''')

    conn.commit()
    conn.close()
    print(f"✅ Banco de dados SQLite inicializado com sucesso em: {DB_PATH}")

if __name__ == "__main__":
    init_db()