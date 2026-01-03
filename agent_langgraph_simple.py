"""
Agente de IA para Atendimento de Supermercado usando LangGraph
Versão com suporte a VISÃO e Pedidos com Comprovante
"""

from typing import Dict, Any, TypedDict, Sequence, List
import re
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_community.callbacks import get_openai_callback
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition, create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from pathlib import Path
import json
import os

from config.settings import settings
from config.logger import setup_logger
from tools.http_tools import estoque, pedidos, alterar, ean_lookup, estoque_preco, busca_lote_produtos
from tools.time_tool import get_current_time, search_message_history
from tools.redis_tools import (
    mark_order_sent, 
    add_item_to_cart, 
    get_cart_items, 
    remove_item_from_cart, 
    clear_cart
)
from memory.limited_postgres_memory import LimitedPostgresChatMessageHistory

logger = setup_logger(__name__)

# ============================================
# Definição das Ferramentas (Tools)
# ============================================

@tool
def estoque_tool(url: str) -> str:
    """
    Consultar estoque e preço atual dos produtos no sistema do supermercado.
    Ex: 'https://.../api/produtos/consulta?nome=arroz'
    """
    return estoque(url)

@tool
def add_item_tool(telefone: str, produto: str, quantidade: float = 1.0, observacao: str = "", preco: float = 0.0, unidades: int = 0) -> str:
    """
    Adicionar um item ao carrinho de compras do cliente.
    USAR IMEDIATAMENTE quando o cliente demonstrar intenção de compra.
    
    Para produtos vendidos por KG (frutas, legumes, carnes):
    - quantidade: peso em kg (ex: 0.45 para 450g)
    - unidades: número de unidades pedidas (ex: 3 para 3 tomates)
    - preco: preço por kg
    
    Para produtos unitários:
    - quantidade: número de itens
    - unidades: deixar 0
    - preco: preço por unidade
    """
    item = {
        "produto": produto,
        "quantidade": quantidade,  # Peso em kg OU quantidade de unidades
        "unidades": unidades,      # Número de unidades (se aplicável)
        "observacao": observacao,
        "preco": preco            # Preço por kg OU por unidade
    }
    import json as json_lib
    if add_item_to_cart(telefone, json_lib.dumps(item, ensure_ascii=False)):
        if unidades > 0:
            return f"✅ Item '{produto}' ({unidades} unidades, ~{quantidade:.3f}kg) adicionado ao carrinho."
        return f"✅ Item '{produto}' ({quantidade}) adicionado ao carrinho."
    return "❌ Erro ao adicionar item. Tente novamente."

@tool
def view_cart_tool(telefone: str) -> str:
    """
    Ver os itens atuais no carrinho do cliente.
    """
    items = get_cart_items(telefone)
    if not items:
        return "🛒 O carrinho está vazio."
    
    summary = ["🛒 **Carrinho Atual:**"]
    total_estimado = 0.0
    for i, item in enumerate(items):
        qtd = item.get("quantidade", 1)
        nome = item.get("produto", "?")
        obs = item.get("observacao", "")
        preco = item.get("preco", 0.0)
        subtotal = qtd * preco
        total_estimado += subtotal
        
        desc = f"{i+1}. {nome} (x{qtd})"
        if preco > 0:
            desc += f" - R$ {subtotal:.2f}"
        if obs:
            desc += f" [Obs: {obs}]"
        summary.append(desc)
    
    if total_estimado > 0:
        summary.append(f"\n💰 **Total Estimado:** R$ {total_estimado:.2f}")
        
    return "\n".join(summary)

@tool
def remove_item_tool(telefone: str, item_index: int) -> str:
    """
    Remover um item do carrinho pelo número (índice 1-based, como mostrado no view_cart).
    Ex: Para remover o item 1, passe 1.
    """
    # Converter de 1-based para 0-based
    idx = int(item_index) - 1
    if remove_item_from_cart(telefone, idx):
        return f"✅ Item {item_index} removido do carrinho."
    return "❌ Erro ao remover item (índice inválido?)."

