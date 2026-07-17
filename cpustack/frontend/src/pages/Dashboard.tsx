import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Row, Spin, Statistic } from 'antd';
import {
  ApiOutlined,
  ClusterOutlined,
  ThunderboltOutlined,
  DatabaseOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { api } from '../services/api';
import type { DashboardStats, ResourceUsagePoint } from '../services/types';
import { VERSION } from '../version';

function formatMemory(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${mb} MB`;
}

const Dashboard: React.FC = () => {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [usage, setUsage] = useState<ResourceUsagePoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, u] = await Promise.all([
        api.getDashboardStats(),
        api.getResourceUsage().catch(() => [] as ResourceUsagePoint[]),
      ]);
      setStats(s);
      setUsage(u);
    } catch (e) {
      setError('加载概览数据失败，请确认后端服务已启动');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    // 30 秒自动刷新
    const timer = setInterval(fetchData, 30000);
    return () => clearInterval(timer);
  }, []);

  const chartOption = useMemo(() => {
    const xs = usage.map((p) => p.timestamp);
    return {
      title: { text: '资源使用率（近 30 分钟）', left: 'center', textStyle: { fontSize: 14, fontWeight: 500 } },
      tooltip: {
        trigger: 'axis',
        formatter: (p: any) =>
          `${p[0].axisValue}<br/>${p.map((i: any) => `${i.marker}${i.seriesName}: ${i.value}%`).join('<br/>')}`,
      },
      legend: { data: ['CPU 使用率', '内存使用率'], bottom: 0, icon: 'roundRect' },
      grid: { left: 50, right: 30, top: 50, bottom: 40 },
      xAxis: { type: 'category', data: xs, boundaryGap: false, axisLine: { lineStyle: { color: '#d9d9d9' } } },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { formatter: '{value}%' },
        splitLine: { lineStyle: { type: 'dashed', color: '#f0f0f0' } },
      },
      series: [
        {
          name: 'CPU 使用率',
          type: 'line',
          smooth: true,
          symbol: 'none',
          areaStyle: {
            color: {
              type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(22, 104, 220, 0.35)' },
                { offset: 1, color: 'rgba(22, 104, 220, 0.02)' },
              ],
            },
          },
          lineStyle: { width: 2.5, color: '#1668dc' },
          itemStyle: { color: '#1668dc' },
          data: usage.map((p) => Number((p.cpu_utilization * 100).toFixed(1))),
        },
        {
          name: '内存使用率',
          type: 'line',
          smooth: true,
          symbol: 'none',
          areaStyle: {
            color: {
              type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(82, 196, 26, 0.35)' },
                { offset: 1, color: 'rgba(82, 196, 26, 0.02)' },
              ],
            },
          },
          lineStyle: { width: 2.5, color: '#52c41a' },
          itemStyle: { color: '#52c41a' },
          data: usage.map((p) => Number((p.memory_utilization * 100).toFixed(1))),
        },
      ],
    };
  }, [usage]);

  const statsCards = useMemo(() => {
    if (!stats) return [];
    return [
      {
        title: '集群总 CPU 核数',
        value: stats.total_cpu_cores,
        icon: <ThunderboltOutlined />,
        iconClass: 'cpu',
        suffix: '核',
      },
      {
        title: '集群总内存',
        value: formatMemory(stats.total_memory_mb),
        icon: <DatabaseOutlined />,
        iconClass: 'mem',
        suffix: `/ 可用 ${formatMemory(stats.available_memory_mb)}`,
        isString: true,
      },
      {
        title: '节点数',
        value: stats.total_workers,
        icon: <ClusterOutlined />,
        iconClass: 'node',
        suffix: `（在线 ${stats.ready_workers}）`,
      },
      {
        title: '运行中模型实例',
        value: stats.running_instances,
        icon: <PlayCircleOutlined />,
        iconClass: 'run',
        suffix: '个',
      },
      {
        title: '已注册模型数',
        value: stats.total_models,
        icon: <ApiOutlined />,
        iconClass: 'model',
        suffix: '个',
      },
    ];
  }, [stats]);

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2>集群概览</h2>
        <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
          刷新
        </Button>
      </div>

      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
          style={{ marginBottom: 16 }}
          action={
            <Button size="small" onClick={fetchData}>
              重试
            </Button>
          }
        />
      )}

      <Spin spinning={loading && !stats}>
        <div className="dashboard-hero">
          <h1>CPUSTACK 分布式 AI 推理集群</h1>
          <p>
            当前版本 v{VERSION} · 集群 {stats?.total_workers ?? 0} 节点 · 在线 {stats?.ready_workers ?? 0} 节点 ·
            运行中实例 {stats?.running_instances ?? 0} 个 · 已注册模型 {stats?.total_models ?? 0} 个
          </p>
        </div>

        <Row gutter={[16, 16]}>
          {statsCards.map((card, idx) => (
            <Col xs={24} sm={12} md={8} lg={6} key={idx}>
              <Card className="stat-card">
                <div className={`stat-icon ${card.iconClass}`}>{card.icon}</div>
                <Statistic
                  title={card.title}
                  value={card.isString ? undefined : card.value}
                  formatter={card.isString ? () => card.value : undefined}
                  suffix={card.suffix}
                />
              </Card>
            </Col>
          ))}
        </Row>

        <Card className="chart-card" style={{ marginTop: 16 }}>
          {usage.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 0', color: 'rgba(0,0,0,0.45)' }}>
              暂无资源使用率数据
            </div>
          ) : (
            <ReactECharts option={chartOption} style={{ height: 320 }} />
          )}
        </Card>
      </Spin>
    </div>
  );
};

export default Dashboard;
