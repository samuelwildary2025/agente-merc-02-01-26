"""
Servidor FastAPI para receber mensagens do WhatsApp e processar com o agente
Suporta: Texto, Áudio (Transcrição), Imagem (Visão) e PDF (Extração de Texto + Link)
Versão: 1.6.0 (Correção de LID e Buffer Personalizado)
"""
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import requests
from datetime import datetime
import time
import random
import threading
import re
import io

# Tenta importar pypdf para leitura de comprovantes
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from config.settings import settings
from config.logger import setup_logger
from agent_langgraph_simple import run_agent_langgraph as run_agent, get_session_history
from tools.whatsapp_api import whatsapp
from tools.redis_tools import (
    push_message_to_buffer,
    get_buffer_length,
    pop_all_messages,
    set_agent_cooldown,
    is_agent_in_cooldown,
    get_order_session,
    start_order_session,
    refresh_session_ttl,
    get_order_context,
)

logger = setup_logger(__name__)

app = FastAPI(title="Agente de Supermercado", version="1.6.0") # FORCE UPDATE CHECK

# --- Models ---
class WhatsAppMessage(BaseModel):
    telefone: str
    mensagem: str
    message_id: Optional[str] = None
    timestamp: Optional[str] = None
    message_type: Optional[str] = "text"

class AgentResponse(BaseModel):
    success: bool
    response: str
    telefone: str
    timestamp: str
    error: Optional[str] = None

# --- Helpers ---

def get_api_base_url() -> str:
    """Prioriza UAZ_API_URL > WHATSAPP_API_URL."""
    return (settings.uaz_api_url or settings.whatsapp_api_url or "").strip().rstrip("/")

def get_media_url_uaz(message_id: str) -> Optional[str]:
    """Solicita link público da mídia (Imagem/PDF)."""
    if not message_id: return None
    base = get_api_base_url()
    if not base: return None

    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/message/download"
    except:
        url = f"{base.split('/message')[0]}/message/download"

    headers = {"Content-Type": "application/json", "token": (settings.whatsapp_token or "").strip()}
    # return_link=True devolve url pública
    payload = {"id": message_id, "return_link": True, "return_base64": False}
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            link = data.get("fileURL") or data.get("url")
            if link: return link
    except Exception as e:
        logger.error(f"Erro ao obter link mídia: {e}")
    return None

def process_pdf_uaz(message_id: str) -> Optional[str]:
    """Baixa o PDF e extrai o texto (para leitura do valor)."""
    if not PdfReader:
        logger.error("❌ Biblioteca pypdf não instalada. Adicione ao requirements.txt")
        return "[Erro: sistema não suporta leitura de PDF]"

    url = get_media_url_uaz(message_id)
    if not url: return None
    
    logger.info(f"📄 Processando PDF: {url}")
    try:
        # Baixar o arquivo
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        
        # Ler PDF em memória
        f = io.BytesIO(response.content)
        reader = PdfReader(f)
        
        text_content = []
        for page in reader.pages:
            text_content.append(page.extract_text())
            
        full_text = "\n".join(text_content)
        full_text = re.sub(r'\s+', ' ', full_text).strip()
        
        logger.info(f"✅ PDF lido com sucesso ({len(full_text)} chars)")
        return full_text
        
    except Exception as e:
        logger.error(f"Erro ao ler PDF: {e}")
        return None

