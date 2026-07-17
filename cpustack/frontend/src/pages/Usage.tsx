import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Empty,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd';
import { ReloadOutlined, ApiOutlined, ThunderboltOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import ReactECharts from 'echarts-for-react';
import { api } from '../services/api';
import type { TokenTotalUsage, TokenUsageSummary } from '../services/types';

const { Text } = Typography;

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

const Usage: React.FC = () => {
  const [summary, setSummary] = useState<TokenUsageSummary[]>([]);
  const [total, setTotal] = useState<TokenTotalUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [days, setDays] = useState<number>(7);
  const [modelFilter, setModelFilter] = useState<string | undefined>(undefined);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [s, t] = await Promise.all([
        api.getTokenUsageSummary(modelFilter, days),
        api.getTokenTotalUsage(),
      ]);
      setSummary(s ?? []);
      setTotal(t);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [days, modelFilter]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const chartOption = useMemo(() => {
    // 汇总每模型每天的 total_tokens
    const byDate: Record<string, number> = {};
    summary.forEach((m) => {
      m.daily.forEach((d) => {
        byDate[d.date] = (byDate[d.date] || 0) + d.total_tokens;
      });
    });
    const dates = Object.keys(byDate).sort();
    return {
      title: { text: `近 ${days} 天集群 Token 用量趋势`, left: 'center', textStyle: { fontSize: 14 } },
      tooltip: { trigger: 'axis' },
      grid: { left: 60, right: 30, top: 50, bottom: 40 },
      xAxis: { type: 'category', data: dates, boundaryGap: false },
      yAxis: { type: 'value', axisLabel: { formatter: (v: number) => formatTokens(v) } },
      series: [
        {
          name: '总 Token',
          type: 'line',
          smooth: true,
          areaStyle: { opacity: 0.2 },
          itemStyle: { color: '#1668dc' },
          data: dates.map((d) => byDate[d]),
        },
      ],
    };
  }, [summary, days]);

  const columns: ColumnsType<TokenUsageSummary> = [
    {
      title: '模型',
      dataIndex: 'model_name',
      key: 'model_name',
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    { title: '请求次数', dataIndex: 'request_count', key: 'request_count', sorter: (a, b) => a.request_count - b.request_count },
    { title: '输入 Tokens', dataIndex: 'prompt_tokens', key: 'prompt_tokens', render: (v: number) => formatTokens(v), sorter: (a, b) => a.prompt_tokens - b.prompt_tokens },
    { title: '输出 Tokens', dataIndex: 'completion_tokens', key: 'completion_tokens', render: (v: number) => formatTokens(v), sorter: (a, b) => a.completion_tokens - b.completion_tokens },
    { title: '总 Tokens', dataIndex: 'total_tokens', key: 'total_tokens', render: (v: number) => <Text strong>{formatTokens(v)}</Text>, sorter: (a, b) => a.total_tokens - b.total_tokens, defaultSortOrder: 'descend' },
  ];

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>Token 用量</h2>
        <Space>
          <Segmented
            value={days}
            onChange={(v) => setDays(Number(v))}
            options={[
              { label: '今天', value: 1 },
              { label: '近 7 天', value: 7 },
              { label: '近 30 天', value: 30 },
            ]}
          />
          <Select
            allowClear
            placeholder="按模型过滤"
            style={{ width: 200 }}
            value={modelFilter}
            onChange={setModelFilter}
            options={summary.map((s) => ({ value: s.model_name, label: s.model_name }))}
          />
          <Button icon={<ReloadOutlined />} onClick={fetchData} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      <Spin spinning={loading && !total}>
        <Row gutter={[16, 16]}>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic title="累计总 Tokens" value={total ? formatTokens(total.total_tokens) : 0} prefix={<ThunderboltOutlined />} />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic title="累计请求次数" value={total?.request_count ?? 0} prefix={<ApiOutlined />} />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic title="输入 Tokens" value={total ? formatTokens(total.prompt_tokens) : 0} />
            </Card>
          </Col>
          <Col xs={24} sm={12} md={8} lg={6}>
            <Card>
              <Statistic title="输出 Tokens" value={total ? formatTokens(total.completion_tokens) : 0} />
            </Card>
          </Col>
        </Row>

        <Card style={{ marginTop: 16 }}>
          {summary.length === 0 ? (
            <Empty description="暂无 Token 用量数据，发起对话后即可统计" style={{ padding: '40px 0' }} />
          ) : (
            <ReactECharts option={chartOption} style={{ height: 320 }} />
          )}
        </Card>

        <Card title="每模型用量明细" style={{ marginTop: 16 }}>
          <Table<TokenUsageSummary>
            rowKey="model_name"
            columns={columns}
            dataSource={summary}
            pagination={false}
            size="middle"
          />
        </Card>
      </Spin>
    </div>
  );
};

export default Usage;
