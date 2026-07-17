import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  AppstoreOutlined,
  CloudDownloadOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { api } from '../services/api';
import type { CatalogEntry, Model, ModelBackend } from '../services/types';

const { Text } = Typography;

const BACKEND_LABELS: Record<ModelBackend, string> = {
  llama_cpp_standalone: 'llama.cpp 单机',
  llama_cpp_rpc: 'llama.cpp RPC',
  prima_cpp: 'prima.cpp 流水线',
  data_parallel: '数据并行',
};

const SOURCE_LABELS: Record<string, string> = {
  huggingface: 'HuggingFace',
  modelscope: 'ModelScope',
};

const SIZE_TIER_LABELS: Record<string, { label: string; color: string }> = {
  tiny: { label: '极小', color: 'green' },
  small: { label: '小型', color: 'blue' },
  medium: { label: '中型', color: 'orange' },
  large: { label: '大型', color: 'red' },
  unknown: { label: '未知', color: 'default' },
};

function formatMemory(mb: number): string {
  if (!mb) return '-';
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${mb} MB`;
}

interface CreateModelFormValues {
  name: string;
  display_name?: string;
  source_repo: string;
  source_model_id: string;
  source_filename?: string;
  backend: ModelBackend;
  replicas: number;
  estimated_memory?: number;
  required_instruction_sets?: string;
}

const Models: React.FC = () => {
  const navigate = useNavigate();
  const [data, setData] = useState<Model[]>([]);
  const [loading, setLoading] = useState(false);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10 });
  const [createOpen, setCreateOpen] = useState(false);
  const [createLoading, setCreateLoading] = useState(false);
  const [createForm] = Form.useForm<CreateModelFormValues>();

  // 模型目录浏览器状态
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [catalogEntries, setCatalogEntries] = useState<CatalogEntry[]>([]);
  const [catalogCategories, setCatalogCategories] = useState<string[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogSearch, setCatalogSearch] = useState('');
  const [catalogCategory, setCatalogCategory] = useState<string | undefined>(undefined);
  const [pullingName, setPullingName] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listModels();
      setData(list ?? []);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const fetchCatalog = useCallback(async () => {
    setCatalogLoading(true);
    try {
      const resp = await api.listModelCatalog(catalogCategory, catalogSearch || undefined);
      setCatalogEntries(resp?.entries ?? []);
      setCatalogCategories(resp?.categories ?? []);
    } catch {
      // ignore
    } finally {
      setCatalogLoading(false);
    }
  }, [catalogCategory, catalogSearch]);

  useEffect(() => {
    if (catalogOpen) {
      fetchCatalog();
    }
  }, [catalogOpen, fetchCatalog]);

  const handleDelete = async (id: number, name: string) => {
    try {
      await api.deleteModel(id);
      message.success(`模型 ${name} 已删除`);
      fetchData();
    } catch {
      // ignore
    }
  };

  const handleCreate = async () => {
    const values = await createForm.validateFields();
    const instructionSets: string[] =
      values.required_instruction_sets && values.required_instruction_sets.trim()
        ? values.required_instruction_sets
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean)
        : [];
    const payload: Partial<Model> = {
      name: values.name,
      display_name: values.display_name,
      source_repo: values.source_repo,
      source_model_id: values.source_model_id,
      source_filename: values.source_filename,
      backend: values.backend,
      replicas: values.replicas,
      estimated_memory: values.estimated_memory,
      required_instruction_sets: instructionSets,
    };
    setCreateLoading(true);
    try {
      await api.createModel(payload);
      message.success(`模型 ${values.name} 已创建`);
      setCreateOpen(false);
      createForm.resetFields();
      fetchData();
    } catch {
      // ignore
    } finally {
      setCreateLoading(false);
    }
  };

  const handlePull = async (entry: CatalogEntry) => {
    setPullingName(entry.name);
    try {
      const result = await api.pullModelFromCatalog({
        catalog_name: entry.name,
        replicas: 1,
        backend_override: entry.recommended_backend,
      });
      message.success(result.message || `模型 ${entry.display_name} 已开始部署`);
      fetchData();
    } catch {
      // ignore
    } finally {
      setPullingName(null);
    }
  };

  const columns: ColumnsType<Model> = [
    {
      title: '模型名',
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
      render: (name, r) => (
        <Tooltip title={r.display_name || r.source_model_id}>
          {name}
        </Tooltip>
      ),
    },
    {
      title: '来源',
      key: 'source',
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <span>{SOURCE_LABELS[r.source_repo] || r.source_repo}</span>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {r.source_model_id}
          </Text>
        </Space>
      ),
    },
    {
      title: '后端',
      dataIndex: 'backend',
      key: 'backend',
      render: (b: ModelBackend) => <Tag color="blue">{BACKEND_LABELS[b] ?? b}</Tag>,
      filters: Object.entries(BACKEND_LABELS).map(([value, text]) => ({ text, value })),
      onFilter: (value, record) => record.backend === value,
    },
    {
      title: '副本数',
      dataIndex: 'replicas',
      key: 'replicas',
      sorter: (a, b) => a.replicas - b.replicas,
    },
    {
      title: '预估内存',
      dataIndex: 'estimated_memory',
      key: 'estimated_memory',
      render: (v: number) => formatMemory(v),
    },
    {
      title: '状态',
      key: 'state',
      render: (_, r) => {
        const ready = r.ready_replicas ?? 0;
        const total = r.replicas ?? 0;
        const color = ready >= total && total > 0 ? 'green' : ready > 0 ? 'orange' : 'default';
        return <Tag color={color}>{`${ready} / ${total} 就绪`}</Tag>;
      },
    },
    {
      title: '操作',
      key: 'action',
      fixed: 'right',
      width: 200,
      render: (_, r) => (
        <Space size={8}>
          <Button
            size="small"
            icon={<ExperimentOutlined />}
            onClick={() => navigate('/instances')}
          >
            实例
          </Button>
          <Popconfirm
            title="确认删除该模型？"
            onConfirm={() => handleDelete(r.id, r.name)}
            okText="确认"
            cancelText="取消"
          >
            <Button danger size="small" icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>模型列表</h2>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
            刷新
          </Button>
          <Button
            type="primary"
            icon={<CloudDownloadOutlined />}
            onClick={() => setCatalogOpen(true)}
          >
            从目录拉取
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            注册模型
          </Button>
        </Space>
      </div>

      <Table<Model>
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={data}
        scroll={{ x: 1100 }}
        pagination={{
          current: pagination.current,
          pageSize: pagination.pageSize,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 个模型`,
          onChange: (current, pageSize) => setPagination({ current, pageSize }),
        }}
      />

      {/* 创建模型对话框 */}
      <Modal
        title="注册新模型"
        open={createOpen}
        onOk={handleCreate}
        confirmLoading={createLoading}
        onCancel={() => {
          setCreateOpen(false);
          createForm.resetFields();
        }}
        okText="创建"
        cancelText="取消"
        width={560}
      >
        <Form<CreateModelFormValues>
          form={createForm}
          layout="vertical"
          initialValues={{ backend: 'llama_cpp_standalone', replicas: 1 }}
        >
          <Form.Item
            label="模型名"
            name="name"
            rules={[{ required: true, message: '请输入模型名' }]}
          >
            <Input placeholder="如 Llama-3.2-3B" />
          </Form.Item>
          <Form.Item label="显示名" name="display_name">
            <Input placeholder="可选" />
          </Form.Item>
          <Form.Item
            label="来源仓库"
            name="source_repo"
            rules={[{ required: true, message: '请选择来源仓库' }]}
          >
            <Select
              options={[
                { value: 'huggingface', label: 'HuggingFace' },
                { value: 'modelscope', label: 'ModelScope' },
              ]}
            />
          </Form.Item>
          <Form.Item
            label="来源模型 ID"
            name="source_model_id"
            rules={[{ required: true, message: '请输入来源模型 ID' }]}
          >
            <Input placeholder="如 meta-llama/Llama-3.2-3B" />
          </Form.Item>
          <Form.Item label="GGUF 文件名" name="source_filename">
            <Input placeholder="可选，如 *.Q4_K_M.gguf" />
          </Form.Item>
          <Form.Item
            label="推理后端"
            name="backend"
            rules={[{ required: true, message: '请选择后端' }]}
          >
            <Select
              options={Object.entries(BACKEND_LABELS).map(([value, label]) => ({
                value,
                label,
              }))}
            />
          </Form.Item>
          <Form.Item
            label="副本数"
            name="replicas"
            rules={[{ required: true, message: '请输入副本数' }]}
          >
            <InputNumber min={1} max={10} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="预估内存 (MB)" name="estimated_memory">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            label="所需指令集（逗号分隔）"
            name="required_instruction_sets"
            tooltip="如 AVX2,AVX-512；将以字符串数组形式提交到后端"
          >
            <Input placeholder="如 AVX2,AVX-512,AMX" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 模型目录浏览器 */}
      <Modal
        title={
          <Space>
            <AppstoreOutlined />
            <span>模型目录 - 一键拉取部署</span>
          </Space>
        }
        open={catalogOpen}
        onCancel={() => setCatalogOpen(false)}
        footer={null}
        width={920}
      >
        <Space style={{ marginBottom: 12, width: '100%' }} direction="vertical">
          <Space wrap>
            <Input
              allowClear
              prefix={<SearchOutlined />}
              placeholder="搜索模型名 / 仓库 ID"
              style={{ width: 320 }}
              value={catalogSearch}
              onChange={(e) => setCatalogSearch(e.target.value)}
            />
            <Segmented
              value={catalogCategory ?? '全部'}
              onChange={(v) => setCatalogCategory(v === '全部' ? undefined : String(v))}
              options={['全部', ...catalogCategories]}
            />
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            全部采用 GGUF Q4_K_M 量化（CPU 推理甜点）。点击「一键部署」将自动创建模型并触发调度器下载与启动。
          </Text>
        </Space>

        {catalogLoading && !catalogEntries.length ? (
          <Empty description="加载中..." />
        ) : catalogEntries.length === 0 ? (
          <Empty description="未找到匹配的模型" />
        ) : (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
              gap: 12,
              maxHeight: 480,
              overflow: 'auto',
              padding: 4,
            }}
          >
            {catalogEntries.map((entry) => {
              const tier = SIZE_TIER_LABELS[entry.size_tier] || SIZE_TIER_LABELS.unknown;
              return (
                <Card
                  key={entry.name}
                  size="small"
                  title={
                    <Space size={4}>
                      <Text strong style={{ fontSize: 13 }}>
                        {entry.display_name}
                      </Text>
                      <Tag color={tier.color}>{tier.label}</Tag>
                    </Space>
                  }
                  extra={
                    <Tag color="blue">{entry.category}</Tag>
                  }
                >
                  <div style={{ marginBottom: 8 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {entry.description || entry.source_model_id}
                    </Text>
                  </div>
                  <Space size={4} wrap style={{ marginBottom: 8 }}>
                    <Tag>{entry.parameters || '-'}</Tag>
                    <Tag>{entry.quantization_size_gb || 0} GB</Tag>
                    <Tag>{formatMemory(entry.estimated_memory_mb)}</Tag>
                    <Tooltip title={entry.source_model_id}>
                      <Tag style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {SOURCE_LABELS[entry.source_repo] || entry.source_repo}
                      </Tag>
                    </Tooltip>
                  </Space>
                  {entry.test_purpose && (
                    <div style={{ marginBottom: 8, fontSize: 11, color: 'rgba(0,0,0,0.45)' }}>
                      {entry.test_purpose}
                    </div>
                  )}
                  <Button
                    type="primary"
                    size="small"
                    block
                    icon={<CloudDownloadOutlined />}
                    loading={pullingName === entry.name}
                    onClick={() => handlePull(entry)}
                  >
                    一键部署
                  </Button>
                </Card>
              );
            })}
          </div>
        )}
      </Modal>
    </div>
  );
};

export default Models;
