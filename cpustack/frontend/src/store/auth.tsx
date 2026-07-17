import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api, clearToken, getToken, getStoredUser, setStoredUser, setToken } from '../services/api';
import type { LoginPayload, User } from '../services/types';

interface AuthContextValue {
  user: User | null;
  token: string | null;
  isAuthed: boolean;
  login: (payload: LoginPayload) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(() => getStoredUser());
  const [token, setTokenState] = useState<string | null>(() => getToken());

  // 启动时若已有 token，尝试拉取一次用户信息以校验有效性
  useEffect(() => {
    if (token && !user) {
      api
        .getCurrentUser()
        .then((u) => {
          setUser(u);
          setStoredUser(u);
        })
        .catch(() => {
          clearToken();
          setTokenState(null);
          setUser(null);
        });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = useCallback(async (payload: LoginPayload) => {
    const result = await api.login(payload);
    const jwt = result.access_token;
    setToken(jwt);
    setTokenState(jwt);
    if (result.user) {
      setUser(result.user);
      setStoredUser(result.user);
    } else {
      try {
        const u = await api.getCurrentUser();
        setUser(u);
        setStoredUser(u);
      } catch {
        // ignore - token may still be valid for other endpoints
      }
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // 忽略后端错误，本地依然登出
    }
    clearToken();
    setTokenState(null);
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, token, isAuthed: !!token, login, logout }),
    [user, token, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth 必须在 AuthProvider 内使用');
  }
  return ctx;
}
