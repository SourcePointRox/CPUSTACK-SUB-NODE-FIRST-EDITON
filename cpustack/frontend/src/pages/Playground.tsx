import React, { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import { SendOutlined, ClearOutlined } from '@ant-design/icons';
import { api, getToken } from '../services/api';
import { useAuth } from '../store/auth';

const { Text, Paragraph } = Typography;

interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

/**
 * 调用 POST /v1/chat/completions（OpenAI 兼容，流式 SSE）。
 * 使用 fetch + ReadableStream 解析 data: 行。
 */
async function streamChat(
  model: string,
  messages: ChatMessage[],
  onDelta: (delta: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken();
  const resp = await fetch(api.chatCompletionsURL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ model, messages, stream: true }),
    signal,
  });

  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => '');
    throw new Error(`请求失败 (${resp.status}) ${text}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || !trimmed.startsWith('data:')) continue;
      const payload = trimmed.slice(5).trim();
      if (payload === '[DONE]') return;
      try {
        const json = JSON.parse(payload);
        const delta = json?.choices?.[0]?.delta?.content;
        if (delta) onDelta(delta);
      } catch {
        // 忽略非 JSON 心跳等
      }
    }
  }
}

const Playground: React.FC = () => {
  const { user } = useAuth();
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState<string | undefined>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .listAvailableModels()
      .then((list) => {
        const ids = (list ?? []).map((m: any) => m.id).filter(Boolean) as string[];
        setModels(ids);
        if (ids.length > 0 && !model) setModel(ids[0]);
      })
      .catch(() => {
        // 忽略：用户也可手填
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const send = async () => {
    const content = input.trim();
    if (!content || streaming) return;
    if (!model) {
      setError('请先选择模型');
      return;
    }
    setError(null);

    const nextMessages: ChatMessage[] = [
      ...messages,
      { role: 'user', content },
      { role: 'assistant', content: '' },
    ];
    setMessages(nextMessages);
    setInput('');
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChat(
        model,
        nextMessages.slice(0, -1),
        (delta) => {
          setMessages((prev) => {
            const copy = [...prev];
            const last = copy[copy.length - 1];
            if (last && last.role === 'assistant') {
              copy[copy.length - 1] = { ...last, content: last.content + delta };
            }
            return copy;
          });
        },
        controller.signal,
      );
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        // 用户主动取消
      } else {
        setError(e?.message || '对话请求失败');
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    setStreaming(false);
  };

  const clearAll = () => {
    setMessages([]);
    setError(null);
  };

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>对话测试</h2>
        <Space>
          <Select
            showSearch
            style={{ width: 280 }}
            placeholder="选择模型"
            value={model}
            onChange={setModel}
            options={models.map((m) => ({ value: m, label: m }))}
            allowClear
          />
          <Button icon={<ClearOutlined />} onClick={clearAll} disabled={streaming}>
            清空
          </Button>
        </Space>
      </div>

      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
          closable
          onClose={() => setError(null)}
          style={{ marginBottom: 12 }}
        />
      )}

      <Card bodyStyle={{ padding: 0 }}>
        <div
          ref={scrollRef}
          style={{
            height: 'calc(100vh - 340px)',
            minHeight: 320,
            overflow: 'auto',
            padding: 16,
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
            background: 'rgba(0,0,0,0.02)',
          }}
        >
          {messages.length === 0 ? (
            <Empty
              description={
                <span>
                  选择模型并输入消息开始对话
                  <br />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    接口：POST /v1/chat/completions (stream=true)
                  </Text>
                </span>
              }
              style={{ margin: 'auto' }}
            />
          ) : (
            messages.map((m, i) => (
              <div
                key={i}
                style={{ display: 'flex', justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start' }}
              >
                <div className={`chat-bubble ${m.role === 'user' ? 'user' : 'assistant'}`}>
                  <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 4 }}>
                    {m.role === 'user' ? (user?.username || '我') : '助手'}
                  </div>
                  {m.content || (m.role === 'assistant' && streaming ? '思考中…' : '')}
                </div>
              </div>
            ))
          )}
        </div>

        <div style={{ padding: 12, borderTop: '1px solid rgba(0,0,0,0.06)' }}>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              placeholder={streaming ? '生成中…' : '输入消息，Ctrl/⌘ + Enter 发送'}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onPressEnter={(e) => {
                if (e.ctrlKey || e.metaKey) send();
              }}
              disabled={streaming}
            />
            {streaming ? (
              <Button danger onClick={stop} icon={<Spin size="small" />}>
                停止
              </Button>
            ) : (
              <Button type="primary" onClick={send} icon={<SendOutlined />} disabled={!input.trim()}>
                发送
              </Button>
            )}
          </Space.Compact>
          <Paragraph style={{ marginTop: 8, marginBottom: 0 }}>
            <Tag color="blue">流式 SSE</Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              响应将通过 Server-Sent Events 逐 token 渲染
            </Text>
          </Paragraph>
        </div>
      </Card>
    </div>
  );
};

export default Playground;
