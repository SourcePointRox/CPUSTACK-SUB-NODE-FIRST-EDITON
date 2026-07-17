import React, { useState } from 'react';
import { Button, Card, Form, Input, Typography } from 'antd';
import { LockOutlined, UserOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../store/auth';
import type { LoginPayload } from '../services/types';

const { Title, Text } = Typography;

const Login: React.FC = () => {
  const navigate = useNavigate();
  const { login } = useAuth();
  const [loading, setLoading] = useState(false);

  const onFinish = async (values: LoginPayload) => {
    setLoading(true);
    try {
      await login(values);
      navigate('/dashboard', { replace: true });
    } catch {
      // 错误已在拦截器中提示
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-wrapper">
      <Card className="login-card" bordered={false}>
        <div className="login-logo">
          <ThunderboltOutlined />
        </div>
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <Title level={3} style={{ marginBottom: 4 }}>
            CPUSTACK 控制台
          </Title>
          <Text type="secondary">分布式 CPU AI 推理平台</Text>
        </div>
        <Form<LoginPayload>
          layout="vertical"
          onFinish={onFinish}
          autoComplete="off"
          initialValues={{ username: '', password: '' }}
        >
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="请输入用户名"
              size="large"
            />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="请输入密码"
              size="large"
            />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button
              type="primary"
              htmlType="submit"
              size="large"
              block
              loading={loading}
              style={{ height: 44, borderRadius: 8, fontWeight: 500 }}
            >
              登录
            </Button>
          </Form.Item>
        </Form>
        <div style={{ marginTop: 20, textAlign: 'center' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            调用接口：POST /v2/auth/login
          </Text>
        </div>
      </Card>
    </div>
  );
};

export default Login;
