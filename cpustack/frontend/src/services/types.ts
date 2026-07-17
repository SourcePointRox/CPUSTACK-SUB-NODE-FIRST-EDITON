// CPUSTACK 后端数据模型类型定义（对齐 backend/cpustack/schemas）

/* ---------- 通用 ---------- */
export interface Paginated<T> {
  items: T[];
  total: number;
  page?: number;
  pageSize?: number;
}

export interface ApiResult<T> {
  code: number;
  message?: string;
  data: T;
}

/* ---------- 用户 ---------- */
export type UserRole = 'admin' | 'user';

export interface User {
  id: number;
  username: string;
  is_admin: boolean;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface LoginPayload {
  username: string;
  password: string;
}

export interface LoginResult {
  access_token: string;
  token_type?: string;
  user?: User;
}

/* ---------- Worker 节点 ---------- */
export type WorkerState = 'not_ready' | 'ready' | 'unreachable';

export interface Worker {
  id: number;
  name: string;
  ip: string;
  port: number;
  state: WorkerState;
  labels: string | null;
  heartbeat_at: string | null;
  // 以下字段在 GET /v2/workers 中以扁平结构直接返回（无嵌套 status）
  cpu_model: string;
  cpu_cores: number;
  cpu_utilization: number;
  instruction_sets: string[]; // 后端已返回数组，不再是 JSON 字符串
  memory_total: number; // MB
  memory_available: number;
  memory_allocated: number;
  disk_total: number;
  disk_available: number;
  os: string;
  numa_nodes: number;
  created_at?: string;
  updated_at?: string;
}

// 向后兼容别名：后端已不再返回嵌套 status，WorkerWithStatus 等同于 Worker
export type WorkerWithStatus = Worker;

/* ---------- 模型 ---------- */
export type ModelBackend =
  | 'llama_cpp_standalone'
  | 'llama_cpp_rpc'
  | 'prima_cpp'
  | 'data_parallel';

export interface Model {
  id: number;
  name: string;
  display_name: string;
  description: string;
  source_repo: string;
  source_model_id: string;
  // source_filename 不在 GET /v2/models 列表响应中，仅在创建表单中使用，故设为可选
  source_filename?: string;
  backend: ModelBackend;
  replicas: number;
  estimated_memory: number; // MB
  // 后端返回的是字符串数组，不再是 JSON 字符串
  required_instruction_sets: string[];
  // GET /v2/models 列表响应中包含 ready_replicas
  ready_replicas: number;
  created_at?: string;
  updated_at?: string;
}

/* ---------- 模型实例 ---------- */
export type ModelInstanceState =
  | 'pending'
  | 'analyzing'
  | 'scheduled'
  | 'initializing'
  | 'downloading'
  | 'starting'
  | 'running'
  | 'error'
  | 'unreachable';

export interface ModelInstance {
  id: number;
  name: string;
  model_id: number;
  worker_id: number | null;
  model_name: string;
  worker_name: string;
  state: ModelInstanceState;
  allocated_cpu_cores: number;
  allocated_memory: number;
  service_port: number | null;
  download_progress: number;
  error_message: string;
  created_at?: string;
  updated_at?: string;
}

// 向后兼容别名：model_name / worker_name 已合并进 ModelInstance
export type ModelInstanceWithMeta = ModelInstance;

/* ---------- API Key ---------- */
export interface APIKey {
  id: number;
  name: string;
  access_token: string;
  user_id: number;
  allowed_model_names?: string | null;
  expires_at?: string | null;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface APIKeyCreatePayload {
  name: string;
  allowed_model_names?: string | null;
  expires_at?: string | null;
}

/* ---------- Dashboard 统计 ---------- */
export interface DashboardStats {
  total_workers: number;
  ready_workers: number;
  total_cpu_cores: number;
  total_memory_mb: number; // MB
  available_memory_mb: number; // MB
  total_models: number;
  running_instances: number;
}

export interface ResourceUsagePoint {
  timestamp: string;
  cpu_utilization: number;
  memory_utilization: number;
}

/* ---------- 局域网发现 ---------- */
export interface DiscoveredWorker {
  name: string;
  ip: string;
  port: number;
  worker_port: number;
  hostname: string;
  cpu_cores: number;
  memory_total_mb: number;
  responded_at: string;
  registered: boolean;
  registered_worker_id?: number | null;
  registered_name?: string | null;
}

export interface DiscoveryScanResult {
  total: number;
  discovered: DiscoveredWorker[];
  broadcast_addresses: string[];
}

export interface DiscoveryRegisterResult {
  ip: string;
  port: number;
  name: string;
  server_url: string;
  worker_token: string;
  command: string;
}

/* ---------- Token 用量 ---------- */
export interface TokenDailyPoint {
  date: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  request_count: number;
}

export interface TokenUsageSummary {
  model_name: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  request_count: number;
  daily: TokenDailyPoint[];
}

export interface TokenTotalUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  request_count: number;
  model_count: number;
}

/* ---------- 模型目录 ---------- */
export interface CatalogEntry {
  name: string;
  display_name: string;
  description: string;
  parameters: string;
  source_repo: string;
  source_model_id: string;
  source_filename: string;
  quantization_size_gb: number;
  estimated_memory_mb: number;
  required_instruction_sets: string[];
  recommended_backend: string;
  test_purpose: string;
  category: string;
  size_tier: string;
}

export interface CatalogResponse {
  total: number;
  categories: string[];
  entries: CatalogEntry[];
}

export interface PullModelPayload {
  catalog_name: string;
  replicas?: number;
  backend_override?: string;
  custom_model_name?: string;
}

export interface PullModelResult {
  model_id: number;
  model_name: string;
  instances: number;
  message: string;
}

/* ---------- 知识库 ---------- */
export interface KnowledgeBase {
  id: number;
  name: string;
  description: string;
  chunk_size: number;
  chunk_overlap: number;
  doc_count: number;
  chunk_count: number;
  state: string;
  error_message: string;
}

export interface KnowledgeBaseCreatePayload {
  name: string;
  description?: string;
  chunk_size?: number;
  chunk_overlap?: number;
}

export interface KnowledgeDocument {
  id: number;
  kb_id: number;
  filename: string;
  file_size: number;
  mime_type: string;
  chunk_count: number;
  char_count: number;
  state: string;
  error_message: string;
}

export interface KnowledgeSearchResult {
  chunk_id: number;
  doc_id: number;
  filename: string;
  content: string;
  score: number;
  chunk_index: number;
}
