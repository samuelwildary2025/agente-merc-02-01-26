"""
Script para revetorizar todos os produtos com embeddings consistentes.
Usa o modelo text-embedding-3-small da OpenAI.
Suporta retomada de onde parou.

Uso: python scripts/revetorize_products.py
"""
import os
import json
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

# Configurações
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
VECTOR_DB_CONNECTION_STRING = os.environ.get(
    "VECTOR_DB_CONNECTION_STRING",
    "postgres://poostgres:85885885@31.97.252.6:8877/agente-db-pgvectorstore?sslmode=disable"
)
EMBEDDING_MODEL = "text-embedding-3-small"
BATCH_SIZE = 50  # Menor para mais checkpoints
TABLE_NAME = "produtos_vectors_ean"

# Arquivo para rastrear progresso
PROGRESS_FILE = "/tmp/revetorize_progress.txt"

def get_openai_client():
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY não configurada!")
    return OpenAI(api_key=api_key)

def generate_embeddings_batch(client, texts):
    """Gera embeddings para uma lista de textos em batch."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"      Retry {attempt + 1}/{max_retries}: {e}")
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise

def get_last_processed_offset():
    """Retorna o offset de onde parou."""
    try:
        with open(PROGRESS_FILE, "r") as f:
            return int(f.read().strip())
    except:
        return 0

def save_progress(offset):
    """Salva o progresso atual."""
    with open(PROGRESS_FILE, "w") as f:
        f.write(str(offset))

def main():
    global api_key
    api_key = OPENAI_API_KEY
    
    print("=" * 60)
    print("REVETORIZAÇÃO DE PRODUTOS")
    print("=" * 60)
    
    client = OpenAI(api_key=api_key)
    
    start_offset = get_last_processed_offset()
    if start_offset > 0:
        print(f"📌 Retomando de onde parou: offset {start_offset}")
    
    with psycopg2.connect(VECTOR_DB_CONNECTION_STRING) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Contar total de produtos
            cur.execute(f"SELECT COUNT(*) as total FROM {TABLE_NAME}")
            total = cur.fetchone()['total']
            print(f"📦 Total de produtos no banco: {total}")
            
            # 2. Buscar produtos a partir do offset
            print(f"\n🔄 Processando em batches de {BATCH_SIZE}...")
            
            processed = start_offset
            errors = 0
            
            while processed < total:
                try:
                    # Buscar batch
                    cur.execute(f"""
                        SELECT id, text FROM {TABLE_NAME} 
                        ORDER BY id 
                        OFFSET {processed} 
                        LIMIT {BATCH_SIZE}
                    """)
                    batch = cur.fetchall()
                    
                    if not batch:
                        break
                    
                    # Extrair textos
                    texts = [p['text'] for p in batch]
                    ids = [p['id'] for p in batch]
                    
                    # Gerar embeddings
                    embeddings = generate_embeddings_batch(client, texts)
                    
                    # Atualizar no banco
                    for product_id, embedding in zip(ids, embeddings):
                        embedding_str = f"[{','.join(map(str, embedding))}]"
                        cur.execute(
                            f"UPDATE {TABLE_NAME} SET embedding = %s::vector WHERE id = %s",
                            (embedding_str, product_id)
                        )
                    
                    conn.commit()
                    processed += len(batch)
                    
                    # Salvar progresso
                    save_progress(processed)
                    
                    # Progress
                    pct = (processed / total) * 100
                    print(f"   ✅ {processed}/{total} ({pct:.1f}%)")
                    
                    # Rate limiting - mais conservador
                    time.sleep(0.3)
                    
                except KeyboardInterrupt:
                    print(f"\n⏸️  Pausado em {processed}. Execute novamente para continuar.")
                    save_progress(processed)
                    return
                except Exception as e:
                    errors += 1
                    print(f"   ❌ Erro: {e}")
                    if errors > 10:
                        print("   Muitos erros, abortando...")
                        break
                    time.sleep(2)
                    continue
            
            print(f"\n{'='*60}")
            print(f"✅ CONCLUÍDO!")
            print(f"   Processados: {processed}")
            print(f"   Erros: {errors}")
            print(f"{'='*60}")
            
            # Limpar progresso
            try:
                os.remove(PROGRESS_FILE)
            except:
                pass

if __name__ == "__main__":
    main()
