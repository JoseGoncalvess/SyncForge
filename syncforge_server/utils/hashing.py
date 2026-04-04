import hashlib

def calculate_file_hash(file_path: str) -> str:
    """Calcula o hash SHA-256 de um arquivo lendo em pequenos blocos (poupa RAM)."""
    sha256_hash = hashlib.sha256()
    try:
        # Abre o arquivo em modo binário de leitura ("rb")
        with open(file_path, "rb") as f:
            # Lê em blocos de 4KB para não estourar a memória
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except FileNotFoundError:
        return ""