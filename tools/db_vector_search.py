"""
Busca vetorial de produtos usando pgvector no PostgreSQL.
Substitui a busca por trigram (db_search.py) por busca semântica com embeddings.
"""
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from config.settings import settings
from config.logger import setup_logger

logger = setup_logger(__name__)

# Cliente OpenAI para gerar embeddings
_openai_client = None

def _get_openai_client() -> OpenAI:
    """Retorna cliente OpenAI singleton."""
    global _openai_client
    if _openai_client is None:
        api_key = settings.openai_api_key
        if not api_key:
            raise ValueError("OPENAI_API_KEY não configurada no .env")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _generate_embedding(text: str) -> list[float]:
    """
    Gera embedding para um texto usando OpenAI.
    Usa o modelo text-embedding-3-small (1536 dimensões).
    """
    client = _get_openai_client()
    
    # Limpar e normalizar o texto
    text = text.strip()
    if not text:
        raise ValueError("Texto vazio para embedding")
    
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    
    return response.data[0].embedding


def search_products_vector(query: str, limit: int = 20) -> str:
    """
    Busca produtos por similaridade vetorial usando pgvector.
    
    Args:
        query: Texto de busca (nome do produto, descrição, etc.)
        limit: Número máximo de resultados (default: 20)
    
    Returns:
        String formatada com EANs encontrados no formato:
        EANS_ENCONTRADOS:
        1) 123456789 - PRODUTO A
        2) 987654321 - PRODUTO B
    """
    # Connection string do banco vetorial
    conn_str = settings.vector_db_connection_string
    if not conn_str:
        # Fallback para o banco de produtos padrão
        conn_str = settings.products_db_connection_string
    
    if not conn_str:
        return "Erro: String de conexão do banco vetorial não configurada."
    
    query = query.strip()
    if not query:
        return "Nenhum termo de busca informado."
    
    # Lista de produtos que são tipicamente hortifruti (frutas, legumes, verduras)
    # Quando detectamos um desses, adicionamos contexto para melhorar a busca
    HORTIFRUTI_KEYWORDS = [
        "tomate", "cebola", "batata", "alface", "cenoura", "pepino", "pimentao",
        "abobora", "abobrinha", "berinjela", "beterraba", "brocolis", "couve",
        "espinafre", "repolho", "rucula", "agriao", "alho", "gengibre", "mandioca",
        "banana", "maca", "laranja", "limao", "abacaxi", "melancia", "melao",
        "uva", "morango", "manga", "mamao", "abacate", "goiaba", "pera", "pessego",
        "ameixa", "kiwi", "coco", "maracuja", "acerola", "caju", "pitanga",
        "cheiro verde", "coentro", "salsa", "cebolinha", "hortela", "manjericao",
        "alecrim", "tomilho", "oregano", "louro", "frango", "carne", "peixe",
        "ovo", "leite", "queijo", "manteiga", "iogurte"
    ]
    
    query_lower = query.lower()
    enhanced_query = query
    
    # Se a busca é por um produto hortifruti, adiciona contexto para melhorar a relevância
    for keyword in HORTIFRUTI_KEYWORDS:
        if keyword in query_lower:
            # Adiciona contexto de categoria para melhorar a similaridade
            if keyword in ["frango", "carne", "peixe"]:
                enhanced_query = f"{query} açougue carnes"
            elif keyword in ["ovo", "leite", "queijo", "manteiga", "iogurte"]:
                enhanced_query = f"{query} laticínios"
            else:
                enhanced_query = f"{query} hortifruti legumes verduras frutas"
            logger.info(f"🎯 [BOOST] Query melhorada: '{enhanced_query}'")
            break
    
    logger.info(f"🔍 [VECTOR SEARCH] Buscando: '{query}'" + (f" → '{enhanced_query}'" if enhanced_query != query else ""))
    
    try:
        # 1. Gerar embedding da query (com boost se aplicável)
        query_embedding = _generate_embedding(enhanced_query)
        logger.info(f"✅ Embedding gerado ({len(query_embedding)} dimensões)")
        
        # 2. Buscar no banco por similaridade
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Busca por cosine similarity com BOOST para HORTI-FRUTI e FRIGORIFICO
                # Produtos de hortifruti/carnes recebem +0.15 no score
                sql = """
                    SELECT 
                        text,
                        metadata,
                        1 - (embedding <=> %s::vector) as base_similarity,
                        CASE 
                            WHEN metadata->>'setor' = 'HORTI-FRUTI' THEN 0.15
                            WHEN metadata->>'setor' = 'FRIGORIFICO' THEN 0.15
                            WHEN metadata->>'categoria' ILIKE '%%LEGUMES%%' THEN 0.10
                            WHEN metadata->>'categoria' ILIKE '%%FRUTAS%%' THEN 0.10
                            WHEN metadata->>'categoria' ILIKE '%%BOVINOS%%' THEN 0.10
                            WHEN metadata->>'categoria' ILIKE '%%SUINOS%%' THEN 0.10
                            ELSE 0
                        END as horti_boost,
                        (1 - (embedding <=> %s::vector)) + 
                        CASE 
                            WHEN metadata->>'setor' = 'HORTI-FRUTI' THEN 0.15
                            WHEN metadata->>'setor' = 'FRIGORIFICO' THEN 0.15
                            WHEN metadata->>'categoria' ILIKE '%%LEGUMES%%' THEN 0.10
                            WHEN metadata->>'categoria' ILIKE '%%FRUTAS%%' THEN 0.10
                            WHEN metadata->>'categoria' ILIKE '%%BOVINOS%%' THEN 0.10
                            WHEN metadata->>'categoria' ILIKE '%%SUINOS%%' THEN 0.10
                            ELSE 0
                        END as similarity
                    FROM produtos_vectors_ean
                    ORDER BY similarity DESC
                    LIMIT %s
                """
                
                # Converter embedding para string no formato pgvector
                embedding_str = f"[{','.join(map(str, query_embedding))}]"
                
                cur.execute(sql, (embedding_str, embedding_str, limit))
                results = cur.fetchall()
                
                logger.info(f"🔍 [VECTOR SEARCH] Encontrados {len(results)} resultados")
                
                # LOG detalhado para debug de relevância
                if results:
                    import re
                    for i, r in enumerate(results[:5]):  # Top 5 para debug
                        text = r.get("text", "")
                        sim = r.get("similarity", 0)
                        match = re.search(r'"produto":\s*"([^"]+)"', text)
                        nome = match.group(1) if match else text[:40]
                        cat_match = re.search(r'"categoria1":\s*"([^"]+)"', text)
                        cat = cat_match.group(1) if cat_match else ""
                        logger.debug(f"   {i+1}. [{sim:.4f}] {nome} | {cat}")
                
                if not results:
                    return "Nenhum produto encontrado com esse termo."
                
                # 3. Processar e formatar resultados
                return _format_results(results)
    
    except Exception as e:
        logger.error(f"❌ Erro na busca vetorial: {e}")
        return f"Erro ao buscar no banco vetorial: {str(e)}"


