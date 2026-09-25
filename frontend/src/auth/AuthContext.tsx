import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "../api/client";
import { clearToken, getToken, setToken } from "../api/token";
import type { AuthResponse, LoginRequest, RegisterRequest, User } from "../api/types";

type OAuthProvider = "github" | "google";

interface AuthContextValue {
  user: User | null;
  isLoading: boolean;
  loginWithPassword: (data: LoginRequest) => Promise<void>;
  register: (data: RegisterRequest) => Promise<void>;
  startOAuth: (provider: OAuthProvider) => void;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const loadMe = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setIsLoading(false);
      return;
    }
    try {
      const data = await api.get<{ user: User }>("/auth/me");
      setUser(data.user);
    } catch {
      clearToken();
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadMe();
  }, [loadMe]);

  const loginWithPassword = useCallback(async (data: LoginRequest) => {
    const res = await api.post<AuthResponse>("/auth/login", data, { skipAuth: true });
    setToken(res.token);
    setUser(res.user);
  }, []);

  const register = useCallback(async (data: RegisterRequest) => {
    const res = await api.post<AuthResponse>("/auth/register", data, { skipAuth: true });
    setToken(res.token);
    setUser(res.user);
  }, []);

  const startOAuth = useCallback((provider: OAuthProvider) => {
    window.location.href = `${API_URL}/api/auth/${provider}/login`;
  }, []);

  const logout = useCallback(async () => {
    try {
      if (getToken()) {
        await api.post("/auth/logout");
      }
    } catch (err) {
      if (!(err instanceof ApiError)) {
        throw err;
      }
    } finally {
      clearToken();
      setUser(null);
    }
  }, []);

  return (
    <AuthContext.Provider value={{ user, isLoading, loginWithPassword, register, startOAuth, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
