import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  CopyOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { api } from '../services/api';
import type { APIKey, APIKeyCreatePayload } from '../services/types';
import StatusTag from '../components/StatusTag';

const { Text, Paragraph } = Typography;

function maskToken(token: string): string {
  if (!token) return '';
  if (token.length <= 12) return token;
  return `${token.slice(0, 8)}••••••••${token.slice(-4)}`;
}

const APIKeys: React.FC = () => {
  const [data, setData] = useState<APIKey[]>([]);
  const [loading, setLoading] = useState(false);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10 });
  const [createOpen, setCreateOpen] = useState(false);
  const [createLoading, setCreateLoading] = useState(false);
  const [createdKey, setCreatedKey] = useState<APIKey | null>(null);
  const [createForm] = Form.useForm<APIKeyCreatePayload>();

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listAPIKeys();
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

  const handleCreate = async () => {
    const values = await createForm.validateFields();
    setCreateLoading(true);
    try {
      const created = await api.createAPIKey(values);
      message.success(`密钥 ${values.name} 已创建`);
      setCreatedKey(created);
      setCreateOpen(false);
      createForm.resetFields();
      fetchData();
    } catch {
      // ignore
    } finally {
      setCreateLoading(false);
    }
  };

  const handleDelete = async (id: number, name: string) => {
    try {
      await api.deleteAPIKey(id);
      message.success(`密钥 ${name} 已删除`);
      fetchData();
    } catch {
      // ignore
    }
  };

  const copyToken = (token: string) => {
    navigator.clipboard
      .writeText(token)
      .then(() => message.success('已复制到剪贴板'))
      .catch(() => message.error('复制失败'));
  };

  const columns: ColumnsType<APIKey> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: 'Access Token',
      dataIndex: 'access_token',
      key: 'access_token',
      render: (token: string, r) => (
        <Space>
          <Text code>{maskToken(token)}</Text>
          <Tooltip title="复制完整 Token">
            <Button
              size="small"
              type="text"
              icon={<CopyOutlined />}
              onClick={() => copyToken(r.access_token)}
            />
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '模型白名单',
      dataIndex: 'allowed_model_names',
      key: 'allowed_model_names',
      render: (v?: string | null) =>
        v ? <Tag>{v}</Tag> : <Text type="secondary">不限制</Text>,
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      key: 'enabled',
      render: (enabled: boolean) => (
        <StatusTag status={enabled ? 'enabled' : 'disabled'} kind="api_key" />
      ),
    },
    {
      title: '过期时间',
      dataIndex: 'expires_at',
      key: 'expires_at',
      render: (v?: string | null) => {
        if (!v) return <Text type="secondary">永久</Text>;
        const t = dayjs(v);
        return t.isValid() ? <Tooltip title={t.format('YYYY-MM-DD HH:mm:ss')}>{t.fromNow()}</Tooltip> : '-';
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (v?: string) =>
        v ? dayjs(v).format('YYYY-MM-DD HH:mm') : <Text type="secondary">-</Text>,
    },
    {
      title: '操作',
      key: 'action',
      fixed: 'right',
      width: 120,
      render: (_, r) => (
        <Popconfirm
          title="确认删除该密钥？"
          description="删除后使用该密钥的请求将立即失效"
          onConfirm={() => handleDelete(r.id, r.name)}
          okText="确认"
          cancelText="取消"
        >
          <Button danger size="small" icon={<DeleteOutlined />}>
            删除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>API 密钥</h2>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
            刷新
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            创建密钥
          </Button>
        </Space>
      </div>

      <Table<APIKey>
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={data}
        scroll={{ x: 900 }}
        pagination={{
          current: pagination.current,
          pageSize: pagination.pageSize,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 个密钥`,
          onChange: (current, pageSize) => setPagination({ current, pageSize }),
        }}
      />

      <Modal
        title="创建 API 密钥"
        open={createOpen}
        onOk={handleCreate}
        confirmLoading={createLoading}
        onCancel={() => {
          setCreateOpen(false);
          createForm.resetFields();
        }}
        okText="创建"
        cancelText="取消"
      >
        <Form<APIKeyCreatePayload>
          form={createForm}
          layout="vertical"
          initialValues={{ allowed_model_names: '' }}
        >
          <Form.Item
            label="名称"
            name="name"
            rules={[{ required: true, message: '请输入密钥名称' }]}
          >
            <Input placeholder="如 playground-key" />
          </Form.Item>
          <Form.Item
            label="模型白名单"
            name="allowed_model_names"
            tooltip="留空表示不限制，可填写模型名，多个用逗号分隔"
          >
            <Input placeholder="如 Llama-3.2-3B" />
          </Form.Item>
          <Form.Item label="过期时间（可选，ISO 字符串）" name="expires_at">
            <Input placeholder="如 2026-12-31T23:59:59Z" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 创建后展示完整 token（仅一次） */}
      <Modal
        title="密钥已创建"
        open={!!createdKey}
        onOk={() => setCreatedKey(null)}
        onCancel={() => setCreatedKey(null)}
        okText="我已保存"
        cancelText="关闭"
      >
        <Paragraph>
          请立即保存以下 Access Token，关闭后将无法再次完整查看：
        </Paragraph>
        <Paragraph code copyable={{ text: createdKey?.access_token || '' }}>
          {createdKey?.access_token}
        </Paragraph>
      </Modal>
    </div>
  );
};

export default APIKeys;
