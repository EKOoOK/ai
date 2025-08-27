// app/api/[[...route]]/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { readFile } from 'fs/promises';
import { join } from 'path';

// ===== 类型定义 =====
interface ChatMessage {
  role: string;
  content: string;
}

interface ChatCompletionRequest {
  model: string;
  messages: ChatMessage[];
  stream?: boolean;
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
}

interface ModelInfo {
  id: string;
  object: string;
  created: number;
  owned_by: string;
}

interface ChatCompletionResponse {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: Array<{
    message: ChatMessage;
    index: number;
    finish_reason: string;
  }>;
  usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

// ===== 全局配置 =====
let VALID_CLIENT_KEYS: Set<string> = new Set();
let JETBRAINS_JWTS: string[] = [];
let currentJwtIndex = 0;
let modelsData: ModelInfo[] = [];

// ===== 工具函数 =====
async function loadConfig() {
  try {
    // 加载客户端API密钥
    const keysData = await readFile(join(process.cwd(), 'client_api_keys.json'), 'utf-8');
    const keys = JSON.parse(keysData);
    VALID_CLIENT_KEYS = new Set(Array.isArray(keys) ? keys : []);

    // 加载JetBrains JWT
    const jwtData = await readFile(join(process.cwd(), 'jetbrainsai.json'), 'utf-8');
    const jwts = JSON.parse(jwtData);
    JETBRAINS_JWTS = Array.isArray(jwts) ? jwts.map(item => item.jwt).filter(Boolean) : [];

    // 加载模型配置
    const modelsJson = await readFile(join(process.cwd(), 'models.json'), 'utf-8');
    const models = JSON.parse(modelsJson);
    modelsData = Array.isArray(models) ? models.map((id: string) => ({
      id,
      object: 'model',
      created: Math.floor(Date.now() / 1000),
      owned_by: 'jetbrains-ai'
    })) : [];

    console.log(`✅ 配置加载成功: ${VALID_CLIENT_KEYS.size} 个密钥, ${JETBRAINS_JWTS.length} 个JWT, ${modelsData.length} 个模型`);
  } catch (error) {
    console.error('❌ 配置加载失败:', error);
    throw new Error('配置文件加载失败');
  }
}

function authenticateClient(request: NextRequest): void {
  const authHeader = request.headers.get('authorization');

  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    throw new Error('需要在 Authorization header 中提供 API 密钥');
  }

  const token = authHeader.slice(7);
  if (!VALID_CLIENT_KEYS.has(token)) {
    throw new Error('无效的客户端 API 密钥');
  }
}

function getNextJetBrainsJWT(): string {
  if (JETBRAINS_JWTS.length === 0) {
    throw new Error('服务不可用: 未配置 JetBrains JWT');
  }

  const jwt = JETBRAINS_JWTS[currentJwtIndex];
  currentJwtIndex = (currentJwtIndex + 1) % JETBRAINS_JWTS.length;
  return jwt;
}

function generateId(): string {
  return `chatcmpl-${Math.random().toString(36).substring(2, 15)}`;
}

// ===== 流式响应处理 =====
async function* processJetBrainsStream(response: Response, model: string) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error('无法读取响应流');

  const decoder = new TextDecoder();
  const streamId = generateId();
  let firstChunk = true;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      const chunk = decoder.decode(value, { stream: true });
      const lines = chunk.split('\n');

      for (const line of lines) {
        if (!line.trim() || line === 'data: end') continue;

        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6));
            const eventType = data.type;

            if (eventType === 'Content') {
              const content = data.content || '';
              if (!content) continue;

              const delta = firstChunk
                ? { role: 'assistant', content }
                : { content };

              firstChunk = false;

              const streamResponse = {
                id: streamId,
                object: 'chat.completion.chunk',
                created: Math.floor(Date.now() / 1000),
                model,
                choices: [{ delta, index: 0, finish_reason: null }]
              };

              yield `data: ${JSON.stringify(streamResponse)}\n\n`;

            } else if (eventType === 'FinishMetadata') {
              const finalResponse = {
                id: streamId,
                object: 'chat.completion.chunk',
                created: Math.floor(Date.now() / 1000),
                model,
                choices: [{ delta: {}, index: 0, finish_reason: 'stop' }]
              };

              yield `data: ${JSON.stringify(finalResponse)}\n\n`;
              break;
            }
          } catch (e) {
            console.warn('⚠️ JSON 解析失败:', line);
          }
        }
      }
    }

    yield 'data: [DONE]\n\n';
  } finally {
    reader.releaseLock();
  }
}

