import json
import time
import uuid
import threading
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# Configuration
CONVERSATION_CACHE_MAX_SIZE = 100
DEFAULT_REQUEST_TIMEOUT = 30.0

# Global variables
VALID_CLIENT_KEYS: set = set()
JETBRAINS_JWTS: list = []
current_jwt_index: int = 0
jwt_rotation_lock = threading.Lock()
models_data: Dict[str, Any] = {}
http_client: Optional[httpx.AsyncClient] = None


# Pydantic Models
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


class ChatCompletionChoice(BaseModel):
    message: ChatMessage
    index: int = 0
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: Dict[str, int] = Field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


class StreamChoice(BaseModel):
    delta: Dict[str, Any] = Field(default_factory=dict)
    index: int = 0
    finish_reason: Optional[str] = None


class StreamResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[StreamChoice]


# FastAPI App
app = FastAPI(title="JetBrains AI OpenAI Compatible API")
security = HTTPBearer(auto_error=False)


# Helper functions
def load_models():
    """加载模型配置"""
    try:
        with open("models.json", "r", encoding="utf-8") as f:
            model_ids = json.load(f)

        processed_models = []
        if isinstance(model_ids, list):
            for model_id in model_ids:
                if isinstance(model_id, str):
                    processed_models.append({
                        "id": model_id,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "jetbrains-ai"
                    })

        return {"data": processed_models}
    except Exception as e:
        print(f"加载 models.json 时出错: {e}")
        return {"data": []}


def load_client_api_keys():
    """加载客户端 API 密钥"""
    global VALID_CLIENT_KEYS
    try:
        with open("client_api_keys.json", "r", encoding="utf-8") as f:
            keys = json.load(f)
            if not isinstance(keys, list):
                print("警告: client_api_keys.json 应包含密钥列表")
                VALID_CLIENT_KEYS = set()
                return
            VALID_CLIENT_KEYS = set(keys)
            if not VALID_CLIENT_KEYS:
                print("警告: client_api_keys.json 为空")
            else:
                print(f"成功加载 {len(VALID_CLIENT_KEYS)} 个客户端 API 密钥")
    except FileNotFoundError:
        print("错误: 未找到 client_api_keys.json")
        VALID_CLIENT_KEYS = set()
    except Exception as e:
        print(f"加载 client_api_keys.json 时出错: {e}")
        VALID_CLIENT_KEYS = set()


def load_jetbrains_jwts():
    """加载 JetBrains AI 认证 JWT"""
    global JETBRAINS_JWTS
    try:
        with open("jetbrainsai.json", "r", encoding="utf-8") as f:
            # 假设 jetbrainsai.json 包含一个对象列表，每个对象都有 'jwt' 键
            jwt_data = json.load(f)
            if isinstance(jwt_data, list):
                JETBRAINS_JWTS = [item.get("jwt") for item in jwt_data if "jwt" in item]

        if not JETBRAINS_JWTS:
            print("警告: jetbrainsai.json 中未找到有效的 JWT")
        else:
            print(f"成功加载 {len(JETBRAINS_JWTS)} 个 JetBrains AI JWT")

    except FileNotFoundError:
        print("错误: 未找到 jetbrainsai.json 文件")
        JETBRAINS_JWTS = []
    except Exception as e:
        print(f"加载 jetbrainsai.json 时出错: {e}")
        JETBRAINS_JWTS = []


def get_model_item(model_id: str) -> Optional[Dict]:
    """根据模型ID获取模型配置"""
    for model in models_data.get("data", []):
        if model.get("id") == model_id:
            return model
    return None


