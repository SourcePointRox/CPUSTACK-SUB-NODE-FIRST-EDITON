import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  CaretRightOutlined,
  DeleteOutlined,
  ReloadOutlined,
  FileTextOutlined,
  PoweroffOutlined,
  RedoOutlined,
} from '@ant-design/icons';
import { api } from '../services/api';
import type { ModelInstanceWithMeta, ModelInstanceState } from '../services/types';
import StatusTag from '../components/StatusTag';

const { Paragraph, Text } = Typography;

const RUNNING_STATES: ModelInstanceState[] = ['running', 'starting', 'downloading', 'initializing'];

const Instances: React.FC = () => {
  const [data, setData] = useState<ModelInstanceWithMeta[]>([]);
  const [loading, setLoading] = useState(false);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10 });
  const [logTarget, setLogTarget] = useState<ModelInstanceWithMeta | null>(null);
  const [logText, setLogText] = useState('');
  const [logLoading, setLogLoading] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listInstances();
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

  const wrapAction = async (fn: () => Promise<void>, ok: string) => {
    try {
      await fn();
      message.success(ok);
      fetchData();
    } catch {
      // ignore
    }
  };

  const handleStart = (id: number) =>
    wrapAction(() => api.startInstance(id), '实例启动请求已提交');
  const handleStop = (id: number) =>
    wrapAction(() => api.stopInstance(id), '实例停止请求已提交');
  const handleRestart = (id: number) =>
    wrapAction(() => api.restartInstance(id), '实例重启请求已提交');
  const handleDelete = (id: number) =>
    wrapAction(() => api.deleteInstance(id), '实例已删除');

  const viewLogs = async (record: ModelInstanceWithMeta) => {
    setLogTarget(record);
    setLogText('');
    setLogLoading(true);
    try {
      const text = await api.getInstanceLogs(record.id);
      setLogText(typeof text === 'string' ? text : JSON.stringify(text, null, 2));
    } catch {
      setLogText('日志加载失败');
    } finally {
      setLogLoading(false);
    }
  };

  const columns: ColumnsType<ModelInstanceWithMeta> = [
    {
      title: '实例名',
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
    },
    {
      title: '模型',
      key: 'model_name',
      render: (_, r) => r.model_name || `#${r.model_id}`,
    },
    {
      title: '节点',
      key: 'worker_name',
      render: (_, r) => r.worker_name || (r.worker_id ? `#${r.worker_id}` : '-'),
    },
    {
      title: '状态',
      dataIndex: 'state',
      key: 'state',
      render: (s: ModelInstanceState) => <StatusTag status={s} kind="instance" />,
      filters: [
        { text: '运行中', value: 'running' },
        { text: '等待中', value: 'pending' },
        { text: '启动中', value: 'starting' },
        { text: '下载中', value: 'downloading' },
        { text: '错误', value: 'error' },
        { text: '不可达', value: 'unreachable' },
      ],
      onFilter: (value, record) => record.state === value,
    },
    {
      title: '端口',
      dataIndex: 'service_port',
      key: 'service_port',
      render: (p?: number | null) => (p ? <Tag>{p}</Tag> : <Text type="secondary">-</Text>),
    },
    {
      title: '已分配资源',
      key: 'alloc',
      render: (_, r) =>
        r.allocated_cpu_cores || r.allocated_memory
          ? `${r.allocated_cpu_cores} 核 / ${r.allocated_memory} MB`
          : '-',
    },
    {
      title: '下载进度',
      dataIndex: 'download_progress',
      key: 'download_progress',
      render: (v: number) =>
        v > 0 ? `${(v * 100).toFixed(0)}%` : <Text type="secondary">-</Text>,
    },
    {
      title: '操作',
      key: 'action',
      fixed: 'right',
      width: 280,
      render: (_, r) => {
        const running = RUNNING_STATES.includes(r.state);
        return (
          <Space size={4} wrap>
            {running ? (
              <Popconfirm
                title="确认停止该实例？"
                onConfirm={() => handleStop(r.id)}
                okText="确认"
                cancelText="取消"
              >
                <Button size="small" icon={<PoweroffOutlined />}>
                  停止
                </Button>
              </Popconfirm>
            ) : (
              <Button
                type="primary"
                size="small"
                icon={<CaretRightOutlined />}
                onClick={() => handleStart(r.id)}
              >
                启动
              </Button>
            )}
            <Button
              size="small"
              icon={<RedoOutlined />}
              onClick={() => handleRestart(r.id)}
            >
              重启
            </Button>
            <Button
              size="small"
              icon={<FileTextOutlined />}
              onClick={() => viewLogs(r)}
            >
              日志
            </Button>
            <Popconfirm
              title="确认删除该实例？"
              onConfirm={() => handleDelete(r.id)}
              okText="确认"
              cancelText="取消"
            >
              <Button danger size="small" icon={<DeleteOutlined />} />
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>模型实例</h2>
        <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
          刷新
        </Button>
      </div>

      <Table<ModelInstanceWithMeta>
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={data}
        scroll={{ x: 1200 }}
        pagination={{
          current: pagination.current,
          pageSize: pagination.pageSize,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 个实例`,
          onChange: (current, pageSize) => setPagination({ current, pageSize }),
        }}
      />

      <Modal
        title={`实例日志：${logTarget?.name ?? ''}`}
        open={!!logTarget}
        onCancel={() => setLogTarget(null)}
        footer={null}
        width={760}
      >
        <pre
          style={{
            maxHeight: 460,
            overflow: 'auto',
            background: 'rgba(0,0,0,0.85)',
            color: '#d4d4d4',
            padding: 12,
            borderRadius: 6,
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {logLoading ? '加载中...' : logText || '暂无日志'}
        </pre>
        {logTarget?.error_message ? (
          <Paragraph type="danger" style={{ marginTop: 8 }}>
            错误信息：{logTarget.error_message}
          </Paragraph>
        ) : null}
      </Modal>
    </div>
  );
};

export default Instances;