def transcribe_audio_uaz(message_id: str) -> Optional[str]:
    """
    Transcreve áudio usando Google Gemini.
    Baixa o áudio em Base64 via API, salva em disco e envia para Gemini.
    """
    if not message_id: return None
    
    logger.info(f"🎤 DEBUG TRANSCRIBE: Iniciando para ID {message_id}")

    # 1. Obter Base64 do áudio via API
    media_data = whatsapp.get_media_base64(message_id)
    
    logger.info(f"🎤 DEBUG TRANSCRIBE: Retorno API = {type(media_data)}")
    
    if not media_data or not media_data.get("base64"):
        logger.error(f"❌ [NOVO CÓDIGO 1.6.0] Falha ao obter Base64: {message_id}")
        return None
    
    try:
        logger.info(f"🎧 Transcrevendo áudio com Gemini: {message_id}")
        
        # 2. Decodificar Base64
        import base64
        audio_data = base64.b64decode(media_data["base64"])
        mime_type_clean = media_data.get("mimetype", "audio/ogg").split(";")[0].strip()
        
        logger.info(f"📤 Uploading to Gemini with mime_type: {mime_type_clean}")
        
        # 3. Usar Google Gemini para transcrever
        from google import genai
        
        client = genai.Client(api_key=settings.google_api_key)
        
        # Upload do áudio para o Gemini
        import tempfile
        import os as os_module
        
        # Determinar extensão baseada no content-type
        ext_map = {
            'audio/ogg': '.ogg',
            'audio/mpeg': '.mp3',
            'audio/mp4': '.m4a',
            'audio/wav': '.wav',
            'audio/webm': '.webm',
        }
        ext = ext_map.get(mime_type_clean, '.ogg')
        
        # Salvar temporariamente
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name
        
        try:
            # Upload do arquivo para Gemini com MIME TYPE explícito
            audio_file = client.files.upload(
                file=tmp_path,
                config={'mime_type': mime_type_clean}
            )
            
            # Transcrever usando Gemini
            response = client.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=[
                    "Transcreva este áudio para texto em português brasileiro. Retorne APENAS o texto transcrito, sem explicações.",
                    audio_file
                ]
            )
            
            transcription = response.text.strip() if response.text else None
            
            if transcription:
                logger.info(f"✅ Áudio transcrito com Gemini: {transcription[:50]}...")
                return transcription
            else:
                logger.warning("⚠️ Gemini retornou transcrição vazia")
                return None
                
        finally:
            # Limpar arquivo temporário
            try:
                os_module.unlink(tmp_path)
            except:
                pass
            
    except Exception as e:
        logger.error(f"Erro transcrição Gemini: {e}")
        return None

def analyze_image_uaz(message_id: Optional[str], url: Optional[str]) -> Optional[str]:
    if not settings.google_api_key:
        return None

    file_path = None
    try:
        from google import genai
        import tempfile
        import os as os_module
        import base64

        mime_type_clean = None
        image_bytes = None

        if message_id:
            media_data = whatsapp.get_media_base64(message_id)
            if media_data and media_data.get("base64"):
                image_bytes = base64.b64decode(media_data["base64"])
                mime_type_clean = (media_data.get("mimetype") or "image/jpeg").split(";")[0].strip()

        if image_bytes is None and url:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            image_bytes = resp.content
            mime_type_clean = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()

        if not image_bytes:
            return None

        ext_map = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        ext = ext_map.get((mime_type_clean or "").lower(), ".jpg")

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(image_bytes)
            file_path = tmp.name

        client = genai.Client(api_key=settings.google_api_key)
        image_file = client.files.upload(file=file_path, config={"mime_type": mime_type_clean or "image/jpeg"})

        prompt = (
            "Analise cuidadosamente esta imagem e identifique o produto (se for um produto). "
            "Retorne um texto curto em português com: nome do produto, marca, versão/sabor/variante, "
            "tamanho/peso/volume e qualquer detalhe útil visível. "
            "Se não for um produto (ex.: foto borrada, pessoa, conversa), diga apenas: 'Imagem não identificada'. "
            "Não invente detalhes; só use o que estiver visível."
        )

        model_candidates = [settings.llm_model or "gemini-2.0-flash-lite", "gemini-2.0-flash"]
        last_err = None
        for model in model_candidates:
            try:
                response = client.models.generate_content(model=model, contents=[prompt, image_file])
                txt = (response.text or "").strip()
                if txt:
                    return txt[:800]
            except Exception as e:
                last_err = e

        if last_err:
            logger.error(f"Erro visão Gemini: {last_err}")
        return None

    except Exception as e:
        logger.error(f"Erro ao analisar imagem: {e}")
        return None
    finally:
        if file_path:
            try:
                import os as os_module
                os_module.unlink(file_path)
            except Exception:
                pass

