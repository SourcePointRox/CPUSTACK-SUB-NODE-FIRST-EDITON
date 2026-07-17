import React from 'react';
import { Card, Descriptions, Typography } from 'antd';
import { useAuth } from '../store/auth';

const { Title } = Typography;

const Settings: React.FC = () => {
  const { user } = useAuth();

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>设置</h2>
      </div>

      <Card title="账户信息" style={{ marginBottom: 16 }}>
        <Descriptions column={1}>
          <Descriptions.Item label="用户名">{user?.username ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="角色">{user?.is_admin ? '管理员' : '普通用户'}</Descriptions.Item>
          <Descriptions.Item label="状态">{user?.enabled ? '启用' : '禁用'}</Descriptions.Item>
          <Descriptions.Item label="用户 ID">{user?.id ?? '-'}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="API 端点" style={{ marginBottom: 16 }}>
        <Descriptions column={1}>
          <Descriptions.Item label="管理 API 基础路径">/v2</Descriptions.Item>
          <Descriptions.Item label="推理 OpenAI 兼容路径">/v1</Descriptions.Item>
          <Descriptions.Item label="登录接口">POST /v2/auth/login</Descriptions.Item>
          <Descriptions.Item label="对话补全（流式）">POST /v1/chat/completions</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="前端版本">
        <Title level={5} style={{ marginTop: 0 }}>
          CPUSTACK Frontend v0.1.0
        </Title>
        <p style={{ color: 'rgba(0,0,0,0.65)', marginBottom: 0 }}>
          React 18 + TypeScript + Vite + Ant Design v5
        </p>
      </Card>
    </div>
  );
};

export default Settings;
