import React from 'react';
import { Tag } from 'antd';

export type StatusKind =
  | 'worker'
  | 'instance'
  | 'model'
  | 'api_key'
  | 'generic';

interface StatusTagProps {
  status?: string | null;
  kind?: StatusKind;
  emptyText?: string;
}

interface StatusMeta {
  text: string;
  color: string;
}

const GENERIC_MAP: Record<string, StatusMeta> = {
  running: { text: '运行中', color: 'green' },
  ready: { text: '就绪', color: 'green' },
  online: { text: '在线', color: 'green' },
  enabled: { text: '启用', color: 'green' },

  pending: { text: '等待中', color: 'orange' },
  analyzing: { text: '分析中', color: 'orange' },
  scheduled: { text: '已调度', color: 'blue' },
  initializing: { text: '初始化', color: 'blue' },
  downloading: { text: '下载中', color: 'blue' },
  starting: { text: '启动中', color: 'blue' },
  not_ready: { text: '未就绪', color: 'orange' },
  disabled: { text: '已禁用', color: 'default' },
  stopped: { text: '已停止', color: 'default' },

  error: { text: '错误', color: 'red' },
  failed: { text: '失败', color: 'red' },

  unreachable: { text: '不可达', color: 'default' },
  offline: { text: '离线', color: 'default' },
  unknown: { text: '未知', color: 'default' },
};

function resolveMeta(status: string, kind: StatusKind): StatusMeta {
  const key = String(status).toLowerCase();
  const found = GENERIC_MAP[key];
  if (found) {
    return found;
  }
  // 未知状态：根据 kind 提供语义化颜色
  const colorByKind: Record<StatusKind, string> = {
    worker: 'blue',
    instance: 'purple',
    model: 'cyan',
    api_key: 'geekblue',
    generic: 'default',
  };
  return { text: status, color: colorByKind[kind] ?? 'default' };
}

const StatusTag: React.FC<StatusTagProps> = ({
  status,
  kind = 'generic',
  emptyText = '-',
}) => {
  if (!status) {
    return <span style={{ color: 'rgba(0,0,0,0.45)' }}>{emptyText}</span>;
  }
  const meta = resolveMeta(status, kind);
  return <Tag color={meta.color}>{meta.text}</Tag>;
};

export default StatusTag;
