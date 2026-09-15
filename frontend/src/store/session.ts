import { defineStore } from 'pinia'
import { login, setToken, clearToken, TOKEN_KEY, USER_KEY, ROLE_KEY } from '../api'

export const useSessionStore = defineStore('session', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: localStorage.getItem(USER_KEY) || '',
    role: localStorage.getItem(ROLE_KEY) || '',
    version: '' as string,
    expiresIn: 0 as number,
  }),
  getters: {
    isAuthed: (s) => !!s.token,
    isAdmin: (s) => s.role === 'admin',
  },
  actions: {
    async login(username: string, password: string) {
      const j = await login(username, password)
      this.token = j.access_token
      this.user = j.user || ''
      this.role = j.role || ''
      this.version = j.gateway_version || ''
      this.expiresIn = j.expires_in || 0
      setToken(this.token, this.user, this.role)
    },
    async logout() {
      try {
        if (this.token) await fetch('/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${this.token}` } })
      } catch { /* 忽略 */ }
      this.token = ''
      this.user = ''
      this.role = ''
      clearToken()
    },
  },
})