@tool
def finalizar_pedido_tool(cliente: str, telefone: str, endereco: str, forma_pagamento: str, observacao: str = "", comprovante: str = "") -> str:
    """
    Finalizar o pedido usando os itens que estão no carrinho.
    Use quando o cliente confirmar que quer fechar a compra.
    
    Args:
    - cliente: Nome do cliente
    - telefone: Telefone (com DDD)
    - endereco: Endereço de entrega (rua, número, bairro)
    - forma_pagamento: PIX, DINHEIRO, CARTAO
    - observacao: Observações do pedido (opcional)
    - comprovante: URL do comprovante (opcional)
    """
    import json as json_lib
    
    # 1. Obter itens do Redis
    items = get_cart_items(telefone)
    if not items:
        return "❌ O carrinho está vazio! Adicione itens antes de finalizar."
    
    # 2. Calcular total e formatar itens para API
    total = 0.0
    itens_formatados = []
    
    for item in items:
        preco = item.get("preco", 0.0)
        quantidade = item.get("quantidade", 1.0)
        unidades = item.get("unidades", 0)
        obs_item = item.get("observacao", "")
        total += preco * quantidade
        
        nome_produto = item.get("produto", item.get("nome_produto", "Produto"))
        
        # Se tem unidades, é produto pesado (tomate, cebola, etc)
        if unidades > 0:
            qtd_api = unidades
            valor_estimado = round(preco * quantidade, 2)
            obs_peso = f"Peso estimado: {quantidade:.3f}kg (~R${valor_estimado:.2f}). PESAR para confirmar valor."
            if obs_item:
                obs_item = f"{obs_item}. {obs_peso}"
            else:
                obs_item = obs_peso
        else:
            # Produto unitário normal
            if quantidade < 1 or quantidade != int(quantidade):
                qtd_api = 1
            else:
                qtd_api = int(quantidade)
        
        itens_formatados.append({
            "nome_produto": nome_produto,
            "quantidade": qtd_api,
            "preco_unitario": round(preco, 2),
            "observacao": obs_item
        })
        
    # 3. Montar payload do pedido (campos corretos para API)
    payload = {
        "nome_cliente": cliente,
        "telefone": telefone,
        "endereco": endereco or "A combinar",
        "forma": forma_pagamento,
        "observacao": observacao or "",
        "itens": itens_formatados
    }
    
    json_body = json_lib.dumps(payload, ensure_ascii=False)
    
    # 4. Enviar via HTTP
    result = pedidos(json_body)
    
    # 5. Se sucesso, limpar carrinho e marcar status
    if "sucesso" in result.lower() or "✅" in result:
        clear_cart(telefone)
        mark_order_sent(telefone)
        
    return result

@tool
def alterar_tool(telefone: str, json_body: str) -> str:
    """Atualizar o pedido no painel (para pedidos JÁ enviados)."""
    return alterar(telefone, json_body)

@tool
def search_history_tool(telefone: str, keyword: str = None) -> str:
    """Busca mensagens anteriores do cliente com horários."""
    return search_message_history(telefone, keyword)

@tool
def time_tool() -> str:
    """Retorna a data e hora atual."""
    return get_current_time()

@tool("ean")
def ean_tool_alias(query: str) -> str:
    """Buscar EAN/infos do produto na base de conhecimento."""
    q = (query or "").strip()
    if q.startswith("{") and q.endswith("}"): q = ""
    return ean_lookup(q)

@tool("estoque")
def estoque_preco_alias(ean: str) -> str:
    """Consulta preço e disponibilidade pelo EAN (apenas dígitos)."""
    return estoque_preco(ean)

@tool("busca_lote")
def busca_lote_tool(produtos: str) -> str:
    """
    Busca MÚLTIPLOS produtos de uma vez em paralelo. Use quando o cliente pedir vários itens.
    
    Args:
        produtos: Lista de produtos separados por vírgula.
                  Ex: "suco de acerola, suco de caju, arroz, feijão"
    
    Returns:
        Lista formatada com todos os produtos encontrados e seus preços.
    """
    # Converter string em lista
    lista_produtos = [p.strip() for p in produtos.split(",") if p.strip()]
    if not lista_produtos:
        return "❌ Informe os produtos separados por vírgula."
    return busca_lote_produtos(lista_produtos)