async def authenticate_client(auth: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """客户端认证"""
    if not VALID_CLIENT_KEYS:
        raise HTTPException(status_code=503, detail="服务不可用: 未配置客户端 API 密钥")

    if not auth or not auth.credentials:
        raise HTTPException(
            status_code=401,
            detail="需要在 Authorization header 中提供 API 密钥",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth.credentials not in VALID_CLIENT_KEYS:
        raise HTTPException(status_code=403, detail="无效的客户端 API 密钥")


def get_next_jetbrains_jwt() -> str:
    """轮询获取下一个 JetBrains JWT"""
    global current_jwt_index

    if not JETBRAINS_JWTS:
        raise HTTPException(status_code=503, detail="服务不可用: 未配置 JetBrains JWT")

    with jwt_rotation_lock:
        if not JETBRAINS_JWTS:
            raise HTTPException(status_code=503, detail="服务不可用: JetBrains JWT 不可用")
        token_to_use = JETBRAINS_JWTS[current_jwt_index]
        current_jwt_index = (current_jwt_index + 1) % len(JETBRAINS_JWTS)
    return token_to_use


# FastAPI 生命周期事件
@app.on_event("startup")
async def startup():
    global models_data, http_client
    models_data = load_models()
    load_client_api_keys()
    load_jetbrains_jwts()
    http_client = httpx.AsyncClient(timeout=None)
    print("JetBrains AI OpenAI Compatible API 服务器已启动")


@app.on_event("shutdown")
async def shutdown():
    global http_client
    if http_client:
        await http_client.aclose()


# API 端点
@app.get("/v1/models", response_model=ModelList)
async def list_models(_: None = Depends(authenticate_client)):
    """列出可用模型"""
    model_list = []
    for model in models_data.get("data", []):
        model_list.append(ModelInfo(
            id=model.get("id", ""),
            created=model.get("created", int(time.time())),
            owned_by=model.get("owned_by", "jetbrains-ai")
        ))
    return ModelList(data=model_list)


async def openai_stream_adapter(
        api_stream_generator: AsyncGenerator[str, None],
        model_name: str
) -> AsyncGenerator[str, None]:
    """将 JetBrains API 的流转换为 OpenAI 格式的 SSE"""
    stream_id = f"chatcmpl-{uuid.uuid4().hex}"
    first_chunk_sent = False

    try:
        async for line in api_stream_generator:
            if not line or line == "data: end":
                continue

            if line.startswith('data: '):
                try:
                    data = json.loads(line[6:])
                    event_type = data.get("type")

                    if event_type == "Content":
                        content = data.get("content", "")
                        if not content:
                            continue

                        delta_payload = {}
                        if not first_chunk_sent:
                            delta_payload = {"role": "assistant", "content": content}
                            first_chunk_sent = True
                        else:
                            delta_payload = {"content": content}

                        stream_resp = StreamResponse(id=stream_id, model=model_name,
                                                     choices=[StreamChoice(delta=delta_payload)])
                        yield f"data: {stream_resp.json()}\n\n"

                    elif event_type == "FinishMetadata":
                        final_resp = StreamResponse(id=stream_id, model=model_name,
                                                    choices=[StreamChoice(delta={}, finish_reason="stop")])
                        yield f"data: {final_resp.json()}\n\n"
                        break
                except json.JSONDecodeError:
                    print(f"警告: 无法解析的 JSON 行: {line}")
                    continue

        yield "data: [DONE]\n\n"

    except Exception as e:
        print(f"流式适配器错误: {e}")
        error_resp = StreamResponse(
            id=stream_id,
            model=model_name,
            choices=[StreamChoice(
                delta={"role": "assistant", "content": f"内部错误: {str(e)}"},
                index=0,
                finish_reason="stop"
            )]
        )
        yield f"data: {error_resp.json()}\n\n"
        yield "data: [DONE]\n\n"


async def aggregate_stream_for_non_stream_response(
        openai_sse_stream: AsyncGenerator[str, None],
        model_name: str
) -> ChatCompletionResponse:
    """聚合流式响应为完整响应"""
    content_parts = []

    async for sse_line in openai_sse_stream:
        if sse_line.startswith("data: ") and sse_line.strip() != "data: [DONE]":
            try:
                data = json.loads(sse_line[6:].strip())
                if data.get("choices") and len(data["choices"]) > 0:
                    delta = data["choices"][0].get("delta", {})
                    if "content" in delta:
                        content_parts.append(delta["content"])
            except:
                pass

    full_content = "".join(content_parts)

    return ChatCompletionResponse(
        model=model_name,
        choices=[ChatCompletionChoice(
            message=ChatMessage(role="assistant", content=full_content),
            finish_reason="stop"
        )]
    )


@app.post("/v1/chat/completions")
async def chat_completions(
        request: ChatCompletionRequest,
        _: None = Depends(authenticate_client)
):
    """创建聊天完成"""
    model_config = get_model_item(request.model)
    if not model_config:
        raise HTTPException(status_code=404, detail=f"模型 {request.model} 未找到")

    auth_token = get_next_jetbrains_jwt()

    # 将 OpenAI 格式的消息转换为 JetBrains 格式
    jetbrains_messages = []
    for msg in request.messages:
        # JetBrains API 需要一个特定的交替格式，这里我们简化处理
        # 实际可能需要更复杂的逻辑来确保用户/助手消息交替
        jetbrains_messages.append({"type": f"{msg.role}_message", "content": msg.content})

    # 创建 API 请求的 payload
    payload = {
        "prompt": "ij.chat.request.new-chat-on-start",  # or other relevant prompt
        "profile": request.model,
        "chat": {
            "messages": jetbrains_messages
        },
        "parameters": {"data": []},
    }

    headers = {
        "User-Agent": "ktor-client",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Accept-Charset": "UTF-8",
        "Cache-Control": "no-cache",
        "grazie-agent": '{"name":"aia:pycharm","version":"251.26094.80.13:251.26094.141"}',  # 可根据需要更新
        "grazie-authenticate-jwt": auth_token,
    }

    async def api_stream_generator():
        """一个包装 httpx 请求的异步生成器"""
        async with http_client.stream("POST", "https://api.jetbrains.ai/user/v5/llm/chat/stream/v7",
                                      json=payload, headers=headers, timeout=300) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line

    # 创建 OpenAI 格式的流
    openai_sse_stream = openai_stream_adapter(
        api_stream_generator(),
        request.model
    )

    # 返回流式或非流式响应
    if request.stream:
        return StreamingResponse(
            openai_sse_stream,
            media_type="text/event-stream"
        )
    else:
        return await aggregate_stream_for_non_stream_response(
            openai_sse_stream,
            request.model
        )


# 主程序入口
if __name__ == "__main__":
    import os

    # 创建示例配置文件（如果不存在）
    if not os.path.exists("client_api_keys.json"):
        with open("client_api_keys.json", "w", encoding="utf-8") as f:
            json.dump(["sk-your-custom-key-here"], f, indent=2)
        print("已创建示例 client_api_keys.json 文件")

    if not os.path.exists("jetbrainsai.json"):
        with open("jetbrainsai.json", "w", encoding="utf-8") as f:
            json.dump([{"jwt": "your-jwt-here"}], f, indent=2)
        print("已创建示例 jetbrainsai.json 文件")

    if not os.path.exists("models.json"):
        with open("models.json", "w", encoding="utf-8") as f:
            json.dump(["anthropic-claude-3.5-sonnet"], f, indent=2)
        print("已创建示例 models.json 文件")

    print("正在启动 JetBrains AI OpenAI Compatible API 服务器...")
    print("端点:")
    print("  GET  /v1/models")
    print("  POST /v1/chat/completions")
    print("\n在 Authorization header 中使用客户端 API 密钥 (Bearer sk-xxx)")

    uvicorn.run(app, host="0.0.0.0", port=8090)