def _extract_incoming(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza e processa (Texto, Áudio, Imagem, Documento/PDF).
    Suporta payload da nova API: { "event": "message", "data": { ... } }
    """
    
    # DEBUG CRÍTICO
    try:
        keys = list(payload.keys())
        logger.info(f"🔍 DEBUG EXTRACT START: Keys={keys}")
    except: pass
    
    # Se o payload vier envelopado no formato novo
    if "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"]
        try:
            logger.info(f"🔍 DEBUG EXTRACT UNWRAPPED: Keys={list(payload.keys())} | From={payload.get('from')} | Body={payload.get('body')}")
        except: pass

    # ADAPTAÇÃO: Se o payload tiver uma chave 'message' (payload aninhado extra)
    # Ex: { "event": "message", "data": { "instanceId": "...", "message": { ... } } }
    if "message" in payload and isinstance(payload["message"], dict):
        # PROMOÇÃO DIRETA: Se existe 'message', usamos ela como payload principal
        payload = payload["message"]
        try:
             logger.info(f"🔍 DEBUG EXTRACT PROMOTED MESSAGE: Keys={list(payload.keys())}")
        except: pass

    def _clean_number(jid: Any) -> Optional[str]:
        """Extrai apenas o número de telefone de um JID válido."""
        if not jid or not isinstance(jid, str): return None
        
        # Se tiver @lid, é ID de dispositivo (IGNORAR)
        if "@lid" in jid: return None
        
        # Se tiver @g.us, é grupo (IGNORAR)
        if "@g.us" in jid: return None
        
        # Pega a parte antes do @
        if "@" in jid:
            jid = jid.split("@")[0]
            
        # Remove tudo que não for dígito
        num = re.sub(r"\D", "", jid)
        
        # Validação básica (evita IDs estranhos)
        if len(num) > 15 or len(num) < 10:
            return None
            
        return num

    chat = payload.get("chat") or {}
    message_any = payload.get("message") or {}
    
    if isinstance(payload.get("messages"), list):
        try:
            m0 = payload["messages"][0]
            message_any = m0
            chat = {"wa_id": m0.get("sender") or m0.get("chatid")}
        except: pass

    # --- LÓGICA DE TELEFONE BLINDADA ---
    telefone = None
    
    # Ordem de prioridade para encontrar o número real
    candidates = []
    
    # 1. Sender/ChatID (Geralmente o mais preciso: 5585...@s.whatsapp.net)
    if isinstance(message_any, dict):
        candidates.append(message_any.get("sender"))
        candidates.append(message_any.get("chatid"))
    
    # 2. Objeto Chat
    candidates.append(chat.get("id"))
    candidates.append(chat.get("wa_id"))
    candidates.append(chat.get("phone"))
    
    # 3. Payload Raiz (Menos confiável)
    candidates.append(payload.get("from"))
    candidates.append(payload.get("sender"))

    # 4. Estrutura Baileys/Key (CRUCIAL PARA MÍDIA/ÁUDIO)
    # Procura dentro de 'key' se existir no payload
    if isinstance(payload.get("key"), dict):
        candidates.append(payload["key"].get("remoteJid"))
        candidates.append(payload["key"].get("participant")) # Para grupos (embora a gente ignore grupos)

    # Varre a lista e pega o primeiro válido (sem LID)
    for cand in candidates:
        cleaned = _clean_number(cand)
        if cleaned:
            telefone = cleaned
            break
            
    # Fallback de emergência (avisa no log)
    if not telefone and payload.get("from"):
        raw = str(payload.get("from"))
        if "@lid" not in raw:
            telefone = re.sub(r"\D", "", raw)
            logger.warning(f"⚠️ Usando fallback de telefone: {telefone}")

    # --- Extração de Conteúdo (Adaptado para nova API) ---
    # Na nova API, 'body' é o texto e 'mediaUrl' indica mídia
    mensagem_texto = payload.get("body") or payload.get("text")
    message_id = payload.get("id") or payload.get("messageid")
    from_me = bool(payload.get("fromMe") or False)
    
    # Determinar tipo
    msg_type = payload.get("type") or "chat"
    media_url = payload.get("mediaUrl")
    
    message_type = "text"
    if msg_type == "ptt" or msg_type == "audio":
        message_type = "audio"
    elif msg_type == "image" or (media_url and "jpg" in str(media_url)):
        message_type = "image"
    elif msg_type == "document" or (media_url and "pdf" in str(media_url)):
        message_type = "document"

    # Se for mídia, tenta pegar a URL direto do payload se vier
    if message_type in ["image", "audio", "document"] and media_url:
        # Na nova API, a URL já vem no payload, não precisa baixar via ID às vezes
        # Mas mantemos a lógica de ID se precisar
        pass

    # Lógica legada para garantir compatibilidade com estruturas antigas
    if not mensagem_texto:
        message_any = payload  # No novo formato, payload já é a mensagem
        
        raw_type = str(message_any.get("messageType") or "").lower()
        media_type = str(message_any.get("mediaType") or "").lower()
        base_type = str(message_any.get("type") or "").lower()
        mimetype = str(message_any.get("mimetype") or "").lower()
        
        if "audio" in raw_type or "ptt" in media_type or "audio" in base_type:
            message_type = "audio"
        elif "image" in raw_type or "image" in media_type or "image" in base_type:
            message_type = "image"
        elif "document" in raw_type or "document" in base_type or "application/pdf" in mimetype:
            message_type = "document"

        content = message_any.get("content")
        if isinstance(content, str) and not mensagem_texto:
            mensagem_texto = content
        elif isinstance(content, dict):
            mensagem_texto = content.get("text") or content.get("caption") or mensagem_texto
        
        if not mensagem_texto:
            txt = message_any.get("text")
            if isinstance(txt, dict):
                mensagem_texto = txt.get("body")
            else:
                mensagem_texto = txt or message_any.get("body")

    if from_me:
        # Se for mensagem enviada por MIM, tenta achar o destinatário
        candidates_me = [chat.get("wa_id"), chat.get("phone"), payload.get("sender"), payload.get("to")]
        telefone = next((re.sub(r"\D", "", c) for c in candidates_me if c and "@lid" not in str(c)), telefone)

    # --- Lógica de Mídia ---
    if message_type == "audio" and not mensagem_texto:
        if message_id:
            # Usa a nova função que suporta Base64
            trans = transcribe_audio_uaz(message_id)
            mensagem_texto = f"[Áudio]: {trans}" if trans else "[Áudio inaudível]"
        else:
            mensagem_texto = "[Áudio sem ID]"
            
    elif message_type == "image":
        caption = mensagem_texto or ""
        url = media_url or get_media_url_uaz(message_id)
        analysis = analyze_image_uaz(message_id, url)
        if analysis:
            base = caption.strip()
            mensagem_texto = f"{base}\n[Análise da imagem]: {analysis}".strip() if base else f"[Análise da imagem]: {analysis}"
        else:
            mensagem_texto = caption.strip() if caption else "[Imagem recebida]"
        if url:
            mensagem_texto = f"{mensagem_texto} [MEDIA_URL: {url}]".strip()

    elif message_type == "document":
        url = media_url or get_media_url_uaz(message_id)
        pdf_text = ""
        if message_id:
            extracted = process_pdf_uaz(message_id)
            if extracted:
                pdf_text = f"\n[Conteúdo PDF]: {extracted[:1200]}..."
        
        if url:
            mensagem_texto = f"Comprovante/PDF Recebido. {pdf_text} [MEDIA_URL: {url}]"
        else:
            mensagem_texto = f"[PDF sem link] {pdf_text}"

    return {
        "telefone": telefone,
        "mensagem_texto": mensagem_texto,
        "message_type": message_type,
        "message_id": message_id,
        "from_me": from_me,
    }

def send_whatsapp_message(telefone: str, mensagem: str) -> bool:
    """Envia mensagem usando a nova classe WhatsAppAPI."""
    
    # Configuração de split de mensagens
    # Max 500 chars por mensagem para não enviar textões
    max_len = 500
    msgs = []
    
    if len(mensagem) > max_len:
        # Divide por parágrafos duplos primeiro
        paragrafos = mensagem.split('\n\n')
        curr = ""
        
        for p in paragrafos:
            # Se o parágrafo sozinho é muito grande, divide por quebras simples
            if len(p) > max_len:
                if curr:
                    msgs.append(curr.strip())
                    curr = ""
                # Divide parágrafo grande por linhas
                linhas = p.split('\n')
                for linha in linhas:
                    if len(curr) + len(linha) + 1 <= max_len:
                        curr += linha + "\n"
                    else:
                        if curr: msgs.append(curr.strip())
                        curr = linha + "\n"
            elif len(curr) + len(p) + 2 <= max_len:
                curr += p + "\n\n"
            else:
                if curr: msgs.append(curr.strip())
                curr = p + "\n\n"
        
        if curr: msgs.append(curr.strip())
    else:
        msgs = [mensagem]
    
    try:
        for i, msg in enumerate(msgs):
            # Usa a nova API
            whatsapp.send_text(telefone, msg)
            
            # Delay entre mensagens para parecer mais natural (exceto última)
            if i < len(msgs) - 1:
                time.sleep(random.uniform(0.8, 1.5))
                
        return True
    except Exception as e:
        logger.error(f"Erro envio: {e}")
        return False

# --- Presença & Buffer ---
presence_sessions = {}
buffer_sessions = {}

def send_presence(num, type_):
    """Envia status: 'composing' (digitando) ou 'paused'."""
    # Mapeamento para nova API
    # Nova API aceita: composing, recording, available, unavailable
    # paused -> available (ou unavailable, mas available para parar de digitar)
    status_map = {
        "composing": "composing",
        "paused": "available" 
    }
    whatsapp.send_presence(num, status_map.get(type_, "available"))

def process_async(tel, msg, mid=None):
    """
    Processa mensagem do Buffer.
    Fluxo Humano:
    1. Espera (simula leitura).
    2. Marca como LIDO (Azul).
    3. Digita (composing).
    4. Processa (IA).
    5. Para de digitar (paused).
    6. Envia.
    """
    try:
        num = re.sub(r"\D", "", tel)
        
        # 1. Simular "Lendo" (Delay Humano)
        tempo_leitura = random.uniform(2.0, 4.0) 
        time.sleep(tempo_leitura)

        # 2. Marcar como LIDO (Azul) AGORA
        # Usa o telefone (chat_id) em vez do message_id, conforme documentação da API
        logger.info(f"👀 Marcando chat {tel} como lido...")
        whatsapp.mark_as_read(tel)
        time.sleep(0.8) # Delay tático: Garante que o usuário veja o AZUL antes de ver o "Digitando..."

        # 3. Começar a "Digitar"
        send_presence(num, "composing")
        
        # 4. Processamento IA
        res = run_agent(tel, msg)
        txt = res.get("output", "Erro ao processar.")
        
        # 5. Parar "Digitar"
        send_presence(num, "paused")
        time.sleep(0.5) # Pausa dramática antes de chegar

        # 6. Enviar Mensagem
        send_whatsapp_message(tel, txt)

    except Exception as e:
        logger.error(f"Erro async: {e}")
    finally:
        # Garante limpeza
        send_presence(tel, "paused")
        presence_sessions.pop(re.sub(r"\D", "", tel), None)

def buffer_loop(tel):
    """
    Loop do Buffer (3 ciclos de 5s = 15 segundos)
    Total espera máxima: ~15 segundos
    
    IMPORTANTE: Após processar, verifica se chegaram novas mensagens durante
    a execução do agente e as processa também (evita mensagens "perdidas").
    """
    try:
        n = re.sub(r"\D","",tel)
        
        while True:  # Loop principal para pegar mensagens que chegam durante processamento
            prev = get_buffer_length(n)
            
            # Se não tem mensagens, sair
            if prev == 0:
                break
                
            stall = 0
            
            # Esperar por mais mensagens (3 ciclos de 3.5s)
            while stall < 3:
                time.sleep(5)  # 3 ciclos de 5s = 15 segundos total
                curr = get_buffer_length(n)
                if curr > prev: prev, stall = curr, 0
                else: stall += 1
            
            # Consumir e processar mensagens
            # AGORA RETORNA TEXTOS E LAST_MID
            msgs, last_mid = pop_all_messages(n)
            
            # Usa ' | ' como separador para o agente entender que são itens/pedidos separados
            final = " | ".join([m for m in msgs if m.strip()])
            
            if not final:
                break
                
            # Obter contexto de sessão
            order_ctx = get_order_context(n)
            if order_ctx:
                final = f"{order_ctx}\n\n{final}"
            
            # Processar (enquanto isso, novas mensagens podem chegar)
            # Passa o last_mid para marcar como lido
            process_async(n, final, mid=last_mid)
            
            # Após processar, o loop vai verificar se tem novas mensagens
            # Se tiver, processa novamente. Se não, sai.
            
    except Exception as e:
        logger.error(f"Erro no buffer_loop: {e}")
    finally: 
        buffer_sessions.pop(re.sub(r"\D","",tel), None)

# --- Endpoints ---
@app.get("/")
async def root(): return {"status":"online", "ver":"1.6.0"}

@app.get("/health")
async def health(): return {"status":"healthy", "ts":datetime.now().isoformat()}

@app.post("/")
@app.post("/webhook/whatsapp")
async def webhook(req: Request, tasks: BackgroundTasks):
    try:
        pl = await req.json()
        data = _extract_incoming(pl)
        tel, txt, from_me = data["telefone"], data["mensagem_texto"], data["from_me"]
        msg_type = data.get("message_type", "text")

        # Se for áudio/imagem/doc, o texto pode vir vazio (será preenchido depois na transcrição ou OCR)
        # Então só bloqueamos se for TEXTO e estiver vazio
        if not tel or (not txt and msg_type == "text"): 
            logger.warning(f"⚠️ IGNORED | Tel: {tel} | Txt: {txt} | Type: {msg_type} | PayloadKeys: {list(pl.keys())}")
            # DUMP DE DEBUG
            try:
                import json
                logger.warning(f"🐛 PAYLOAD DUMP: {json.dumps(pl, default=str)[:2000]}")
            except: pass
            
            return JSONResponse(content={"status":"ignored"})
        
        logger.info(f"In: {tel} | {msg_type} | {txt[:50] if txt else '[Mídia]'}")

        if from_me:
            # Detectar Human Takeover: Se o número do agente enviou mensagem
            # Ativar cooldown para pausar a IA
            agent_number = (settings.whatsapp_agent_number or "").strip()
            if agent_number:
                # Limpar para comparação
                agent_clean = re.sub(r"\D", "", agent_number)
                # Se a mensagem foi enviada PARA um cliente (não é conversa interna)
                if tel and tel != agent_clean:
                    # Ativar cooldown - IA pausa por X minutos
                    ttl = settings.human_takeover_ttl  # Default: 900s (15min)
                    set_agent_cooldown(tel, ttl)
                    logger.info(f"🙋 Human Takeover ativado para {tel} - IA pausa por {ttl//60}min")
            
            try: get_session_history(tel).add_ai_message(txt)
            except: pass
            return JSONResponse(content={"status":"ignored_self"})

        num = re.sub(r"\D","",tel)
        
        # NOTA: 'send_presence' imediato removido para evitar comportamento robótico.
        # O cliente verá 'digitando' apenas após o buffer, no process_async.

        active, _ = is_agent_in_cooldown(num)
        if active:
            push_message_to_buffer(num, txt)
            return JSONResponse(content={"status":"cooldown"})

        try:
            if not presence_sessions.get(num):
                presence_sessions[num] = True
        except: pass

        if push_message_to_buffer(num, txt):
            if not buffer_sessions.get(num):
                buffer_sessions[num] = True
                threading.Thread(target=buffer_loop, args=(num,), daemon=True).start()
        else:
            tasks.add_task(process_async, tel, txt)

        return JSONResponse(content={"status":"buffering"})
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return JSONResponse(status_code=500, detail=str(e))

@app.post("/message")
async def direct_msg(msg: WhatsAppMessage):
    try:
        res = run_agent(msg.telefone, msg.mensagem)
        return AgentResponse(success=True, response=res["output"], telefone=msg.telefone, timestamp="")
    except Exception as e:
        return AgentResponse(success=False, response="", telefone="", error=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=settings.server_host, port=settings.server_port, log_level=settings.log_level.lower())