def _extract_ean_and_name(result: dict) -> tuple[str, str]:
    """
    Extrai EAN e nome do produto do resultado.
    O n8n salva os dados em 'text' (conteúdo) e 'metadata' (JSON).
    """
    text = result.get("text", "")
    metadata = result.get("metadata", {})
    
    # Tentar extrair do metadata primeiro (mais confiável)
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except:
            metadata = {}
    
    ean = ""
    nome = ""
    
    # Buscar EAN no metadata ou no texto
    if metadata:
        ean = str(metadata.get("codigo_ean", metadata.get("ean", "")))
        nome = metadata.get("produto", metadata.get("nome", ""))
    
    # Se não achou no metadata, parsear do texto
    if not ean or not nome:
        # O texto pode estar no formato: {"codigo_ean": 123, "produto": "NOME"}
        import re
        
        # Tentar encontrar codigo_ean no texto
        ean_match = re.search(r'"codigo_ean":\s*"?(\d+)"?', text)
        if ean_match:
            ean = ean_match.group(1)
        
        # Tentar encontrar produto no texto
        nome_match = re.search(r'"produto":\s*"([^"]+)"', text)
        if nome_match:
            nome = nome_match.group(1)
    
    # Fallback: usar o texto inteiro como nome
    if not nome and text:
        nome = text[:100]  # Truncar se muito longo
    
    return ean, nome


def _format_results(results: list[dict]) -> str:
    """Formata lista de resultados para o formato esperado pelo agente."""
    lines = ["EANS_ENCONTRADOS:"]
    seen_eans = set()  # Evitar duplicatas
    
    for i, row in enumerate(results, 1):
        ean, nome = _extract_ean_and_name(row)
        similarity = row.get("similarity", 0)
        
        # Pular se não tem EAN ou se já vimos esse EAN
        if not ean or ean in seen_eans:
            continue
        
        seen_eans.add(ean)
        
        # Formatar com score de similaridade para debug
        logger.debug(f"   {i}. {nome} (EAN: {ean}) [Similarity: {similarity:.3f}]")
        
        if ean and nome:
            lines.append(f"{len(seen_eans)}) {ean} - {nome}")
    
    if len(lines) == 1:  # Só tem o header
        return "Nenhum produto com EAN válido encontrado."
    
    return "\n".join(lines)
