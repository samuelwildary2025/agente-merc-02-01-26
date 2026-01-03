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
    
    # Traduções de termos comuns para abreviações usadas no banco
    TERM_TRANSLATIONS = {
        "absorvente": "abs",
        "achocolatado": "achoc",
        "refrigerante": "refrig",
        "amaciante": "amac",
        "desodorante": "desod",
        "shampoo": "sh",
        "condicionador": "cond",
        "hotdog": "pao hot dog maxpaes",
        "cachorro quente": "pao hot dog maxpaes",
        "cachorro-quente": "pao hot dog maxpaes",
        "musarela": "queijo mussarela",
        "muçarela": "queijo mussarela", 
        "mussarela": "queijo mussarela",
        "presunto": "presunto fatiado",
        # Biscoitos e bolachas
        "creme crack": "bolacha cream cracker",
        "cream crack": "bolacha cream cracker",
        "cracker": "bolacha cream cracker",
        # Refrigerantes
        "guarana": "refrig guarana antarctica",
        "coca cola": "refrig coca cola",
        "coca-cola": "refrig coca cola",
        "fanta": "refrig fanta",
        "sprite": "refrig sprite",
        # Normalização de acentos (banco usa sem acento)
        "açúcar": "acucar cristal",
        "açucar": "acucar cristal",
        "café": "cafe",
        "maçã": "maca",
        "feijão": "feijao",
    }
    
    query_lower = query.lower()
    enhanced_query = query
    
    # Primeiro, aplicar traduções de termos
    for term, abbreviation in TERM_TRANSLATIONS.items():
        if term in query_lower:
            enhanced_query = query.replace(term, abbreviation).replace(term.capitalize(), abbreviation.upper())
            # Manter o termo original também para ajudar no contexto
            enhanced_query = f"{abbreviation} {query}"
            logger.info(f"🔄 [TRADUÇÃO] '{term}' → '{abbreviation}'")
            break
    
    # Se a busca é por um produto hortifruti, adiciona contexto para melhorar a relevância
    # MAS: Se a busca contém termos de produtos processados, NÃO aplicar boost de hortifruti
    PROCESSED_TERMS = ["doce", "suco", "molho", "extrato", "polpa", "geleia", "compota"]
    is_processed = any(term in query_lower for term in PROCESSED_TERMS)
    
    if not is_processed:
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
    else:
        logger.info(f"⏭️ [BOOST SKIP] Produto processado detectado, pulando boost hortifruti")
    
    logger.info(f"🔍 [VECTOR SEARCH] Buscando: '{query}'" + (f" → '{enhanced_query}'" if enhanced_query != query else ""))
    
    try:
        # 1. Gerar embedding da query (com boost se aplicável)
        query_embedding = _generate_embedding(enhanced_query)
        # 2. BUSCA HÍBRIDA usando função PostgreSQL (FTS + Vetorial com RRF)
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Converter embedding para string no formato pgvector
                embedding_str = f"[{','.join(map(str, query_embedding))}]"
                
                # 🔥 BUSCA HÍBRIDA V2: FTS + Vetorial + Boost para HORTI-FRUTI/FRIGORIFICO
                # Usa RRF (Reciprocal Rank Fusion) para combinar rankings
                # - full_text_weight: peso da busca por texto
                # - semantic_weight: peso da busca vetorial
                # - setor_boost: +0.5 para HORTI-FRUTI e FRIGORIFICO
                sql = """
                    SELECT 
                        h.text,
                        h.metadata,
                        h.score as similarity,
                        h.rank
                    FROM hybrid_search_v2(
                        %s,                    -- query_text
                        %s::vector,            -- query_embedding
                        %s,                    -- match_count
                        1.0,                   -- full_text_weight
                        1.0,                   -- semantic_weight
                        0.5,                   -- setor_boost (HORTI-FRUTI/FRIGORIFICO)
                        50                     -- rrf_k (parâmetro RRF)
                    ) h
                """
                
                logger.info(f"🔀 [HYBRID SEARCH] Query: '{query}' → '{enhanced_query}'")
                
                cur.execute(sql, (enhanced_query, embedding_str, limit))
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
                
                # 🔄 RETRY AUTOMÁTICO: Se o melhor score for muito baixo, tentar com palavras individuais
                MIN_SCORE_THRESHOLD = 0.50
                if results and results[0].get("similarity", 0) < MIN_SCORE_THRESHOLD:
                    logger.info(f"⚠️ [RETRY] Score baixo ({results[0].get('similarity', 0):.3f}), tentando busca por palavras individuais")
                    
                    # Dividir query em palavras (ignorar palavras muito curtas e stop words)
                    STOP_WORDS = {"de", "da", "do", "para", "com", "sem", "um", "uma", "kg", "und", "pct"}
                    words = [w for w in query.lower().split() if len(w) >= 3 and w not in STOP_WORDS]
                    
                    if len(words) >= 1:
                        best_results = results  # Manter resultados originais como fallback
                        best_score = results[0].get("similarity", 0)
                        
                        # Tentar cada palavra individual
                        for word in words:
                            # Gerar embedding para a palavra individual
                            word_embedding = _generate_embedding(word)
                            word_embedding_str = f"[{','.join(map(str, word_embedding))}]"
                            
                            cur.execute(sql, (word_embedding_str, word_embedding_str, limit))
                            word_results = cur.fetchall()
                            
                            if word_results:
                                word_score = word_results[0].get("similarity", 0)
                                # Aceitar se score for significativamente melhor
                                if word_score > best_score + 0.05:
                                    logger.info(f"✅ [RETRY] Palavra '{word}' encontrou melhores resultados: {word_score:.3f}")
                                    best_results = word_results
                                    best_score = word_score
                        
                        results = best_results
                
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
