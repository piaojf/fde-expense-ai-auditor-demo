import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { portalHttp, setStorageUser, type User } from '../api/enterprise';

interface AuthState {
  user: User | null; loading: boolean;
  login: (company_id: string, username: string, password: string, captcha_id: string, captcha_code: string) => Promise<void>;
  logout: () => Promise<void>;
}
const AuthContext = createContext<AuthState>({ user: null, loading: false,
  login: async () => {}, logout: async () => {} });
export function useAuth() { return useContext(AuthContext); }
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    const expired = () => { setStorageUser(null); setUser(null); };
    window.addEventListener('enterprise-auth-expired', expired);
    portalHttp<{ user: User }>('/auth/me').then(data => {
      if (active) { setStorageUser(data.user); setUser(data.user); }
    }).catch(() => { if (active) { setStorageUser(null); setUser(null); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; window.removeEventListener('enterprise-auth-expired', expired); };
  }, []);
  async function login(company_id: string, username: string, password: string, captcha_id: string, captcha_code: string) {
    const data = await portalHttp<{ user: User }>('/auth/login', { company_id, username, password, captcha_id, captcha_code });
    setStorageUser(data.user); setUser(data.user);
  }
  async function logout() {
    await portalHttp('/auth/logout', {});
    // Only this application's owned form drafts; no browser-wide storage clearing.
    Object.keys(sessionStorage).filter(key => key.startsWith('ep-')).forEach(key => sessionStorage.removeItem(key));
    setStorageUser(null); setUser(null);
  }
  return <AuthContext.Provider value={{ user, loading, login, logout }}>{children}</AuthContext.Provider>;
}