# Ferramentas ativas
ACTIVE_TOOLS = [
    ean_tool_alias,
    estoque_preco_alias,
    busca_lote_tool,  # Nova tool para busca em lote
    estoque_tool,
    time_tool,
    search_history_tool,
    add_item_tool,
    view_cart_tool,
    remove_item_tool,
    finalizar_pedido_tool,
    alterar_tool,
]

# ============================================
# Funções do Grafo
# ============================================

def load_system_prompt() -> str:
    base_dir = Path(__file__).resolve().parent
    prompt_path = str((base_dir / "prompts" / "agent_system_optimized.md"))
    try:
        text = Path(prompt_path).read_text(encoding="utf-8")
        text = text.replace("{base_url}", settings.supermercado_base_url)
        text = text.replace("{ean_base}", settings.estoque_ean_base_url)
        return text
    except Exception as e:
        logger.error(f"Falha ao carregar prompt: {e}")
        raise

def _build_llm():
    model = getattr(settings, "llm_model", "gemini-2.5-flash")
    temp = float(getattr(settings, "llm_temperature", 0.0))
    provider = getattr(settings, "llm_provider", "google")
    
    if provider == "google":
        logger.info(f"🚀 Usando Google Gemini: {model}")
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=settings.google_api_key,
            temperature=temp,
            convert_system_message_to_human=True,  # Necessário para Gemini processar system prompts
        )
    else:
        logger.info(f"🚀 Usando OpenAI: {model}")
        return ChatOpenAI(
            model=model,
            openai_api_key=settings.openai_api_key,
            temperature=temp
        )

def create_agent_with_history():
    system_prompt = load_system_prompt()
    llm = _build_llm()
    memory = MemorySaver()
    agent = create_react_agent(llm, ACTIVE_TOOLS, prompt=system_prompt, checkpointer=memory)
    return agent

_agent_graph = None
def get_agent_graph():
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = create_agent_with_history()
    return _agent_graph

# ============================================
# Função Principal
# ============================================