async function aggregateStreamResponse(
  streamGenerator: AsyncGenerator<string>,
  model: string
): Promise<ChatCompletionResponse> {
  const contentParts: string[] = [];

  for await (const chunk of streamGenerator) {
    if (chunk.startsWith('data: ') && chunk.trim() !== 'data: [DONE]') {
      try {
        const data = JSON.parse(chunk.slice(6).trim());
        const delta = data.choices?.[0]?.delta;
        if (delta?.content) {
          contentParts.push(delta.content);
        }
      } catch (e) {
        // 忽略解析错误
      }
    }
  }

  return {
    id: generateId(),
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{
      message: { role: 'assistant', content: contentParts.join('') },
      index: 0,
      finish_reason: 'stop'
    }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }
  };
}

// ===== API 路由处理 =====
export async function GET(request: NextRequest) {
  const url = new URL(request.url);
  const pathname = url.pathname;

  try {
    // 加载配置（如果还未加载）
    if (VALID_CLIENT_KEYS.size === 0) {
      await loadConfig();
    }

    // 认证检查
    authenticateClient(request);

    // 处理 /v1/models 端点
    if (pathname.endsWith('/v1/models')) {
      return NextResponse.json({
        object: 'list',
        data: modelsData
      });
    }

    return NextResponse.json({ error: '端点未找到' }, { status: 404 });

  } catch (error) {
    const message = error instanceof Error ? error.message : '认证失败';
    return NextResponse.json({ error: message }, { status: 401 });
  }
}

export async function POST(request: NextRequest) {
  const url = new URL(request.url);
  const pathname = url.pathname;

  try {
    // 加载配置（如果还未加载）
    if (VALID_CLIENT_KEYS.size === 0) {
      await loadConfig();
    }

    // 认证检查
    authenticateClient(request);

    // 处理 /v1/chat/completions 端点
    if (pathname.endsWith('/v1/chat/completions')) {
      const body: ChatCompletionRequest = await request.json();

      // 验证模型
      const modelExists = modelsData.some(m => m.id === body.model);
      if (!modelExists) {
        return NextResponse.json(
          { error: `模型 ${body.model} 未找到` },
          { status: 404 }
        );
      }

      // 获取 JWT
      const authToken = getNextJetBrainsJWT();

      // 转换消息格式
      const jetbrainsMessages = body.messages.map(msg => ({
        type: `${msg.role}_message`,
        content: msg.content
      }));

      // 构建请求载荷
      const payload = {
        prompt: 'ij.chat.request.new-chat-on-start',
        profile: body.model,
        chat: { messages: jetbrainsMessages },
        parameters: { data: [] }
      };

      // 请求头
      const headers = {
        'User-Agent': 'ktor-client',
        'Accept': 'text/event-stream',
        'Content-Type': 'application/json',
        'Accept-Charset': 'UTF-8',
        'Cache-Control': 'no-cache',
        'grazie-agent': '{"name":"aia:nextjs","version":"1.0.0"}',
        'grazie-authenticate-jwt': authToken
      };

      // 发起请求到 JetBrains API
      const response = await fetch(
        'https://api.jetbrains.ai/user/v5/llm/chat/stream/v7',
        {
          method: 'POST',
          headers,
          body: JSON.stringify(payload)
        }
      );

      if (!response.ok) {
        throw new Error(`JetBrains API 请求失败: ${response.status}`);
      }

      // 处理流式响应
      const streamGenerator = processJetBrainsStream(response, body.model);

      if (body.stream) {
        // 返回流式响应
        const stream = new ReadableStream({
          async start(controller) {
            const encoder = new TextEncoder();
            try {
              for await (const chunk of streamGenerator) {
                controller.enqueue(encoder.encode(chunk));
              }
            } catch (error) {
              console.error('流式处理错误:', error);
            } finally {
              controller.close();
            }
          }
        });

        return new Response(stream, {
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
          }
        });
      } else {
        // 聚合流式响应
        const result = await aggregateStreamResponse(streamGenerator, body.model);
        return NextResponse.json(result);
      }
    }

    return NextResponse.json({ error: '端点未找到' }, { status: 404 });

  } catch (error) {
    console.error('❌ API 错误:', error);
    const message = error instanceof Error ? error.message : '内部服务器错误';
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
