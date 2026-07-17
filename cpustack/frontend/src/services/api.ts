import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios';
import { message } from 'antd';
import type {
  APIKey,
  APIKeyCreatePayload,
  AdoptWorkerResult,
  CatalogResponse,
  DashboardStats,
  DiscoveryRegisterResult,
  DiscoveryScanResult,
  KnowledgeBase,
  KnowledgeBaseCreatePayload,
  KnowledgeDocument,
  KnowledgeSearchResult,
  LoginPayload,
  LoginResult,
  Model,
  ModelInstance,
  ModelInstanceWithMeta,
  PullModelPayload,
  PullModelResult,
  ResourceUsagePoint,
  TokenTotalUsage,
  TokenUsageSummary,
  User,
  Worker,
  WorkerWithStatus,
} from './types';

/* ---------- axios 实例 ---------- */
const TOKEN_STORAGE_KEY = 'cpustack_token';
const USER_STORAGE_KEY = 'cpustack_user';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
  localStorage.removeItem(USER_STORAGE_KEY);
}

export function getStoredUser(): User | null {
  const raw = localStorage.getItem(USER_STORAGE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as User;
  } catch {
    return null;
  }
}

export function setStoredUser(user: User): void {
  localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user));
}

const request: AxiosInstance = axios.create({
  baseURL: '/',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

/* ---------- 请求拦截器：自动附加 JWT ---------- */
request.interceptors.request.use(
  (config) => {
    const token = getToken();
    if (token && !config.headers?.Authorization) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error),
);

/* ---------- 响应拦截器：统一错误处理 ---------- */
request.interceptors.response.use(
  (response) => response.data,
  (error: AxiosError<{ detail?: string; message?: string }>) => {
    const status = error.response?.status;
    const detail = error.response?.data?.detail || error.response?.data?.message;

    if (status === 401) {
      message.error(detail || '登录已过期，请重新登录');
      clearToken();
      // 避免在登录页循环跳转
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    } else if (status === 403) {
      message.error(detail || '没有权限执行此操作');
    } else if (status && status >= 500) {
      message.error(detail || '服务器内部错误，请稍后再试');
    } else if (status && status >= 400) {
      message.error(detail || error.message || '请求失败');
    } else if (error.request) {
      message.error('网络异常，无法连接到服务器');
    } else {
      message.error(error.message || '未知错误');
    }
    return Promise.reject(error);
  },
);

/* ---------- API 封装 ---------- */
export const api = {
  /* 认证 */
  login(payload: LoginPayload): Promise<LoginResult> {
    return request.post('/v2/auth/login', payload);
  },
  logout(): Promise<void> {
    clearToken();
    return Promise.resolve();
  },
  getCurrentUser(): Promise<User> {
    return request.get('/v2/auth/me');
  },

  /* Dashboard */
  getDashboardStats(): Promise<DashboardStats> {
    return request.get('/v2/dashboard');
  },
  getResourceUsage(): Promise<ResourceUsagePoint[]> {
    return Promise.resolve([]);
  },

  /* Workers */
  listWorkers(): Promise<WorkerWithStatus[]> {
    return request.get('/v2/workers');
  },
  getWorker(id: number): Promise<Worker> {
    return request.get(`/v2/workers/${id}`);
  },
  deleteWorker(id: number): Promise<void> {
    return request.delete(`/v2/workers/${id}`);
  },

  /* Models */
  listModels(): Promise<Model[]> {
    return request.get('/v2/models');
  },
  getModel(id: number): Promise<Model> {
    return request.get(`/v2/models/${id}`);
  },
  createModel(payload: Partial<Model>): Promise<Model> {
    return request.post('/v2/models', payload);
  },
  updateModel(id: number, payload: Partial<Model>): Promise<Model> {
    return request.put(`/v2/models/${id}`, payload);
  },
  deleteModel(id: number): Promise<void> {
    return request.delete(`/v2/models/${id}`);
  },

  /* Instances */
  listInstances(): Promise<ModelInstanceWithMeta[]> {
    return request.get('/v2/models/instances');
  },
  getInstance(id: number): Promise<ModelInstance> {
    return request.get(`/v2/models/instances/${id}`);
  },
  startInstance(id: number): Promise<void> {
    return request.post(`/v2/models/instances/${id}/restart`, {});
  },
  stopInstance(id: number): Promise<void> {
    return request.post(`/v2/models/instances/${id}/stop`, {});
  },
  restartInstance(id: number): Promise<void> {
    return request.post(`/v2/models/instances/${id}/restart`, {});
  },
  getInstanceLogs(id: number): Promise<string> {
    return request.get(`/v2/models/instances/${id}/logs`);
  },
  deleteInstance(id: number): Promise<void> {
    return request.delete(`/v2/models/instances/${id}`);
  },

  /* API Keys */
  listAPIKeys(): Promise<APIKey[]> {
    return request.get('/v2/auth/api-keys');
  },
  createAPIKey(payload: APIKeyCreatePayload): Promise<APIKey> {
    return request.post('/v2/auth/api-keys', payload);
  },
  deleteAPIKey(id: number): Promise<void> {
    return request.delete(`/v2/auth/api-keys/${id}`);
  },

  /* Playground - 流式对话由页面直接使用 fetch 调用，这里提供配置 */
  chatCompletionsURL: '/v1/chat/completions',
  listAvailableModels(): Promise<{ id: string; object: string }[]> {
    // /v1/models 返回 OpenAI 格式 { object: "list", data: [...] }
    // axios 响应拦截器已返回 body，这里再提取 data 字段得到模型数组
    return request.get('/v1/models').then((res: any) => (res as any)?.data ?? []);
  },

  /* 局域网发现 */
  scanLanWorkers(timeout?: number): Promise<DiscoveryScanResult> {
    return request.get('/v2/discovery/scan', {
      params: timeout ? { timeout } : undefined,
    });
  },
  registerDiscoveredWorker(
    ip: string,
    port: number,
    name?: string,
  ): Promise<DiscoveryRegisterResult> {
    return request.post('/v2/discovery/register', { ip, port, name });
  },
  adoptWorker(ip: string, port: number, name?: string): Promise<AdoptWorkerResult> {
    return request.post('/v2/discovery/adopt', { ip, port, name }, { timeout: 60000 });
  },

  /* Token 用量 */
  getTokenUsageSummary(
    modelName?: string,
    days = 7,
  ): Promise<TokenUsageSummary[]> {
    return request.get('/v2/tokens/summary', {
      params: { model_name: modelName, days },
    });
  },
  getTokenTotalUsage(): Promise<TokenTotalUsage> {
    return request.get('/v2/tokens/total');
  },

  /* 模型目录与一键拉取 */
  listModelCatalog(
    category?: string,
    search?: string,
  ): Promise<CatalogResponse> {
    return request.get('/v2/models/catalog', {
      params: { category, search },
    });
  },
  pullModelFromCatalog(payload: PullModelPayload): Promise<PullModelResult> {
    return request.post('/v2/models/pull', payload);
  },

  /* 知识库 */
  listKnowledgeBases(): Promise<KnowledgeBase[]> {
    return request.get('/v2/knowledge-bases');
  },
  createKnowledgeBase(
    payload: KnowledgeBaseCreatePayload,
  ): Promise<KnowledgeBase> {
    return request.post('/v2/knowledge-bases', payload);
  },
  deleteKnowledgeBase(id: number): Promise<void> {
    return request.delete(`/v2/knowledge-bases/${id}`);
  },
  listKnowledgeDocuments(kbId: number): Promise<KnowledgeDocument[]> {
    return request.get(`/v2/knowledge-bases/${kbId}/documents`);
  },
  uploadKnowledgeDocument(kbId: number, file: File): Promise<KnowledgeDocument> {
    const form = new FormData();
    form.append('file', file);
    return request.post(`/v2/knowledge-bases/${kbId}/documents`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  deleteKnowledgeDocument(kbId: number, docId: number): Promise<void> {
    return request.delete(`/v2/knowledge-bases/${kbId}/documents/${docId}`);
  },
  searchKnowledge(
    kbId: number,
    query: string,
    topK = 5,
  ): Promise<KnowledgeSearchResult[]> {
    return request.post(`/v2/knowledge-bases/${kbId}/search`, {
      query,
      top_k: topK,
    });
  },
};

export type RequestOptions = AxiosRequestConfig;
export default request;
