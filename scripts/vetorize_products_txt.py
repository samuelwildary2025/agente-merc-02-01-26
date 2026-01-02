"""
Script para vetorizar produtos do novo formato (texto plano).
Cada linha: ean [EAN] [NOME] setor [SETOR] categoria [CATEGORIA] subcategoria [SUBCATEGORIA]

Uso: python scripts/vetorize_products_txt.py
"""
import os
import re
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
BATCH_SIZE = 50
TABLE_NAME = "produtos_vectors_ean"
INPUT_FILE = "produtos_processados (1).txt"

# Arquivo de progresso
PROGRESS_FILE = "/tmp/vetorize_txt_progress.txt"


def parse_line(line):
    """
    Parseia uma linha no formato:
    ean 102 ABACATE  kg setor HORTI-FRUTI categoria FRUTAS subcategoria 
    
    Retorna dict com ean, nome, setor, categoria, subcategoria
    """
    # Regex mais flexível para aceitar categorias vazias
    pattern = r'^ean\s+(\S+)\s+(.+?)\s+setor\s+(.*?)\s+categoria\s*(.*?)\s*subcategoria\s*(.*)$'
    match = re.match(pattern, line.strip())
    
    if match:
        ean, nome, setor, categoria, subcategoria = match.groups()
        return {
            "ean": ean.strip(),
            "nome": nome.strip(),
            "setor": setor.strip() if setor else "",
            "categoria": categoria.strip() if categoria else "",
            "subcategoria": subcategoria.strip() if subcategoria else ""
        }
    return None


def format_for_embedding(product):
    """
    Formata o produto para embedding.
    Ex: "ABACATE kg - setor: HORTI-FRUTI - categoria: FRUTAS"
    """
    text = product["nome"]
    if product["setor"]:
        text += f" - setor: {product['setor']}"
    if product["categoria"]:
        text += f" - categoria: {product['categoria']}"
    if product["subcategoria"]:
        text += f" - subcategoria: {product['subcategoria']}"
    return text


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
                time.sleep(2 ** attempt)
            else:
                raise


def get_last_processed():
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
    print("=" * 60)
    print("VETORIZAÇÃO DE PRODUTOS (NOVO FORMATO)")
    print("=" * 60)
    
    if not OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY não configurada!")
        return
    
    client = OpenAI(api_key=OPENAI_API_KEY)
    
    # Ler arquivo
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    total = len(lines)
    print(f"📦 Total de linhas no arquivo: {total}")
    
    # Parsear todas as linhas
    products = []
    for i, line in enumerate(lines):
        parsed = parse_line(line)
        if parsed:
            parsed["text"] = format_for_embedding(parsed)
            products.append(parsed)
        else:
            print(f"   ⚠️ Linha {i+1} não parseada: {line[:50]}...")
    
    print(f"✅ Produtos parseados: {len(products)}")
    
    start_offset = get_last_processed()
    if start_offset > 0:
        print(f"📌 Retomando de onde parou: offset {start_offset}")
    
    # Conectar ao banco e limpar tabela se for do início
    with psycopg2.connect(VECTOR_DB_CONNECTION_STRING) as conn:
        with conn.cursor() as cur:
            if start_offset == 0:
                print("🗑️ Limpando tabela existente...")
                cur.execute(f"DELETE FROM {TABLE_NAME}")
                conn.commit()
                print("   Tabela limpa.")
            
            processed = start_offset
            errors = 0
            
            while processed < len(products):
                try:
                    batch = products[processed:processed + BATCH_SIZE]
                    
                    # Extrair textos para embedding
                    texts = [p["text"] for p in batch]
                    
                    # Gerar embeddings
                    embeddings = generate_embeddings_batch(client, texts)
                    
                    # Inserir no banco
                    for product, embedding in zip(batch, embeddings):
                        embedding_str = f"[{','.join(map(str, embedding))}]"
                        
                        # Metadata como JSON
                        metadata = {
                            "ean": product["ean"],
                            "setor": product["setor"],
                            "categoria": product["categoria"],
                            "subcategoria": product["subcategoria"]
                        }
                        
                        cur.execute(f"""
                            INSERT INTO {TABLE_NAME} (text, embedding, metadata)
                            VALUES (%s, %s::vector, %s::jsonb)
                        """, (product["text"], embedding_str, str(metadata).replace("'", '"')))
                    
                    conn.commit()
                    processed += len(batch)
                    
                    # Salvar progresso
                    save_progress(processed)
                    
                    # Progress
                    pct = (processed / len(products)) * 100
                    print(f"   ✅ {processed}/{len(products)} ({pct:.1f}%)")
                    
                    # Rate limiting
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
