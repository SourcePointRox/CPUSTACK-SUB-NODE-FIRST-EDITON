import React, { useMemo, useState } from 'react';
import { ProLayout } from '@ant-design/pro-components';
import type { MenuDataItem, ProLayoutProps } from '@ant-design/pro-components';
import { Dropdown, Switch, Tooltip, message } from 'antd';
import {
  AppstoreOutlined,
  BarChartOutlined,
  BookOutlined,
  ClusterOutlined,
  ControlOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  KeyOutlined,
  LogoutOutlined,
  MessageOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../store/auth';
import { ConfigProvider as AntConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';

type MenuItem = MenuDataItem & { path: string };

const menuRoutes: MenuItem[] = [
  { path: '/dashboard', name: '概览', icon: <DashboardOutlined /> },
  { path: '/workers', name: '节点', icon: <ClusterOutlined /> },
  { path: '/models', name: '模型', icon: <AppstoreOutlined /> },
  { path: '/instances', name: '模型实例', icon: <ExperimentOutlined /> },
  { path: '/playground', name: '对话测试', icon: <MessageOutlined /> },
  { path: '/api-keys', name: 'API 密钥', icon: <KeyOutlined /> },
  { path: '/usage', name: '用量', icon: <BarChartOutlined /> },
  { path: '/knowledge', name: '知识库', icon: <BookOutlined /> },
  { path: '/settings', name: '设置', icon: <SettingOutlined /> },
];

const BasicLayout: React.FC = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [darkMode, setDarkMode] = useState<boolean>(false);

  const handleLogout = async () => {
    await logout();
    message.success('已退出登录');
    navigate('/login', { replace: true });
  };

  const userMenu = {
    items: [
      {
        key: 'profile',
        label: (
          <span>
            <UserOutlined /> {user?.username || '用户'}
            {user?.is_admin ? '（管理员）' : ''}
          </span>
        ),
        disabled: true,
      },
      { type: 'divider' as const },
      {
        key: 'logout',
        label: (
          <span onClick={handleLogout}>
            <LogoutOutlined /> 退出登录
          </span>
        ),
      },
    ],
  };

  const layoutProps: ProLayoutProps = useMemo(
    () => ({
      title: 'CPUSTACK',
      logo: '/cpu.svg',
      layout: 'mix',
      fixedHeader: true,
      fixSiderbar: true,
      contentWidth: 'Fluid',
      navTheme: darkMode ? 'realDark' : 'light',
      headerTheme: darkMode ? 'realDark' : 'light',
      menu: { request: async () => menuRoutes },
      location: { pathname: location.pathname },
      menuItemRender: (item, defaultDom) =>
        item.path ? <Link to={item.path}>{defaultDom}</Link> : defaultDom,
      avatarProps: {
        icon: <UserOutlined />,
        size: 'small',
        render: (_, dom) => (
          <Dropdown menu={userMenu} placement="bottomRight">
            {dom}
          </Dropdown>
        ),
      },
      actionsRender: () => [
        <Tooltip title="主题切换" key="theme">
          <Switch
            checked={darkMode}
            onChange={setDarkMode}
            checkedChildren="夜"
            unCheckedChildren="日"
          />
        </Tooltip>,
        <Tooltip title="设置" key="settings">
          <Link to="/settings">
            <ControlOutlined />
          </Link>
        </Tooltip>,
      ],
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [darkMode, location.pathname, user],
  );

  return (
    <AntConfigProvider
      locale={zhCN}
      theme={{
        algorithm: darkMode ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: { colorPrimary: '#1668dc' },
      }}
    >
      <ProLayout {...layoutProps}>
        <Outlet />
      </ProLayout>
    </AntConfigProvider>
  );
};

export default BasicLayout;
