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
  }, []);

  const chartOption = useMemo(() => {
    const xs = usage.map((p) => p.timestamp);
    return {
      title: { text: '资源使用率（近 30 分钟）', left: 'center', textStyle: { fontSize: 14 } },
      tooltip: { trigger: 'axis', formatter: (p: any) => `${p[0].axisValue}<br/>${p.map((i: any) => `${i.marker}${i.seriesName}: ${i.value}%`).join('<br/>')}` },
      legend: { data: ['CPU 使用率', '内存使用率'], bottom: 0 },
      grid: { left: 50, right: 30, top: 50, bottom: 40 },
      xAxis: { type: 'category', data: xs, boundaryGap: false },
      yAxis: { type: 'value', min: 0, max: 100, axisLabel: { formatter: '{value}%' } },
      series: [
        {
          name: 'CPU 使用率',
          type: 'line',
          smooth: true,
          areaStyle: { opacity: 0.15 },
          itemStyle: { color: '#1668dc' },
          data: usage.map((p) => Number((p.cpu_utilization * 100).toFixed(1))),
        },
        {
          name: '内存使用率',
          type: 'line',
          smooth: true,
          areaStyle: { opacity: 0.15 },
          itemStyle: { color: '#52c41a' },
          data: usage.map((p) => Number((p.memory_utilization * 100).toFixed(1))),
        },
      ],
    };
  }, [usage]);

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>集群概览</h2>
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
        <Row gutter={[16, 16]}>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic
                title="集群总 CPU 核数"
                value={stats?.total_cpu_cores ?? 0}
                prefix={<ThunderboltOutlined />}
              />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic
                title="集群总内存"
                value={stats ? formatMemory(stats.total_memory_mb) : 0}
                prefix={<DatabaseOutlined />}
                suffix={stats ? `/ 可用 ${formatMemory(stats.available_memory_mb)}` : ''}
              />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic
                title="节点数"
                value={stats?.total_workers ?? 0}
                prefix={<ClusterOutlined />}
                suffix={stats ? `（在线 ${stats.ready_workers}）` : ''}
              />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic
                title="运行中模型实例"
                value={stats?.running_instances ?? 0}
                prefix={<PlayCircleOutlined />}
              />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic
                title="已注册模型数"
                value={stats?.total_models ?? 0}
                prefix={<ApiOutlined />}
              />
            </Card>
          </Col>
        </Row>

        <Card style={{ marginTop: 16 }}>
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
