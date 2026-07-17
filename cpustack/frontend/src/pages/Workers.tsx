import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
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
  DeleteOutlined,
  ReloadOutlined,
  RadarChartOutlined,
  CopyOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { api } from '../services/api';
import type { DiscoveredWorker, DiscoveryRegisterResult, Worker } from '../services/types';
import StatusTag from '../components/StatusTag';

const { Text, Paragraph } = Typography;

function formatMemory(mb: number): string {
  if (!mb) return '-';
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${mb} MB`;
}

function formatHeartbeat(at?: string | null): React.ReactNode {
  if (!at) return <Text type="secondary">无</Text>;
  const t = dayjs(at);
  if (!t.isValid()) return <Text type="secondary">-</Text>;
  return <Tooltip title={t.format('YYYY-MM-DD HH:mm:ss')}>{t.fromNow()}</Tooltip>;
}

const Workers: React.FC = () => {
  const [data, setData] = useState<Worker[]>([]);
  const [loading, setLoading] = useState(false);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10 });
  const [scanOpen, setScanOpen] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredWorker[]>([]);
  const [registerResult, setRegisterResult] = useState<DiscoveryRegisterResult | null>(null);
  const [adoptingIp, setAdoptingIp] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listWorkers();
      setData(list ?? []);
    } catch {
      // 错误已在拦截器提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleDelete = async (id: number, name: string) => {
    try {
      await api.deleteWorker(id);
      message.success(`节点 ${name} 已移除`);
      fetchData();
    } catch {
      // ignore
    }
  };

  const handleScan = async () => {
    setScanning(true);
    setDiscovered([]);
    setRegisterResult(null);
    try {
      const result = await api.scanLanWorkers(5);
      setDiscovered(result?.discovered ?? []);
      if ((result?.discovered ?? []).length === 0) {
        message.info('未发现局域网内的 CPUSTACK Worker 节点');
      }
    } catch {
      // ignore
    } finally {
      setScanning(false);
    }
  };

  const handleRegisterDiscovered = async (dw: DiscoveredWorker) => {
    try {
      const result = await api.registerDiscoveredWorker(dw.ip, dw.port, dw.name);
      setRegisterResult(result);
      message.success(`已生成 ${dw.ip} 的注册引导命令`);
      fetchData();
    } catch {
      // ignore
    }
  };

  const handleAdopt = async (dw: DiscoveredWorker) => {
    setAdoptingIp(`${dw.ip}:${dw.port}`);
    try {
      const result = await api.adoptWorker(dw.ip, dw.port, dw.name);
      if (result.ok) {
        message.success(`节点 ${dw.ip} ${result.message}`);
        // 立即刷新扫描列表和节点列表
        await handleScan();
        fetchData();
      } else {
        message.error(`添加失败：${result.message}`);
      }
    } catch {
      // 错误已在拦截器提示
    } finally {
      setAdoptingIp(null);
    }
  };

  const columns: ColumnsType<Worker> = [
    {
      title: '节点名',
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
    },
    {
      title: 'IP',
      key: 'ip',
      render: (_, r) => `${r.ip}:${r.port}`,
    },
    {
      title: '状态',
      dataIndex: 'state',
      key: 'state',
      render: (state: string) => <StatusTag status={state} kind="worker" />,
      filters: [
        { text: '就绪', value: 'ready' },
        { text: '未就绪', value: 'not_ready' },
        { text: '不可达', value: 'unreachable' },
      ],
      onFilter: (value, record) => record.state === value,
    },
    {
      title: 'CPU 核数',
      key: 'cpu_cores',
      render: (_, r) =>
        r.cpu_cores ? `${r.cpu_cores}（使用率 ${r.cpu_utilization ?? 0}%）` : '-',
      sorter: (a, b) => (a.cpu_cores ?? 0) - (b.cpu_cores ?? 0),
    },
    {
      title: '内存',
      key: 'memory',
      render: (_, r) =>
        r.memory_total
          ? `${formatMemory(r.memory_total)}（已分配 ${formatMemory(r.memory_allocated)}）`
          : '-',
    },
    {
      title: 'CPU 型号',
      key: 'cpu_model',
      ellipsis: true,
      render: (_, r) => r.cpu_model || '-',
    },
    {
      title: '指令集',
      key: 'instruction_sets',
      render: (_, r) => {
        const sets = r.instruction_sets ?? [];
        if (sets.length === 0) return <Text type="secondary">-</Text>;
        return (
          <Space size={[4, 4]} wrap>
            {sets.map((s) => (
              <Tag key={s}>{s}</Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: '心跳时间',
      key: 'heartbeat_at',
      render: (_, r) => formatHeartbeat(r.heartbeat_at),
    },
    {
      title: '操作',
      key: 'action',
      fixed: 'right',
      width: 120,
      render: (_, r) => (
        <Popconfirm
          title="确认移除该节点？"
          description={`将移除节点 ${r.name}`}
          onConfirm={() => handleDelete(r.id, r.name)}
          okText="确认"
          cancelText="取消"
        >
          <Button danger size="small" icon={<DeleteOutlined />}>
            移除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  const discoveredColumns: ColumnsType<DiscoveredWorker> = [
    { title: '节点名', dataIndex: 'name', key: 'name', ellipsis: true },
    { title: 'IP', key: 'ip', render: (_, r) => `${r.ip}:${r.port}` },
    { title: '主机名', dataIndex: 'hostname', key: 'hostname', ellipsis: true },
    {
      title: 'CPU/内存',
      key: 'res',
      render: (_, r) => (r.cpu_cores ? `${r.cpu_cores} 核 / ${formatMemory(r.memory_total_mb)}` : '-'),
    },
    {
      title: '状态',
      key: 'state',
      render: (_, r) =>
        r.registered ? (
          <Tag color="green">已注册（{r.registered_name}）</Tag>
        ) : (
          <Tag color="orange">未注册</Tag>
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 160,
      render: (_, r) =>
        r.registered ? (
          <Tag color="green">已在算力池</Tag>
        ) : (
          <Space size={4}>
            <Button
              type="primary"
              size="small"
              icon={<PlusOutlined />}
              loading={adoptingIp === `${r.ip}:${r.port}`}
              onClick={() => handleAdopt(r)}
            >
              一键添加
            </Button>
            <Tooltip title="生成手动注册命令（备用）">
              <Button
                size="small"
                icon={<CopyOutlined />}
                onClick={() => handleRegisterDiscovered(r)}
              />
            </Tooltip>
          </Space>
        ),
    },
  ];

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>节点列表</h2>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
            刷新
          </Button>
          <Button
            type="primary"
            icon={<RadarChartOutlined />}
            onClick={() => {
              setScanOpen(true);
              handleScan();
            }}
          >
            扫描局域网
          </Button>
        </Space>
      </div>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="局域网自动发现 + 一键添加"
        description="Worker 节点启动后会监听 UDP 30090 端口响应主节点扫描。点击「扫描局域网」发现节点后，点击「一键添加」即可自动接管注册并入算力池，无需在子节点手动执行命令。"
      />

      <Table<Worker>
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={data}
        scroll={{ x: 1100 }}
        pagination={{
          current: pagination.current,
          pageSize: pagination.pageSize,
          showSizeChanger: true,
          showQuickJumper: true,
          showTotal: (total) => `共 ${total} 个节点`,
          onChange: (current, pageSize) => setPagination({ current, pageSize }),
        }}
      />

      <Modal
        title="局域网节点扫描"
        open={scanOpen}
        onCancel={() => setScanOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setScanOpen(false)}>关闭</Button>
            <Button type="primary" icon={<ReloadOutlined />} loading={scanning} onClick={handleScan}>
              重新扫描
            </Button>
          </Space>
        }
        width={780}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="一键添加子节点"
          description="发现未注册节点后，点击「一键添加」按钮，主节点会主动推送注册指令，子节点自动注册并入算力池。无需手动执行命令。"
        />
        <Table<DiscoveredWorker>
          rowKey={(r) => `${r.ip}:${r.port}`}
          size="small"
          loading={scanning}
          columns={discoveredColumns}
          dataSource={discovered}
          pagination={false}
          locale={{ emptyText: scanning ? '扫描中...' : '未发现节点' }}
        />

        {registerResult && (
          <div style={{ marginTop: 16 }}>
            <Typography.Title level={5}>注册引导命令（在目标节点 {registerResult.ip} 执行）</Typography.Title>
            <Paragraph>
              <pre
                style={{
                  background: 'rgba(0,0,0,0.85)',
                  color: '#d4d4d4',
                  padding: 12,
                  borderRadius: 6,
                  fontSize: 12,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {registerResult.command}
              </pre>
              <Button
                size="small"
                icon={<CopyOutlined />}
                onClick={() => {
                  navigator.clipboard?.writeText(registerResult.command);
                  message.success('命令已复制到剪贴板');
                }}
              >
                复制命令
              </Button>
            </Paragraph>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default Workers;