def run_agent_langgraph(telefone: str, mensagem: str) -> Dict[str, Any]:
    """
    Executa o agente. Suporta texto e imagem (via tag [MEDIA_URL: ...]).
    """
    print(f"[AGENT] Telefone: {telefone} | Msg bruta: {mensagem[:50]}...")
    
    # 1. Extrair URL de imagem se houver (Formato: [MEDIA_URL: https://...])
    image_url = None
    clean_message = mensagem
    
    # Regex para encontrar a tag de mídia injetada pelo server.py
    media_match = re.search(r"\[MEDIA_URL:\s*(.*?)\]", mensagem)
    if media_match:
        image_url = media_match.group(1)
        # Remove a tag da mensagem de texto para não confundir o histórico visual
        # Mas mantemos o texto descritivo original
        clean_message = mensagem.replace(media_match.group(0), "").strip()
        if not clean_message:
            clean_message = "Analise esta imagem/comprovante enviada."
        logger.info(f"📸 Mídia detectada para visão: {image_url}")

    # 2. Salvar histórico (User)
    history_handler = None
    try:
        history_handler = get_session_history(telefone)
        history_handler.add_user_message(mensagem)
    except Exception as e:
        logger.error(f"Erro DB User: {e}")

    try:
        agent = get_agent_graph()
        
        # 3. Construir mensagem (Texto Simples ou Multimodal)
        # IMPORTANTE: Injetar telefone no contexto para que o LLM saiba qual usar nas tools
        telefone_context = f"[TELEFONE_CLIENTE: {telefone}]\n\n"
        
        if image_url:
            # Formato multimodal para GPT-4o / GPT-4o-mini
            message_content = [
                {"type": "text", "text": telefone_context + clean_message},
                {
                    "type": "image_url",
                    "image_url": {"url": image_url}
                }
            ]
            initial_message = HumanMessage(content=message_content)
        else:
            initial_message = HumanMessage(content=telefone_context + clean_message)

        initial_state = {"messages": [initial_message]}
        config = {"configurable": {"thread_id": telefone}, "recursion_limit": 100}
        
        logger.info("Executando agente...")
        
        # Contador de tokens (nota: get_openai_callback pode não funcionar 100% com Gemini)
        with get_openai_callback() as cb:
            result = agent.invoke(initial_state, config)
            
            # Cálculo de custo baseado no provider
            provider = getattr(settings, "llm_provider", "google")
            if provider == "google":
                # Gemini 2.5 Flash-Lite pricing (atualizado 12/2024)
                # Input: $0.10 per 1M tokens | Output: $0.40 per 1M tokens
                input_cost = (cb.prompt_tokens / 1_000_000) * 0.10
                output_cost = (cb.completion_tokens / 1_000_000) * 0.40
            else:
                # OpenAI gpt-4o-mini pricing
                # Input: $0.15 per 1M tokens | Output: $0.60 per 1M tokens
                input_cost = (cb.prompt_tokens / 1_000_000) * 0.15
                output_cost = (cb.completion_tokens / 1_000_000) * 0.60
            
            total_cost = input_cost + output_cost
            
            # Log de tokens
            logger.info(f"📊 TOKENS - Prompt: {cb.prompt_tokens} | Completion: {cb.completion_tokens} | Total: {cb.total_tokens}")
            logger.info(f"💰 CUSTO: ${total_cost:.6f} USD (Input: ${input_cost:.6f} | Output: ${output_cost:.6f})")
        
        # 4. Extrair resposta (com fallback para Gemini empty responses)
        output = ""
        if isinstance(result, dict) and "messages" in result:
            messages = result["messages"]
            logger.debug(f"📨 Total de mensagens no resultado: {len(messages) if messages else 0}")
            if messages:
                # Log das últimas mensagens para debug
                for i, msg in enumerate(messages[-5:]):
                    msg_type = type(msg).__name__
                    has_tool_calls = hasattr(msg, 'tool_calls') and msg.tool_calls
                    content_preview = str(msg.content)[:100] if msg.content else "(vazio)"
                    logger.debug(f"📝 Msg[{i}] type={msg_type} tool_calls={has_tool_calls} content={content_preview}")
                
                # Tentar pegar a última mensagem AI que tenha conteúdo real (não tool call)
                for msg in reversed(messages):
                    # Verificar se é AIMessage
                    if not isinstance(msg, AIMessage):
                        continue
                    
                    # Ignorar mensagens que são tool calls (não tem resposta textual)
                    if hasattr(msg, 'tool_calls') and msg.tool_calls:
                        continue
                    
                    # Extrair conteúdo
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    
                    # Ignorar mensagens vazias
                    if not content or not content.strip():
                        continue
                    
                    # Ignorar mensagens que parecem ser dados estruturados
                    if content.strip().startswith(("[", "{")):
                        continue
                    
                    output = content
                    break
        
        # Fallback se ainda estiver vazio
        if not output or not output.strip():
            # Logar o que foi rejeitado para debug
            if isinstance(result, dict) and "messages" in result:
                last_ai = None
                for msg in reversed(result["messages"]):
                    if isinstance(msg, AIMessage):
                        last_ai = msg
                        break
                if last_ai:
                    logger.warning(f"⚠️ Última AIMessage rejeitada: content='{str(last_ai.content)[:200]}' tool_calls={getattr(last_ai, 'tool_calls', None)}")
            
            # FALLBACK INTELIGENTE: Analisa as mensagens de tool para gerar resposta útil
            tool_results = []
            produtos_encontrados = []
            precos_encontrados: List[str] = []
            nao_encontrados_list: List[str] = []
            
            for msg in result.get("messages", []):
                if hasattr(msg, 'content') and isinstance(msg.content, str):
                    content = msg.content
                    # Detectar resposta de estoque vazio
                    if "0 item" in content or "disponíveis após filtragem" in content or "[]" in content:
                        tool_results.append("sem_estoque")
                    # Detectar busca de EAN e extrair nomes dos produtos
                    elif "EANS_ENCONTRADOS" in content:
                        tool_results.append("ean_encontrado")
                        # Extrair nomes dos produtos (formato: "1) EAN - NOME PRODUTO")
                        matches = re.findall(r'\d+\) \d+ - ([A-Z][^\n;]+)', content)
                        if matches:
                            produtos_encontrados.extend(matches[:3])  # Pegar até 3 produtos
                    # Detectar produto não encontrado
                    elif "Nenhum produto encontrado" in content or "não encontrado" in content.lower():
                        tool_results.append("nao_encontrado")
                    # Detectar formato da busca em lote
                    elif "PRODUTOS_ENCONTRADOS:" in content:
                        tool_results.append("busca_lote_ok")
                        # Capturar linhas com "• Nome - R$ XX,YY"
                        linhas = content.split("\n")
                        for ln in linhas:
                            ln_str = ln.strip()
                            if ln_str.startswith("• ") and ("R$" in ln_str or "R$" in ln_str.replace(" ", "")):
                                precos_encontrados.append(ln_str[2:].strip())
                    elif "NÃO_ENCONTRADOS:" in content or "NAO_ENCONTRADOS:" in content:
                        # Extrair lista após os dois pontos
                        try:
                            parte = content.split(":", 1)[1]
                            nomes = [x.strip() for x in parte.split(",") if x.strip()]
                            nao_encontrados_list.extend(nomes)
                        except Exception:
                            pass
                    # Detectar SUCESSO na busca em lote (Fallback para quando o LLM falha em responder)
                    elif "✅ [BUSCA LOTE] Sucesso" in content:
                        # Extrair produto e preço: "Sucesso com 'NOME' (R$ XX.XX)"
                        match = re.search(r"Sucesso com '([^']+)' \((R\$ [0-9.,]+)\)", content)
                        if match:
                            prod, preco = match.groups()
                            tool_results.append(f"sucesso:{prod}:{preco}")
            
            # Gerar resposta baseada nos resultados das tools
            if any(r.startswith("sucesso:") for r in tool_results) or ("busca_lote_ok" in tool_results and precos_encontrados):
                # Extrair itens encontrados
                itens_ok = []
                if precos_encontrados:
                    itens_ok.extend(precos_encontrados)
                for r in tool_results:
                    if r.startswith("sucesso:"):
                        _, prod, preco = r.split(":", 2)
                        itens_ok.append(f"{prod} - {preco}")

                # Montar resposta amigável
                if itens_ok:
                    linhas = ["Aqui estão os valores:"] + [f"* {ln}" for ln in itens_ok]
                    if nao_encontrados_list:
                        linhas.append(f"\nNão encontrei: {', '.join(nao_encontrados_list)}.")
                    linhas.append("Quer que eu adicione ao carrinho?")
                    output = "\n".join(linhas)
                    logger.info(f"🔄 Fallback inteligente: gerando resposta de preços - {output}")
                else:
                    output = "Não consegui obter os preços agora. Pode repetir?"

            elif "sem_estoque" in tool_results:
                if produtos_encontrados:
                    # Oferecer alternativas da lista de produtos encontrados
                    alternativas = ", ".join(produtos_encontrados[:2])
                    output = f"Não temos esse produto disponível. Temos: {alternativas}. Quer algum desses?"
                    logger.info(f"🔄 Fallback inteligente: oferecendo alternativas - {alternativas}")
                else:
                    output = "Não temos esse produto disponível no momento. Quer outro?"
                    logger.info("🔄 Fallback inteligente: produto sem estoque, sem alternativas")
            elif "nao_encontrado" in tool_results:
                output = "Não achei esse produto. Pode descrever de outra forma?"
                logger.info("🔄 Fallback inteligente: produto não encontrado")
            else:
                output = "Desculpe, não consegui processar sua solicitação. Pode repetir?"
                logger.warning("⚠️ Resposta vazia do LLM, usando fallback genérico")
        
        logger.info("✅ Agente executado")
        logger.info(f"💬 RESPOSTA: {output[:200]}{'...' if len(output) > 200 else ''}")
        
        # 5. Salvar histórico (IA)
        if history_handler:
            try:
                history_handler.add_ai_message(output)
            except Exception as e:
                logger.error(f"Erro DB AI: {e}")

        return {"output": output, "error": None}
        
    except Exception as e:
        logger.error(f"Falha agente: {e}", exc_info=True)
        return {"output": "Tive um problema técnico, tente novamente.", "error": str(e)}

def get_session_history(session_id: str) -> LimitedPostgresChatMessageHistory:
    return LimitedPostgresChatMessageHistory(
        connection_string=settings.postgres_connection_string,
        session_id=session_id,
        table_name=settings.postgres_table_name,
        max_messages=settings.postgres_message_limit
    )

run_agent = run_agent_langgraph
