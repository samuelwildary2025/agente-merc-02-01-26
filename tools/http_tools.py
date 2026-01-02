"""
Ferramentas HTTP para interação com a API do Supermercado
"""
import requests
import json
from typing import Dict, Any
from config.settings import settings
from config.logger import setup_logger
from .db_vector_search import search_products_vector

logger = setup_logger(__name__)


def get_auth_headers() -> Dict[str, str]:
    """Retorna os headers de autenticação para as requisições"""
    return {
        "Authorization": settings.supermercado_auth_token,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def estoque(url: str) -> str:
    """
    Consulta o estoque e preço de produtos no sistema do supermercado.
    
    Args:
        url: URL completa para consulta (ex: .../api/produtos/consulta?nome=arroz)
    
    Returns:
        JSON string com informações do produto ou mensagem de erro
    """
    logger.info(f"Consultando estoque: {url}")
    
    try:
        response = requests.get(
            url,
            headers=get_auth_headers(),
            timeout=10
        )
        response.raise_for_status()
        
        data = response.json()
        
        # OTIMIZAÇÃO DE TOKENS: Filtrar apenas campos essenciais
        # A API retorna muitos dados inúteis (impostos, ncm, ids internos)
        # que gastam tokens desnecessariamente.
        def _filter_product(prod: Dict[str, Any]) -> Dict[str, Any]:
            keys_to_keep = [
                "id", "produto", "nome", "descricao", 
                "preco", "preco_venda", "valor", "valor_unitario",
                "estoque", "quantidade", "saldo", "disponivel"
            ]
            clean = {}
            for k, v in prod.items():
                if k.lower() in keys_to_keep or any(x in k.lower() for x in ["preco", "valor", "estoque"]):
                     # Ignora campos de imposto/fiscal mesmo se tiver palavras chave
                    if any(x in k.lower() for x in ["trib", "ncm", "fiscal", "custo", "margem"]):
                        continue
                    clean[k] = v
            return clean

        if isinstance(data, list):
            filtered_data = [_filter_product(p) for p in data]
        elif isinstance(data, dict):
            filtered_data = _filter_product(data)
        else:
            filtered_data = data
            
        logger.info(f"Estoque consultado com sucesso: {len(data) if isinstance(data, list) else 1} produto(s)")
        
        return json.dumps(filtered_data, indent=2, ensure_ascii=False)
    
    except requests.exceptions.Timeout:
        error_msg = "Erro: Timeout ao consultar estoque. Tente novamente."
        logger.error(error_msg)
        return error_msg
    
    except requests.exceptions.HTTPError as e:
        error_msg = f"Erro HTTP ao consultar estoque: {e.response.status_code} - {e.response.text}"
        logger.error(error_msg)
        return error_msg
    
    except requests.exceptions.RequestException as e:
        error_msg = f"Erro ao consultar estoque: {str(e)}"
        logger.error(error_msg)
        return error_msg
    
    except json.JSONDecodeError:
        error_msg = "Erro: Resposta da API não é um JSON válido."
        logger.error(error_msg)
        return error_msg


def pedidos(json_body: str) -> str:
    """
    Envia um pedido finalizado para o painel dos funcionários (dashboard).
    
    Args:
        json_body: JSON string com os detalhes do pedido
                   Exemplo: '{"cliente": "João", "itens": [{"produto": "Arroz", "quantidade": 1}]}'
    
    Returns:
        Mensagem de sucesso com resposta do servidor ou mensagem de erro
    """
    # Remove trailing slashed from base and from endpoint to ensure correct path
    base = settings.supermercado_base_url.rstrip("/")
    url = f"{base}/pedidos/"  # Barra final necessária para FastAPI
    logger.info(f"Enviando pedido para: {url}")
    
    # DEBUG: Log token being used (only first/last 4 chars for security)
    token = settings.supermercado_auth_token or ""
    token_preview = f"{token[:12]}...{token[-4:]}" if len(token) > 16 else token
    logger.info(f"🔑 Token usado: {token_preview}")
    
    try:
        # Validar JSON
        data = json.loads(json_body)
        logger.debug(f"Dados do pedido: {data}")
        
        response = requests.post(
            url,
            headers=get_auth_headers(),
            json=data,
            timeout=10
        )
        response.raise_for_status()
        
        result = response.json()
        success_msg = f"✅ Pedido enviado com sucesso!\n\nResposta do servidor:\n{json.dumps(result, indent=2, ensure_ascii=False)}"
        logger.info("Pedido enviado com sucesso")
        
        return success_msg
    
    except json.JSONDecodeError:
        error_msg = "Erro: O corpo da requisição não é um JSON válido."
        logger.error(error_msg)
        return error_msg
    
    except requests.exceptions.Timeout:
        error_msg = "Erro: Timeout ao enviar pedido. Tente novamente."
        logger.error(error_msg)
        return error_msg
    
    except requests.exceptions.HTTPError as e:
        error_msg = f"Erro HTTP ao enviar pedido: {e.response.status_code} - {e.response.text}"
        logger.error(error_msg)
        return error_msg
    
    except requests.exceptions.RequestException as e:
        error_msg = f"Erro ao enviar pedido: {str(e)}"
        logger.error(error_msg)
        return error_msg


def alterar(telefone: str, json_body: str) -> str:
    """
    Atualiza um pedido existente no painel dos funcionários (dashboard).
    
    Args:
        telefone: Telefone do cliente para identificar o pedido
        json_body: JSON string com os dados a serem atualizados
    
    Returns:
        Mensagem de sucesso com resposta do servidor ou mensagem de erro
    """
    # Remove caracteres não numéricos do telefone
    telefone_limpo = "".join(filter(str.isdigit, telefone))
    url = f"{settings.supermercado_base_url}/pedidos/telefone/{telefone_limpo}"
    
    logger.info(f"Atualizando pedido para telefone: {telefone_limpo}")
    
    try:
        # Validar JSON
        data = json.loads(json_body)
        logger.debug(f"Dados de atualização: {data}")
        
        response = requests.put(
            url,
            headers=get_auth_headers(),
            json=data,
            timeout=10
        )
        response.raise_for_status()
        
        result = response.json()
        success_msg = f"✅ Pedido atualizado com sucesso!\n\nResposta do servidor:\n{json.dumps(result, indent=2, ensure_ascii=False)}"
        logger.info("Pedido atualizado com sucesso")
        
        return success_msg
    
    except json.JSONDecodeError:
        error_msg = "Erro: O corpo da requisição não é um JSON válido."
        logger.error(error_msg)
        return error_msg


def ean_lookup(query: str) -> str:
    """
    Busca informações/EAN do produto via busca vetorial (pgvector + OpenAI embeddings).

    Args:
        query: Texto com o nome/descrição do produto ou entrada de chat.

    Returns:
        String com lista de EANs encontrados ou mensagem de erro.
    """
    logger.info(f"🔍 [VECTOR] Consultando pgvector: query='{query}'")
    return search_products_vector(query)



def estoque_preco(ean: str) -> str:
    """
    Consulta preço e disponibilidade pelo EAN.

    Monta a URL completa concatenando o EAN ao final de settings.estoque_ean_base_url.
    Exemplo: {base}/7891149103300

    Args:
        ean: Código EAN do produto (apenas dígitos).

    Returns:
        JSON string com informações do produto ou mensagem de erro amigável.
    """
    base = (settings.estoque_ean_base_url or "").strip().rstrip("/")
    if not base:
        msg = "Erro: ESTOQUE_EAN_BASE_URL não configurado no .env"
        logger.error(msg)
        return msg

    # manter apenas dígitos no EAN
    ean_digits = "".join(ch for ch in ean if ch.isdigit())
    if not ean_digits:
        msg = "Erro: EAN inválido. Informe apenas números."
        logger.error(msg)
        return msg

    url = f"{base}/{ean_digits}"
    logger.info(f"Consultando estoque_preco por EAN: {url}")

    headers = {
        "Accept": "application/json",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

        # resposta esperada: lista de objetos
        try:
            items = resp.json()
        except json.JSONDecodeError:
            txt = resp.text
            logger.warning("Resposta não é JSON válido; retornando texto bruto")
            return txt

        # Se vier um único objeto, normalizar para lista
        items = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])

        # Heurística de extração de preço
        PRICE_KEYS = (
            "vl_produto",
            "vl_produto_normal",
            "preco",
            "preco_venda",
            "valor",
            "valor_unitario",
            "preco_unitario",
            "atacadoPreco",
        )

        # Possíveis chaves de quantidade de estoque (remover da saída)
        # NOTA: qtd_produto é a chave principal, qtd_movimentacao NÃO é estoque real
        STOCK_QTY_KEYS = {
            "qtd_produto",  # Chave principal do sistema
            "estoque", "qtd", "qtde", "qtd_estoque", "quantidade", "quantidade_disponivel",
            "quantidadeDisponivel", "qtdDisponivel", "qtdEstoque", "estoqueAtual", "saldo",
            "qty", "quantity", "stock", "amount"
            # REMOVIDO: "qtd_movimentacao" - isso é movimentação, não estoque!
        }

        # Possíveis indicadores de disponibilidade
        STATUS_KEYS = ("situacao", "situacaoEstoque", "status", "statusEstoque")

        def _parse_float(val) -> float | None:
            try:
                s = str(val).strip()
                if not s:
                    return None
                # aceita formato brasileiro
                s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 and s.count(".") > 1 else s.replace(",", ".")
                return float(s)
            except Exception:
                return None

        def _has_positive_qty(d: Dict[str, Any]) -> bool:
            for k in STOCK_QTY_KEYS:
                if k in d:
                    v = d.get(k)
                    try:
                        n = float(str(v).replace(",", "."))
                        if n > 0:
                            return True
                    except Exception:
                        # ignore não numérico
                        pass
            return False

        def _is_available(d: Dict[str, Any]) -> bool:
            # 1. Verificar se está ativo (se a flag existir)
            # Se 'ativo' não existir, assume True por padrão
            is_active = d.get("ativo", True)
            if not is_active:
                logger.debug(f"Item filtrado: ativo=False")
                return False

            # 2. Verificar Estoque
            qty = _extract_qty(d)
            
            # Categorias de pesagem que ACEITAM estoque negativo/zerado
            # (Pois muitas vezes vendem antes de dar entrada na nota)
            cat = str(d.get("classificacao01", "")).upper()
            aceita_negativo = any(x in cat for x in ["FRIGORIFICO", "HORTI", "AÇOUGUE", "ACOUGUE", "LEGUMES", "VERDURAS", "AVES", "CARNES"])
            
            if aceita_negativo:
                 # Para pesáveis, aceitamos qualquer coisa diferente de zero?
                 # Ou aceitamos até zero se estiver ativo?
                 # O usuário disse: "não leve em consideração a quantidade negativa".
                 # Vou assumir que se estiver ativo, tá valendo, independente do estoque (mesmo 0 ou negativo).
                 # Mas 0 geralmente é indisponível real. Vou manter a regra de aceitar negativo, mas bloquear 0.
                 if qty is not None and qty != 0:
                     return True
                 # Se for 0, bloqueia?
                 # O frango estava com -1174 (negativo). O Tomate 145 (positivo).
                 # Se vier 0, provavelmente não tem.
                 return False

            # Para os demais (Mercearia, Bebidas, etc), estoque deve ser POSITIVO
            if qty is not None and qty > 0:
                return True
            
            # Se chegou aqui, ou é 0, ou é negativo em categoria que não pode
            logger.debug(f"Item filtrado: quantidade={qty} (Categoria: {cat})")
            return False

        def _extract_qty(d: Dict[str, Any]) -> float | None:
            for k in STOCK_QTY_KEYS:
                if k in d:
                    try:
                        return float(str(d.get(k)).replace(',', '.'))
                    except Exception:
                        pass
            return None

        def _extract_price(d: Dict[str, Any]) -> float | None:
            for k in PRICE_KEYS:
                if k in d:
                    val = _parse_float(d.get(k))
                    if val is not None:
                        return val
            return None

        # [OTIMIZAÇÃO] Filtro estrito para saída
        sanitized: list[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if not _is_available(it):
                continue  # manter apenas itens com estoque/disponibilidade

            # Cria dict limpo apenas com campos essenciais
            clean = {}
            
            # Copiar apenas identificadores básicos se existirem
            for k in ["produto", "nome", "descricao", "id", "ean", "cod_barra"]:
                if k in it: clean[k] = it[k]

            # Normalizar disponibilidade (se passou no _is_available, é True)
            clean["disponibilidade"] = True

            # Normalizar preço em campo unificado
            price = _extract_price(it)
            if price is not None:
                clean["preco"] = price

            qty = _extract_qty(it)
            if qty is not None:
                clean["quantidade"] = qty

            sanitized.append(clean)

        logger.info(f"EAN {ean_digits}: {len(sanitized)} item(s) disponíveis após filtragem")

        return json.dumps(sanitized, indent=2, ensure_ascii=False)

    except requests.exceptions.Timeout:
        msg = "Erro: Timeout ao consultar preço/estoque por EAN. Tente novamente."
        logger.error(msg)
        return msg
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        body = getattr(e.response, "text", "")
        msg = f"Erro HTTP ao consultar EAN: {status} - {body}"
        logger.error(msg)
        return msg
    except requests.exceptions.RequestException as e:
        msg = f"Erro ao consultar EAN: {str(e)}"
        logger.error(msg)
        return msg

    # [Cleanup] Removido bloco duplicado de ean_lookup antigo fora de função


# ============================================
# BUSCA EM LOTE (PARALELA)
# ============================================

def busca_lote_produtos(produtos: list[str]) -> str:
    """
    Busca múltiplos produtos em PARALELO para otimizar performance.
    
    Em vez de buscar sequencialmente (10s × N produtos), busca todos ao mesmo tempo.
    
    Args:
        produtos: Lista de nomes de produtos para buscar
        
    Returns:
        String formatada com todos os produtos encontrados e seus preços
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time
    
    start_time = time.time()
    logger.info(f"🚀 Iniciando busca em lote para {len(produtos)} produtos")
    
    def buscar_produto_completo(produto: str) -> dict:
        """Busca EAN e depois preço de um produto"""
        try:
            # 1. Buscar EAN (Postgres)
            # IMPORTANTE: ean_lookup retorna uma string formatada (EANS_ENCONTRADOS: ...)
            ean_result = ean_lookup(produto)
            
            # Se a busca no banco falhou ou não achou nada
            if "EANS_ENCONTRADOS" not in ean_result:
                logger.warning(f"❌ [BUSCA LOTE] Banco não retornou resultados para '{produto}'")
                return {"produto": produto, "erro": "Não encontrado", "preco": None}
            
            # 2. Parse da string de retorno do ean_lookup para extrair lista de dicts
            # Formato esperado: "EANS_ENCONTRADOS:\n1) 123 - PRODUTO A\n2) 456 - PRODUTO B"
            import re
            
            linhas = ean_result.split('\n')
            candidatos = []
            
            for linha in linhas:
                # Procurar padrão: número) EAN - NOME
                # Regex flexível para pegar "1) 123 - NOME"
                match = re.match(r'\d+\)\s*(\d+)\s*-\s*(.+)', linha.strip())
                if match:
                    ean = match.group(1)
                    nome = match.group(2).strip()
                    candidatos.append({"ean": ean, "nome": nome})
            
            if not candidatos:
                logger.warning(f"❌ [BUSCA LOTE] Falha ao fazer parse dos candidatos para '{produto}'. Texto: {ean_result[:50]}...")
                return {"produto": produto, "erro": "EAN não extraído", "preco": None}
            
            # 3. Encontrar os melhores candidatos (Ranking)
            PREFERENCIAS = {
                "frango": ["abatido"],
                "leite": ["liquido"],
                "arroz": ["tipo 1"],
                "acucar": ["cristal"],
                "feijao": ["carioca"],
                "oleo": ["soja"],
                "tomate": ["tomate kg"],
                "cebola": ["cebola kg"],
                "batata": ["batata kg"],
                "calabresa": ["calabresa kg"], # Prioriza a granel
            }
            
            produto_lower = produto.lower()
            
            # Termos de preferência para este produto (se houver)
            termos_preferidos = []
            for chave, termos in PREFERENCIAS.items():
                if chave in produto_lower:
                    termos_preferidos = termos
                    break

            candidatos_pontuados = []

            for c in candidatos:
                nome_lower = c["nome"].lower()
                score = 0
                
                # 1. Match de palavras da busca (Base)
                score += sum(2 for palavra in produto_lower.split() if palavra in nome_lower)
                
                # 2. Bonus por match exato da frase
                if produto_lower in nome_lower:
                    score += 5
                
                # 3. Bonus por Preferências
                for i, termo in enumerate(termos_preferidos):
                    if termo in nome_lower:
                        score += (10 - i)
                        break
                
                # 4. Penalidade por tamanho
                score -= len(nome_lower) * 0.05
                
                candidatos_pontuados.append((score, c))
            
            # Ordenar por score (maior para menor)
            candidatos_pontuados.sort(key=lambda x: x[0], reverse=True)
            
            # 4. Tentar buscar preço nos Top 3 candidatos (Retry Logic)
            for score, candidato in candidatos_pontuados[:3]:
                ean = candidato["ean"]
                nome_candidato = candidato["nome"]
                logger.info(f"👉 [BUSCA LOTE] Tentando: '{nome_candidato}' (EAN: {ean}) | Score: {score:.2f}")
                
                preco_result = estoque_preco(ean)
                
                try:
                    preco_data = json.loads(preco_result)
                    if preco_data and isinstance(preco_data, list) and len(preco_data) > 0:
                        item = preco_data[0]
                        nome = item.get("produto", item.get("nome", produto))
                        preco = item.get("preco", 0)
                        logger.info(f"✅ [BUSCA LOTE] Sucesso com '{nome}' (R$ {preco})")
                        return {"produto": nome, "erro": None, "preco": preco, "ean": ean}
                    else:
                        logger.info(f"⚠️ [BUSCA LOTE] '{nome_candidato}' sem estoque/preço. Tentando próximo...")
                except Exception as e:
                    logger.warning(f"Erro ao processar retorno de preço para {ean}: {e}")
            
            logger.warning(f"❌ [BUSCA LOTE] Nenhum dos top 3 candidatos para '{produto}' tinha estoque.")
            return {"produto": produto, "erro": "Indisponível (sem estoque)", "preco": None}
            
        except Exception as e:
            logger.error(f"Erro ao buscar {produto}: {e}")
            return {"produto": produto, "erro": str(e), "preco": None}
    
    # Executar buscas em paralelo (máximo 5 threads para não sobrecarregar)
    resultados = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(buscar_produto_completo, p): p for p in produtos}
        
        for future in as_completed(futures):
            resultado = future.result()
            resultados.append(resultado)
    
    elapsed = time.time() - start_time
    logger.info(f"✅ Busca em lote concluída em {elapsed:.2f}s para {len(produtos)} produtos")
    
    # Formatar resposta
    encontrados = []
    nao_encontrados = []
    
    for r in resultados:
        if r["preco"] is not None:
            encontrados.append(f"• {r['produto']} - R${r['preco']:.2f}")
        else:
            nao_encontrados.append(r['produto'])
    
    resposta = []
    if encontrados:
        resposta.append("PRODUTOS_ENCONTRADOS:")
        resposta.extend(encontrados)
    
    if nao_encontrados:
        resposta.append(f"\nNÃO_ENCONTRADOS: {', '.join(nao_encontrados)}")
    
    return "\n".join(resposta) if resposta else "Nenhum produto encontrado."
