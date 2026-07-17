import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import type { UploadProps } from 'antd';
import {
  DatabaseOutlined,
  DeleteOutlined,
  FileTextOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { api } from '../services/api';
import type {
  KnowledgeBase,
  KnowledgeDocument,
  KnowledgeSearchResult,
} from '../services/types';

const { Text, Paragraph } = Typography;

function formatSize(bytes: number): string {
  if (!bytes) return '-';
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

const KnowledgeBasePage: React.FC = () => {
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [selectedKb, setSelectedKb] = useState<KnowledgeBase | null>(null);
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<KnowledgeSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);

  const fetchKbs = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listKnowledgeBases();
      setKbs(list ?? []);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchKbs();
  }, [fetchKbs]);

  const fetchDocs = useCallback(async (kbId: number) => {
    setDocsLoading(true);
    try {
      const list = await api.listKnowledgeDocuments(kbId);
      setDocs(list ?? []);
    } catch {
      // ignore
    } finally {
      setDocsLoading(false);
    }
  }, []);

  const handleCreate = async () => {
    const values = await createForm.validateFields();
    try {
      await api.createKnowledgeBase({
        name: values.name,
        description: values.description,
        chunk_size: values.chunk_size,
        chunk_overlap: values.chunk_overlap,
      });
      message.success(`知识库 ${values.name} 已创建`);
      setCreateOpen(false);
      createForm.resetFields();
      fetchKbs();
    } catch {
      // ignore
    }
  };

  const handleDelete = async (id: number, name: string) => {
    try {
      await api.deleteKnowledgeBase(id);
      message.success(`知识库 ${name} 已删除`);
      if (selectedKb?.id === id) setSelectedKb(null);
      fetchKbs();
    } catch {
      // ignore
    }
  };

  const uploadProps: UploadProps = {
    accept: '.txt,.md,.markdown,.json,.csv,.log,.py,.js,.ts,.yaml,.yml,.html,.xml',
    showUploadList: false,
    customRequest: async (options) => {
      const { file, onSuccess, onError } = options;
      if (!selectedKb) return;
      try {
        const doc = await api.uploadKnowledgeDocument(selectedKb.id, file as File);
        message.success(`${(file as File).name} 已上传，正在切分索引`);
        onSuccess?.(doc, file);
        fetchDocs(selectedKb.id);
        fetchKbs();
      } catch (e) {
        onError?.(e as Error);
      }
    },
  };

  const handleSearch = async () => {
    if (!selectedKb || !searchQuery.trim()) return;
    setSearching(true);
    try {
      const results = await api.searchKnowledge(selectedKb.id, searchQuery, 5);
      setSearchResults(results ?? []);
    } catch {
      // ignore
    } finally {
      setSearching(false);
    }
  };

  const docStateColor: Record<string, string> = {
    ready: 'green',
    processing: 'processing',
    pending: 'default',
    error: 'red',
  };

  return (
    <div className="page-container">
      <div className="page-toolbar">
        <h2 style={{ margin: 0 }}>本地知识库</h2>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchKbs} loading={loading}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建知识库
          </Button>
        </Space>
      </div>

      <div style={{ display: 'flex', gap: 16, marginTop: 16 }}>
        <Card
          title="知识库列表"
          style={{ width: 320, flexShrink: 0 }}
          bodyStyle={{ padding: 0 }}
        >
          <Spin spinning={loading}>
            {kbs.length === 0 ? (
              <Empty description="暂无知识库" style={{ padding: 32 }} />
            ) : (
              <List
                dataSource={kbs}
                renderItem={(kb) => (
                  <List.Item
                    style={{
                      cursor: 'pointer',
                      background: selectedKb?.id === kb.id ? 'rgba(22,104,220,0.08)' : undefined,
                      padding: '12px 16px',
                    }}
                    onClick={() => {
                      setSelectedKb(kb);
                      setSearchResults(null);
                      setSearchQuery('');
                      fetchDocs(kb.id);
                    }}
                    actions={[
                      <Popconfirm
                        key="del"
                        title="确认删除该知识库？"
                        onConfirm={(e) => {
                          e?.stopPropagation();
                          handleDelete(kb.id, kb.name);
                        }}
                        onCancel={(e) => e?.stopPropagation()}
                      >
                        <Button danger size="small" icon={<DeleteOutlined />} onClick={(e) => e.stopPropagation()} />
                      </Popconfirm>,
                    ]}
                  >
                    <List.Item.Meta
                      avatar={<DatabaseOutlined style={{ fontSize: 20, color: '#1668dc' }} />}
                      title={<Text strong>{kb.name}</Text>}
                      description={
                        <Space size={4} wrap>
                          <Tag>{kb.doc_count} 文档</Tag>
                          <Tag>{kb.chunk_count} 分段</Tag>
                          {kb.description && (
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              {kb.description}
                            </Text>
                          )}
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
            )}
          </Spin>
        </Card>

        <Card style={{ flex: 1 }} title={selectedKb ? `知识库：${selectedKb.name}` : '详情'}>
          {!selectedKb ? (
            <Empty description="请从左侧选择知识库" style={{ padding: 60 }} />
          ) : (
            <>
              <Space style={{ marginBottom: 16 }} wrap>
                <Upload {...uploadProps}>
                  <Button type="primary" icon={<UploadOutlined />}>
                    上传文档
                  </Button>
                </Upload>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  支持 txt/md/json/csv 等文本文件，切分大小 {selectedKb.chunk_size} 字符
                </Text>
              </Space>

              <div style={{ marginBottom: 16 }}>
                <Space.Compact style={{ width: '100%' }}>
                  <Input
                    placeholder="输入问题，检索相关分段（BM25 关键词检索）"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    onPressEnter={handleSearch}
                    prefix={<SearchOutlined />}
                  />
                  <Button type="primary" onClick={handleSearch} loading={searching}>
                    检索
                  </Button>
                </Space.Compact>
              </div>

              {searchResults !== null && (
                <Card size="small" title={`检索结果（${searchResults.length} 条）`} style={{ marginBottom: 16 }}>
                  {searchResults.length === 0 ? (
                    <Empty description="无匹配分段" />
                  ) : (
                    <List
                      size="small"
                      dataSource={searchResults}
                      renderItem={(r) => (
                        <List.Item>
                          <div style={{ width: '100%' }}>
                            <Space size={8}>
                              <Tag color="blue">{r.filename}</Tag>
                              <Tag>#{r.chunk_index}</Tag>
                              <Text type="secondary">相关度 {r.score}</Text>
                            </Space>
                            <Paragraph
                              style={{ marginTop: 6, marginBottom: 0, fontSize: 13, color: 'rgba(0,0,0,0.75)' }}
                              ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
                            >
                              {r.content}
                            </Paragraph>
                          </div>
                        </List.Item>
                      )}
                    />
                  )}
                </Card>
              )}

              <Typography.Title level={5} style={{ marginTop: 0 }}>
                文档列表
              </Typography.Title>
              <Spin spinning={docsLoading}>
                {docs.length === 0 ? (
                  <Empty description="暂无文档，点击上方上传" />
                ) : (
                  <List
                    dataSource={docs}
                    renderItem={(d) => (
                      <List.Item
                        actions={[
                          <Popconfirm
                            key="del"
                            title="确认删除该文档？"
                            onConfirm={async () => {
                              try {
                                await api.deleteKnowledgeDocument(selectedKb.id, d.id);
                                message.success('文档已删除');
                                fetchDocs(selectedKb.id);
                                fetchKbs();
                              } catch {
                                // ignore
                              }
                            }}
                          >
                            <Button danger size="small" icon={<DeleteOutlined />} />
                          </Popconfirm>,
                        ]}
                      >
                        <List.Item.Meta
                          avatar={<FileTextOutlined style={{ fontSize: 18, color: '#8c8c8c' }} />}
                          title={d.filename}
                          description={
                            <Space size={8} wrap>
                              <Tag color={docStateColor[d.state] || 'default'}>{d.state}</Tag>
                              <Text type="secondary">{formatSize(d.file_size)}</Text>
                              <Text type="secondary">{d.chunk_count} 分段</Text>
                              <Text type="secondary">{d.char_count} 字符</Text>
                              {d.error_message && (
                                <Tooltip title={d.error_message}>
                                  <Text type="danger" style={{ fontSize: 12 }}>
                                    错误
                                  </Text>
                                </Tooltip>
                              )}
                            </Space>
                          }
                        />
                      </List.Item>
                    )}
                  />
                )}
              </Spin>
            </>
          )}
        </Card>
      </div>

      <Modal
        title="新建知识库"
        open={createOpen}
        onOk={handleCreate}
        onCancel={() => {
          setCreateOpen(false);
          createForm.resetFields();
        }}
        okText="创建"
        cancelText="取消"
      >
        <Form form={createForm} layout="vertical" initialValues={{ chunk_size: 512, chunk_overlap: 64 }}>
          <Form.Item label="名称" name="name" rules={[{ required: true, message: '请输入知识库名称' }]}>
            <Input placeholder="如：产品文档库" />
          </Form.Item>
          <Form.Item label="描述" name="description">
            <Input.TextArea rows={2} placeholder="可选" />
          </Form.Item>
          <Form.Item label="切分大小（字符）" name="chunk_size" rules={[{ required: true }]}>
            <InputNumber min={64} max={4096} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="切分重叠（字符）" name="chunk_overlap" rules={[{ required: true }]}>
            <InputNumber min={0} max={512} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default KnowledgeBasePage;
