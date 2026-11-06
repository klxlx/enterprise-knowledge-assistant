<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'

const apiBase = `${import.meta.env.BASE_URL}api`
const GREETING = { role: 'assistant', content: '你好，我是公司内部助手，有什么问题可以问我。' }
const TOOL_LABELS = {
  search_knowledge: '正在检索知识库…',
  get_weather: '正在查询实时天气…',
  escalate_to_human: '正在转接人工…',
}
// 审计日志里的动作代号翻成人话
const AUDIT_LABELS = {
  'auth.login': '登录',
  'auth.logout': '退出登录',
  'knowledge.upload': '更新知识库',
  'session.delete': '删除会话',
  'feedback.submit': '提交反馈',
  'handoff.created': '转人工',
  'employee.create': '新增员工',
  'employee.delete': '删除员工',
  'dingtalk.download': '下载钉盘文件',
}

const modelReady = ref(false)
const serviceStatus = ref('检查服务中…')

const messages = ref([{ ...GREETING }])
const input = ref('')
const loading = ref(false)
const uploadMessage = ref('')
const uploadError = ref('')
const messagesEl = ref(null)

const sessions = ref([])
const scopes = ref([])
const employees = ref([])
const employeeDepartments = ref([])
const employeeKeyword = ref('')
const employeeDepartment = ref('')
const employeeModalOpen = ref(false)
const employeeForm = ref(emptyEmployee())
const employeeError = ref('')
const employeeMessage = ref('')
const savingEmployee = ref(false)
const fileLibraryOpen = ref(false)
const libraryFiles = ref([])
const libraryLoading = ref(false)
const libraryError = ref('')
const libraryMessage = ref('')
const importingFileId = ref('')
let employeeTimer = null
const uploadScope = ref('')
const pendingFile = ref(null)
const confirmScope = ref('')
const confirmDepartment = ref('')
const confirmVisibility = ref('department')
// 时效留空表示「今天生效、长期有效」；填了失效日期，到期后这份资料会自动退出检索
const confirmEffectiveFrom = ref('')
const confirmExpiresAt = ref('')
const uploading = ref(false)
const sessionId = ref(localStorage.getItem('rag-session-id') || `user_${Date.now()}`)
localStorage.setItem('rag-session-id', sessionId.value)

function emptyEmployee() {
  return {
    name: '', department: '', title: '', employee_type: '员工', responsibilities: '',
    contact: '', keywords: '', scope: '', visibleToNewcomers: true,
  }
}

// 会话按账号隔离：ID 里带用户名，换成别人登录时不会撞到同一个会话
function newSessionId() {
  const who = currentUser.value?.username || 'guest'
  return `user_${who}_${Date.now()}`
}

const token = ref(localStorage.getItem('rag-token') || '')
const currentUser = ref(null)
const loginOpen = ref(false)
const loginForm = ref({ username: '', password: '' })
const loginError = ref('')

const isStaff = computed(() => currentUser.value?.role === 'staff')
const isHR = computed(() => isStaff.value && currentUser.value?.department === '人力资源部')
const roleLabel = computed(() => (isStaff.value ? '部门主管' : '新人'))

const uploadVisibilityOptions = computed(() => {
  if (isHR.value) {
    return [
      { value: 'all', label: '全员公开' },
      { value: 'department', label: '本部门可见' },
      { value: 'staff', label: '仅主管可见' },
      { value: 'sensitive', label: '敏感资料（仅人事，不进问答）' },
    ]
  }
  if (isStaff.value) {
    return [
      { value: 'all', label: '全员公开' },
      { value: 'department', label: '本部门可见' },
      { value: 'staff', label: '仅主管可见' },
    ]
  }
  return [
    { value: 'all', label: '全员公开' },
    { value: 'department', label: '本部门可见' },
  ]
})

// 侧栏分区：会话 / 通讯录 / 知识库 / 盲区 / 审计
const activeTab = ref('sessions')
const blindSpots = ref([])
const blindSpotDays = ref(30)
const blindSpotError = ref('')
const auditLogs = ref([])
const auditError = ref('')

const sideTabs = computed(() => {
  const tabs = [
    { key: 'sessions', label: '会话' },
    { key: 'directory', label: '通讯录' },
    { key: 'knowledge', label: '知识库' },
  ]
  if (isStaff.value) {
    tabs.push({ key: 'blindspots', label: '盲区' })
    tabs.push({ key: 'audit', label: '审计' })
  }
  return tabs
})

function authHeaders(extra = {}) {
  return token.value
    ? { ...extra, Authorization: `Bearer ${token.value}` }
    : { ...extra }
}

// 从文件名里猜适用范围，用来提醒"文件名写着 X5，标签却填了 T30"这类贴错
const SCOPE_HINT = /\b(?:DEEBOT\s+[A-Z0-9]{1,6}(?:\s+[A-Z0-9]{1,6}){0,2}|[TX]\d{1,2}(?:\s+(?:MAX|PRO|OMNI|TURBO))?)\b/i

const scopeHint = computed(() => {
  if (!pendingFile.value) return ''
  const name = pendingFile.value.name.replace(/\.[^.]+$/, '')
  const match = SCOPE_HINT.exec(name)
  return match ? match[0].trim() : ''
})

const scopeMismatch = computed(() => {
  const entered = confirmScope.value.trim()
  if (!scopeHint.value || !entered) return false
  const normalize = (text) => text.toLowerCase().replace(/[\s,，、]/g, '')
  const hint = normalize(scopeHint.value)
  const value = normalize(entered)
  return !(value.includes(hint) || hint.includes(value))
})

const NO_EXPIRY_DATE = '9999-12-31'

// 只有真正设了失效期的资料才显示时效标签，长期有效的资料不加噪声
function sourceValidity(source) {
  if (!source || !source.expires_at || source.expires_at === NO_EXPIRY_DATE) return ''
  const today = new Date().toISOString().slice(0, 10)
  return source.expires_at < today
    ? `资料已过期（${source.expires_at}）`
    : `有效期至 ${source.expires_at}`
}

function isExpiredSource(source) {
  return Boolean(source?.expires_at) && source.expires_at !== NO_EXPIRY_DATE
    && source.expires_at < new Date().toISOString().slice(0, 10)
}

const FILE_VISIBILITY_LABELS = {
  all: '全员公开',
  department: '本部门',
  staff: '仅主管',
  sensitive: '敏感',
}

function fileVisibilityLabel(value) {
  return FILE_VISIBILITY_LABELS[value] || value || ''
}

onMounted(async () => {
  try {
    const response = await fetch(`${apiBase}/health`)
    if (!response.ok) throw new Error('health check failed')
    const payload = await response.json()
    modelReady.value = payload.model_configured === true
    serviceStatus.value = modelReady.value ? '在线' : '模型待配置'
  } catch {
    serviceStatus.value = '服务暂不可用'
  }
  await loadCurrentUser()
  await loadSessions()
  await loadScopes()
  await loadEmployees()
  await restoreActiveSession()
  // 当前标签如果是需要拉数据的（盲区/审计），进页面就把数据准备好
  await selectTab(activeTab.value)
  if (fileLibraryOpen.value && currentUser.value) await loadLibraryFiles()
})

function formatTime(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (number) => String(number).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

async function scrollToBottom() {
  await nextTick()
  if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
}

async function loadCurrentUser() {
  if (!token.value) {
    currentUser.value = null
    return
  }
  try {
    const response = await fetch(`${apiBase}/auth/me`, { headers: authHeaders() })
    if (!response.ok) return
    const payload = await response.json()
    currentUser.value = payload.user
    if (!payload.user) {
      token.value = ''
      localStorage.removeItem('rag-token')
    }
  } catch {
    // 拿不到用户信息就按未登录处理
  }
}

async function submitLogin() {
  loginError.value = ''
  try {
    const response = await fetch(`${apiBase}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(loginForm.value),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '登录失败')
    token.value = payload.token
    currentUser.value = payload.user
    localStorage.setItem('rag-token', payload.token)
    loginOpen.value = false
    loginForm.value = { username: '', password: '' }
    // 换账号就换会话，避免看到别人（或匿名时）的历史
    startNewSession()
    await loadSessions()
  } catch (error) {
    loginError.value = error.message
  }
}

async function logout() {
  try {
    await fetch(`${apiBase}/auth/logout`, { method: 'POST', headers: authHeaders() })
  } catch {
    // 退出失败也让前端回到未登录状态
  }
  token.value = ''
  currentUser.value = null
  localStorage.removeItem('rag-token')
  // 退出后回到匿名会话，员工专属标签也不能再停留
  startNewSession()
  if (['blindspots', 'audit'].includes(activeTab.value)) {
    activeTab.value = 'sessions'
  }
  await loadSessions()
}

async function loadSessions() {
  try {
    const response = await fetch(`${apiBase}/sessions`, { headers: authHeaders() })
    if (!response.ok) return
    const payload = await response.json()
    sessions.value = payload.sessions || []
  } catch {
    // 会话列表加载失败不影响聊天本身
  }
}

async function loadScopes() {
  try {
    const response = await fetch(`${apiBase}/scopes`, { headers: authHeaders() })
    if (!response.ok) return
    const payload = await response.json()
    scopes.value = payload.scopes || []
  } catch {
    // 适用范围列表拿不到不影响上传，用户可以手填
  }
}

async function loadEmployees() {
  try {
    const params = new URLSearchParams()
    const keyword = employeeKeyword.value.trim()
    if (keyword) params.set('topic', keyword)
    else if (employeeDepartment.value) params.set('department', employeeDepartment.value)
    const query = params.toString()
    const response = await fetch(`${apiBase}/employees${query ? `?${query}` : ''}`, {
      headers: authHeaders(),
    })
    if (!response.ok) return
    const payload = await response.json()
    employees.value = payload.employees || []
    employeeDepartments.value = payload.departments || []
  } catch {
    // 通讯录拿不到不影响提问
  }
}

function onEmployeeSearch() {
  clearTimeout(employeeTimer)
  employeeTimer = setTimeout(loadEmployees, 300)
}

function openEmployeeModal() {
  employeeForm.value = emptyEmployee()
  employeeError.value = ''
  employeeModalOpen.value = true
}

async function submitEmployee() {
  if (savingEmployee.value) return
  employeeError.value = ''
  const form = employeeForm.value
  if (!form.name.trim() || !form.department.trim()) {
    employeeError.value = '姓名和部门不能为空'
    return
  }
  savingEmployee.value = true
  try {
    const response = await fetch(`${apiBase}/employees`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        name: form.name.trim(),
        department: form.department.trim(),
        title: form.title.trim(),
        employee_type: form.employee_type,
        responsibilities: form.responsibilities.trim(),
        contact: form.contact.trim(),
        keywords: form.keywords.trim(),
        scope: form.scope.trim(),
        visibility: form.visibleToNewcomers ? 'all' : 'staff',
      }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '保存失败')
    employeeModalOpen.value = false
    employeeMessage.value = `已添加 ${payload.employee.name}（${payload.employee.department}）`
    await loadEmployees()
  } catch (error) {
    employeeError.value = error.message
  } finally {
    savingEmployee.value = false
  }
}

async function removeEmployee(person) {
  if (!window.confirm(`删除「${person.name}」的员工卡片？删除后新人将查不到这张卡片。`)) return
  employeeError.value = ''
  try {
    const response = await fetch(
      `${apiBase}/employees/${encodeURIComponent(person.employee_id)}`,
      { method: 'DELETE', headers: authHeaders() },
    )
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '删除失败')
    employeeMessage.value = payload.referenced_by?.length
      ? `已删除 ${person.name}；这些流程需要重新指派负责人：${payload.referenced_by.join('、')}`
      : `已删除 ${person.name}`
    await loadEmployees()
  } catch (error) {
    employeeError.value = error.message
  }
}

async function downloadFile(file, message = null) {
  if (!currentUser.value) {
    loginOpen.value = true
    return
  }
  if (message) message.fileError = ''
  libraryError.value = ''
  try {
    const response = await fetch(
      `${apiBase}/dingtalk/files/${encodeURIComponent(file.file_id)}/download`,
      { headers: authHeaders() },
    )
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || `下载失败（${response.status}）`)
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = file.name
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  } catch (error) {
    if (message) message.fileError = error.message
    else libraryError.value = error.message
  }
}

function openFileLibrary() {
  if (!currentUser.value) {
    loginOpen.value = true
    return
  }
  fileLibraryOpen.value = true
  libraryMessage.value = ''
  loadLibraryFiles()
}

async function loadLibraryFiles() {
  libraryLoading.value = true
  libraryError.value = ''
  try {
    const response = await fetch(`${apiBase}/files`, { headers: authHeaders() })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || `读取文件失败（${response.status}）`)
    libraryFiles.value = payload.files || []
  } catch (error) {
    libraryError.value = error.message
  } finally {
    libraryLoading.value = false
  }
}

async function importLibraryFile(file) {
  if (importingFileId.value) return
  importingFileId.value = file.file_id
  libraryError.value = ''
  libraryMessage.value = ''
  try {
    const response = await fetch(
      `${apiBase}/files/${encodeURIComponent(file.file_id)}/import`,
      { method: 'POST', headers: authHeaders() },
    )
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '导入失败')
    libraryMessage.value = payload.message
    await loadLibraryFiles()
    await loadScopes()
  } catch (error) {
    libraryError.value = error.message
  } finally {
    importingFileId.value = ''
  }
}

async function loadBlindSpots() {
  blindSpotError.value = ''
  try {
    const response = await fetch(`${apiBase}/reviews/blind-spots?days=${blindSpotDays.value}`, {
      headers: authHeaders(),
    })
    if (!response.ok) throw new Error(`读取盲区清单失败（${response.status}）`)
    const payload = await response.json()
    blindSpots.value = payload.blind_spots || []
  } catch (error) {
    blindSpotError.value = error.message
  }
}

async function loadAudit() {
  auditError.value = ''
  try {
    const response = await fetch(`${apiBase}/audit?limit=50`, { headers: authHeaders() })
    if (!response.ok) throw new Error(`读取审计日志失败（${response.status}）`)
    const payload = await response.json()
    auditLogs.value = payload.logs || []
  } catch (error) {
    auditError.value = error.message
  }
}

async function selectTab(key) {
  activeTab.value = key
  if (key === 'blindspots' && isStaff.value) await loadBlindSpots()
  if (key === 'audit' && isStaff.value) await loadAudit()
}

function auditLabel(action) {
  return AUDIT_LABELS[action] || action
}

// 历史会话里的回答也要能点赞点踩：把后端给的 turn_id / feedback 转成前端字段
function restoreMessages(list) {
  return (list || []).map((item) => ({
    ...item,
    turnId: item.turn_id,
    feedback: item.feedback ?? null,
    feedbackSent: item.feedback !== null && item.feedback !== undefined,
    files: item.files || [],
  }))
}

function toggleFeedbackComment(message) {
  message.feedbackOpen = !message.feedbackOpen
  if (!message.feedbackOpen) message.feedbackComment = ''
}

async function sendFeedback(message, rating) {
  if (!message.turnId || message.feedbackSent) return
  if (rating === -1 && !message.feedbackOpen) {
    // 差评先展开输入框，让用户补一句原因（可留空直接提交）
    message.feedbackOpen = true
    return
  }
  try {
    const response = await fetch(`${apiBase}/feedback`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        turn_id: message.turnId,
        rating,
        comment: (message.feedbackComment || '').trim(),
      }),
    })
    if (!response.ok) throw new Error(`反馈提交失败（${response.status}）`)
    message.feedback = rating
    message.feedbackSent = true
    message.feedbackOpen = false
  } catch (error) {
    uploadError.value = error.message
  }
}

async function openSession(id) {
  if (loading.value) return
  uploadError.value = ''
  try {
    const response = await fetch(`${apiBase}/sessions/${encodeURIComponent(id)}`, {
      headers: authHeaders(),
    })
    if (!response.ok) throw new Error(`读取会话失败（${response.status}）`)
    const payload = await response.json()
    messages.value = [{ ...GREETING }, ...restoreMessages(payload.messages)]
    sessionId.value = id
    localStorage.setItem('rag-session-id', id)
    await scrollToBottom()
  } catch (error) {
    uploadError.value = error.message
  }
}

async function restoreActiveSession() {
  try {
    const response = await fetch(`${apiBase}/sessions/${encodeURIComponent(sessionId.value)}`, {
      headers: authHeaders(),
    })
    if (response.status === 403) {
      // 本地存的会话属于别的账号：换一个自己的新会话
      sessionId.value = newSessionId()
      localStorage.setItem('rag-session-id', sessionId.value)
      return
    }
    if (!response.ok) return
    const payload = await response.json()
    if (payload.messages?.length) {
      messages.value = [{ ...GREETING }, ...restoreMessages(payload.messages)]
      await scrollToBottom()
    }
  } catch {
    // 没有历史就保持空白会话
  }
}

function startNewSession() {
  if (loading.value) return
  sessionId.value = newSessionId()
  localStorage.setItem('rag-session-id', sessionId.value)
  messages.value = [{ ...GREETING }]
  uploadMessage.value = ''
  uploadError.value = ''
}

async function removeSession(id) {
  if (!window.confirm('删除这个会话？删除后无法恢复。')) return
  try {
    const response = await fetch(`${apiBase}/sessions/${encodeURIComponent(id)}`, {
      method: 'DELETE',
      headers: authHeaders(),
    })
    if (!response.ok) throw new Error(`删除失败（${response.status}）`)
    await loadSessions()
    if (id === sessionId.value) startNewSession()
  } catch (error) {
    uploadError.value = error.message
  }
}

async function sendMessage() {
  const question = input.value.trim()
  if (!question || loading.value || !modelReady.value) return

  messages.value.push({ role: 'user', content: question })
  messages.value.push({ role: 'assistant', content: '', sources: [] })
  input.value = ''
  loading.value = true
  await scrollToBottom()

  try {
    const response = await fetch(`${apiBase}/chat`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ input: question, session_id: sessionId.value }),
    })
    if (!response.ok || !response.body) throw new Error(`请求失败（${response.status}）`)

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split('\n\n')
      buffer = events.pop() || ''
      for (const event of events) {
        const line = event.split('\n').find((item) => item.startsWith('data: '))
        if (!line) continue
        const data = line.slice(6)
        if (data === '[DONE]') continue
        const payload = JSON.parse(data)
        if (payload.error) throw new Error(payload.error)
        const latest = messages.value[messages.value.length - 1]
        if (payload.turn_id !== undefined) {
          // 这轮回答的编号，反馈按钮挂在它上面
          latest.turnId = payload.turn_id
        } else if (payload.tool) {
          latest.toolStatus = TOOL_LABELS[payload.tool.name] || `正在调用 ${payload.tool.name}…`
        } else if (payload.handoff) {
          latest.handoff = payload.handoff
        } else if (payload.faq) {
          latest.faq = payload.faq
        } else if (payload.process) {
          latest.process = payload.process
        } else if (Array.isArray(payload.employees)) {
          latest.employees = payload.employees
        } else if (Array.isArray(payload.files)) {
          latest.files = payload.files
        } else if (Array.isArray(payload.sources)) {
          latest.sources = payload.sources
        } else {
          latest.content += payload.content || ''
          latest.toolStatus = ''
        }
        await scrollToBottom()
      }
    }
  } catch (error) {
    messages.value[messages.value.length - 1].content = `抱歉，服务暂时不可用：${error.message}`
  } finally {
    loading.value = false
    await loadSessions()
  }
}

function uploadFile(event) {
  const file = event.target.files[0]
  event.target.value = ''
  if (!file) return
  if (!currentUser.value) {
    loginOpen.value = true
    return
  }
  // 先弹窗确认适用范围和可见性，不直接上传——贴错标签会污染整个范围的问答
  pendingFile.value = file
  confirmScope.value = uploadScope.value.trim()
  confirmDepartment.value = currentUser.value?.department || '通用'
  confirmVisibility.value = 'department'
  confirmEffectiveFrom.value = ''
  confirmExpiresAt.value = ''
  uploadMessage.value = ''
  uploadError.value = ''
}

function cancelUpload() {
  pendingFile.value = null
  confirmScope.value = ''
}

async function confirmUpload() {
  const file = pendingFile.value
  if (!file || uploading.value) return
  uploading.value = true
  uploadMessage.value = ''
  uploadError.value = ''
  try {
    const response = await fetch(`${apiBase}/knowledge/upload`, {
      method: 'POST',
      headers: authHeaders({
        'X-Filename': encodeURIComponent(file.name),
        'X-Scope': encodeURIComponent(confirmScope.value.trim()),
        'X-Department': encodeURIComponent(confirmDepartment.value.trim()),
        'X-Visibility': confirmVisibility.value,
        'X-Effective-From': confirmEffectiveFrom.value,
        'X-Expires-At': confirmExpiresAt.value,
      }),
      body: file,
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '上传失败')
    uploadMessage.value = payload.message
    uploadScope.value = confirmScope.value.trim()
    await loadScopes()
    pendingFile.value = null
  } catch (error) {
    uploadError.value = error.message
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <main class="page-shell">
    <section class="chat-card">
      <header class="hero">
        <div>
          <p class="eyebrow">COMPANY KNOWLEDGE ASSISTANT</p>
          <h1>公司内部助手</h1>
          <p class="subtitle">新人快速熟悉业务与制度</p>
        </div>
        <div class="hero-right">
          <div class="status"><span v-if="modelReady"></span> {{ serviceStatus }}</div>
          <div v-if="currentUser" class="user-chip">
            {{ currentUser.display_name }} · {{ currentUser.department || '未配置部门' }} · {{ roleLabel }}
            <button type="button" class="link" @click="logout">退出</button>
          </div>
          <div class="hero-actions">
            <button v-if="!currentUser" type="button" class="login-button" @click="loginOpen = true">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
                <circle cx="12" cy="7" r="4" />
              </svg>
              员工登录
            </button>
            <button type="button" class="header-action" @click="openFileLibrary">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <path d="M14 2v6h6" />
              </svg>
              资料文件
            </button>
          </div>
        </div>
      </header>

      <div ref="messagesEl" class="messages">
        <div v-for="(message, index) in messages" :key="index" class="message-row" :class="message.role">
          <div class="avatar">{{ message.role === 'assistant' ? 'AI' : '我' }}</div>
          <div class="message-body">
            <p v-if="message.faq" class="faq-tag">常见问题 · {{ message.faq.title }}</p>
            <div class="bubble">{{ message.content || (loading && index === messages.length - 1 ? (message.toolStatus || '正在思考…') : '') }}</div>
            <div v-if="message.process" class="process-card">
              <p class="process-title">{{ message.process.title }}</p>
              <ol class="process-steps">
                <li v-for="(step, stepIndex) in message.process.steps" :key="stepIndex">{{ step }}</li>
              </ol>
              <p v-if="message.process.owner" class="process-owner">负责人：{{ message.process.owner }}</p>
              <div v-if="message.process.links && message.process.links.length" class="process-links">
                <a v-for="link in message.process.links" :key="link" :href="link" target="_blank" rel="noopener">打开办理入口</a>
              </div>
            </div>
            <div v-if="message.employees && message.employees.length" class="employee-cards">
              <div v-for="(person, personIndex) in message.employees" :key="personIndex" class="employee-card">
                <p class="employee-card-name">
                  {{ person.name }}
                  <span class="employee-dept">{{ person.department }}</span>
                  <span v-if="person.title" class="employee-role">{{ person.title }}</span>
                  <span v-if="person.employee_type" class="employee-kind">{{ person.employee_type }}</span>
                </p>
                <p v-if="person.responsibilities" class="employee-duty">负责：{{ person.responsibilities }}</p>
                <p v-if="person.contact" class="employee-contact">联系：{{ person.contact }}</p>
              </div>
            </div>
            <div v-if="message.files && message.files.length" class="file-cards">
              <div v-for="file in message.files" :key="file.file_id" class="file-card">
                <div class="file-icon">
                  <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <path d="M14 2v6h6" />
                    <path d="M16 13H8" />
                    <path d="M16 17H8" />
                  </svg>
                </div>
                <div class="file-main">
                  <p class="file-name">{{ file.name }}</p>
                  <p class="file-meta">
                    {{ file.publisher || '未知发布人' }} · {{ file.published_at || '日期未知' }}
                    <span v-if="file.group"> · {{ file.group }}</span>
                  </p>
                  <p v-if="file.description" class="file-desc">{{ file.description }}</p>
                </div>
                <button
                  type="button"
                  class="file-download"
                  title="下载到本地"
                  @click="downloadFile(file, message)"
                >
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <path d="M7 10l5 5 5-5" />
                    <path d="M12 15V3" />
                  </svg>
                  下载
                </button>
              </div>
            </div>
            <p v-if="message.fileError" class="feedback error">{{ message.fileError }}</p>
            <ul v-if="message.sources && message.sources.length" class="sources">
              <li v-for="source in message.sources" :key="source.index">
                <span class="source-name">{{ source.source }}</span>
                <span v-if="source.scope" class="scope-tag">{{ source.scope }}</span>
                <span v-if="source.visibility === 'staff'" class="visibility-tag">仅员工</span>
                <span
                  v-if="sourceValidity(source)"
                  class="validity-tag"
                  :class="{ expired: isExpiredSource(source) }"
                >{{ sourceValidity(source) }}</span>
                <span class="source-meta">
                  <span v-if="source.reranked" class="badge">精排</span>
                  第 {{ source.chunk + 1 }} 段 · 相关度 {{ Math.round(source.score * 100) }}%
                </span>
              </li>
            </ul>
            <p v-if="message.handoff" class="handoff">
              已转人工 · 工单号 {{ message.handoff.ticket_no }}
              <span v-if="message.handoff.scope" class="handoff-model">（{{ message.handoff.scope }}）</span>
            </p>
            <div v-if="message.role === 'assistant' && message.turnId" class="feedback-bar">
              <template v-if="!message.feedbackSent">
                <button
                  type="button"
                  class="feedback-btn"
                  title="这个回答有用"
                  aria-label="这个回答有用"
                  @click="sendFeedback(message, 1)"
                >
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M7 10v12" />
                    <path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" />
                  </svg>
                </button>
                <button
                  type="button"
                  class="feedback-btn"
                  title="这个回答没用"
                  aria-label="这个回答没用"
                  @click="sendFeedback(message, -1)"
                >
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M17 14V2" />
                    <path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" />
                  </svg>
                </button>
                <template v-if="message.feedbackOpen">
                  <input
                    v-model="message.feedbackComment"
                    class="feedback-input"
                    placeholder="哪里不对？可留空直接提交"
                    @keyup.enter="sendFeedback(message, -1)"
                  />
                  <button type="button" class="feedback-submit" @click="sendFeedback(message, -1)">提交</button>
                </template>
              </template>
              <span v-else class="feedback-done">
                {{ message.feedback === 1 ? '感谢反馈' : '已记录，我们会去补相关资料' }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <form class="composer" @submit.prevent="sendMessage">
        <input v-model="input" :disabled="loading || !modelReady" :placeholder="modelReady ? '请输入你的问题…' : serviceStatus" autocomplete="off" />
        <button type="submit" :disabled="loading || !modelReady || !input.trim()">发送</button>
      </form>
    </section>

    <aside class="side-column">
      <nav class="side-tabs">
        <button
          v-for="tab in sideTabs"
          :key="tab.key"
          type="button"
          class="side-tab"
          :class="{ active: activeTab === tab.key }"
          @click="selectTab(tab.key)"
        >{{ tab.label }}</button>
      </nav>

      <section v-if="activeTab === 'knowledge'" class="knowledge-card">
        <div class="card-heading">
          <div class="upload-icon">↑</div>
          <div>
            <h2>更新知识库</h2>
            <p>主管和新人都可以导入资料</p>
          </div>
        </div>
        <p class="permission-hint">
          {{ isHR ? '人事可以指定任意部门，并导入敏感资料（敏感资料不进问答）。'
                  : `资料默认归入「${currentUser?.department || '本部门'}」，也可以选择全员公开。` }}
        </p>
        <label class="model-field">
          <span>适用范围（留空表示通用资料）</span>
          <input v-model="uploadScope" list="scope-options" placeholder="例如 DEEBOT T20 / 人力资源部" />
          <datalist id="scope-options">
            <option v-for="scope in scopes" :key="scope" :value="scope" />
          </datalist>
        </label>
        <label class="upload-box">
          <input
            type="file"
            :disabled="!modelReady"
            accept=".txt,.md,.markdown,.pdf,.docx,.png,.jpg,.jpeg,.webp,.bmp"
            @change="uploadFile"
          />
          <span>点击选择文件</span>
          <small>上传前会弹窗确认部门、可见性和时效</small>
        </label>
        <p v-if="uploadMessage" class="feedback success">{{ uploadMessage }}</p>
        <p v-if="uploadError" class="feedback error">{{ uploadError }}</p>
      </section>

      <section v-else-if="activeTab === 'directory'" class="directory-card">
        <div class="card-heading">
          <div class="upload-icon">
            <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
              <circle cx="9" cy="7" r="4" />
              <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
              <path d="M16 3.13a4 4 0 0 1 0 7.75" />
            </svg>
          </div>
          <div>
            <h2>员工通讯录</h2>
            <p>找负责的同事，看联系方式和负责范围</p>
          </div>
        </div>
        <input
          v-model="employeeKeyword"
          class="directory-search"
          placeholder="搜事项或姓名，例如「报销」"
          @input="onEmployeeSearch"
        />
        <select v-model="employeeDepartment" class="directory-select" @change="loadEmployees">
          <option value="">全部部门</option>
          <option v-for="department in employeeDepartments" :key="department" :value="department">
            {{ department }}
          </option>
        </select>
        <button v-if="isStaff" type="button" class="new-session" @click="openEmployeeModal">
          添加员工卡片
        </button>
        <p v-if="employeeMessage" class="feedback success">{{ employeeMessage }}</p>
        <p v-if="employeeError" class="feedback error">{{ employeeError }}</p>
        <ul v-if="employees.length" class="employee-list">
          <li v-for="person in employees" :key="person.employee_id">
            <div class="employee-row-head">
              <p class="employee-card-name">
                {{ person.name }}
                <span class="employee-dept">{{ person.department }}</span>
                <span v-if="person.title" class="employee-role">{{ person.title }}</span>
                <span v-if="person.employee_type" class="employee-kind">{{ person.employee_type }}</span>
              </p>
              <button
                v-if="isStaff"
                type="button"
                class="employee-delete"
                title="删除员工卡片"
                aria-label="删除员工卡片"
                @click.stop="removeEmployee(person)"
              >
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M3 6h18" />
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                  <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                </svg>
              </button>
            </div>
            <p v-if="person.responsibilities" class="employee-duty">负责：{{ person.responsibilities }}</p>
            <p v-if="person.contact" class="employee-contact">联系：{{ person.contact }}</p>
          </li>
        </ul>
        <p v-else class="feedback">没有匹配的同事，换个关键词试试。</p>
      </section>

      <section v-else-if="activeTab === 'blindspots'" class="sessions-card">
        <div class="card-heading">
          <div class="upload-icon">
            <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
              <path d="M12 9v4" />
              <path d="M12 17h.01" />
            </svg>
          </div>
          <div>
            <h2>知识盲区</h2>
            <p>答不上来、转人工、被点没用的问题</p>
          </div>
        </div>
        <label class="model-field compact">
          <span>统计范围</span>
          <select v-model.number="blindSpotDays" @change="loadBlindSpots">
            <option :value="7">最近 7 天</option>
            <option :value="30">最近 30 天</option>
            <option :value="90">最近 90 天</option>
          </select>
        </label>
        <p v-if="blindSpotError" class="feedback error">{{ blindSpotError }}</p>
        <ul v-if="blindSpots.length" class="blind-spot-list">
          <li v-for="spot in blindSpots" :key="spot.question">
            <p class="blind-spot-question">{{ spot.question }}</p>
            <p class="blind-spot-meta">
              出现 {{ spot.times }} 次
              <span v-if="spot.negative"> · 差评 {{ spot.negative }}</span>
              <span v-if="spot.handoffs"> · 转人工 {{ spot.handoffs }}</span>
            </p>
            <p v-if="spot.last_answer" class="blind-spot-answer">最近回答：{{ spot.last_answer }}</p>
          </li>
        </ul>
        <p v-else class="feedback">
          {{ blindSpotError ? '' : '这段时间没有盲区，知识库接得住。' }}
        </p>
      </section>

      <section v-else-if="activeTab === 'audit'" class="sessions-card">
        <div class="card-heading">
          <div class="upload-icon">
            <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <path d="M14 2v6h6" />
              <path d="M9 13h6" />
              <path d="M9 17h6" />
            </svg>
          </div>
          <div>
            <h2>审计日志</h2>
            <p>谁在什么时候动了什么</p>
          </div>
        </div>
        <p v-if="auditError" class="feedback error">{{ auditError }}</p>
        <ul v-if="auditLogs.length" class="audit-list">
          <li v-for="entry in auditLogs" :key="entry.id">
            <p class="audit-line">
              <span class="audit-action">{{ auditLabel(entry.action) }}</span>
              <span v-if="entry.status !== 'ok'" class="audit-failed">失败</span>
              <span class="audit-time">{{ formatTime(entry.created_at) }}</span>
            </p>
            <p class="audit-meta">
              {{ entry.username || '未登录' }}
              <span v-if="entry.target"> · {{ entry.target }}</span>
            </p>
          </li>
        </ul>
        <p v-else class="feedback">{{ auditError ? '' : '还没有审计记录。' }}</p>
      </section>

      <section v-else class="sessions-card">
        <div class="card-heading">
          <div class="upload-icon">
            <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
              <path d="M3 3v5h5" />
              <path d="M12 7v5l4 2" />
            </svg>
          </div>
          <div>
            <h2>历史会话</h2>
            <p>共 {{ sessions.length }} 个，点击可继续话题</p>
          </div>
        </div>
        <button class="new-session" type="button" @click="startNewSession">开始新会话</button>
        <ul v-if="sessions.length" class="session-list">
          <li v-for="item in sessions" :key="item.id" :class="{ active: item.id === sessionId }">
            <button class="session-open" type="button" @click="openSession(item.id)">
              <span class="session-preview">{{ item.preview || '（空会话）' }}</span>
              <span class="session-meta">
                {{ formatTime(item.updated_at) }} · {{ item.turn_count }} 轮
                <span v-if="item.summarized" class="badge">已压缩</span>
              </span>
            </button>
            <button
              class="session-delete"
              type="button"
              title="删除会话"
              aria-label="删除会话"
              @click.stop="removeSession(item.id)"
            >
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M3 6h18" />
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
              </svg>
            </button>
          </li>
        </ul>
        <p v-else class="feedback">还没有历史会话。</p>
      </section>
    </aside>

    <div v-if="pendingFile" class="modal-mask" @click.self="cancelUpload">
      <div class="modal">
        <h3>请确认适用范围、可见性和时效</h3>
        <p class="modal-file">{{ pendingFile.name }}</p>
        <p class="modal-hint">同名、同适用范围的资料会作为新版本替换旧版；内容完全相同的文件会自动跳过。</p>
        <label class="model-field">
          <span>适用范围（型号 / 部门 / 业务线；多个用逗号分隔，留空表示通用）</span>
          <input v-model="confirmScope" list="scope-options" placeholder="例如 DEEBOT T20" />
        </label>
        <label class="model-field">
          <span>归属部门（新人 / 主管只能选本部门或「通用」，人事可任选）</span>
          <input
            v-model="confirmDepartment"
            list="department-options"
            :disabled="!isHR"
            placeholder="例如 客户服务部"
          />
          <datalist id="department-options">
            <option value="通用" />
            <option v-for="dept in employeeDepartments" :key="dept" :value="dept" />
          </datalist>
        </label>
        <label class="model-field">
          <span>可见性</span>
          <select v-model="confirmVisibility">
            <option v-for="option in uploadVisibilityOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <div class="validity-fields">
          <label class="model-field">
            <span>生效日期（留空=今天）</span>
            <input v-model="confirmEffectiveFrom" type="date" />
          </label>
          <label class="model-field">
            <span>失效日期（留空=长期有效）</span>
            <input v-model="confirmExpiresAt" type="date" />
          </label>
        </div>
        <p v-if="confirmVisibility === 'sensitive'" class="feedback warn">
          敏感资料只出现在人事的文件库，不参与问答检索。
        </p>
        <p v-if="scopeMismatch" class="feedback warn">
          文件名里出现「{{ scopeHint }}」，但适用范围填的是「{{ confirmScope }}」。确认没有贴错吗？
        </p>
        <div class="modal-actions">
          <button type="button" class="ghost" :disabled="uploading" @click="cancelUpload">取消</button>
          <button type="button" :disabled="uploading" @click="confirmUpload">
            {{ uploading ? '上传中…' : '确认上传' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="fileLibraryOpen" class="modal-mask" @click.self="fileLibraryOpen = false">
      <div class="modal modal-wide">
        <h3>资料文件</h3>
        <p class="modal-hint">
          这里能看到当前账号有权限查看的模拟文件。可以下载到本地，也可以直接导入知识库。
          敏感资料只对人事主管可见，并且不会进入问答检索。
        </p>
        <p v-if="libraryLoading" class="feedback">正在加载文件…</p>
        <p v-if="libraryError" class="feedback error">{{ libraryError }}</p>
        <p v-if="libraryMessage" class="feedback success">{{ libraryMessage }}</p>
        <ul v-if="libraryFiles.length" class="library-list">
          <li v-for="file in libraryFiles" :key="file.file_id" class="library-item">
            <div class="file-icon">
              <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <path d="M14 2v6h6" />
              </svg>
            </div>
            <div class="file-main">
              <p class="file-name">{{ file.name }}</p>
              <p class="file-meta">
                {{ file.publisher || '未知发布人' }} · {{ file.published_at || '日期未知' }}
                · {{ file.department }}<span v-if="file.group"> · {{ file.group }}</span>
              </p>
              <p v-if="file.description" class="file-desc">{{ file.description }}</p>
            </div>
            <div class="library-actions">
              <span v-if="!file.sensitive" class="visibility-tag">{{ fileVisibilityLabel(file.visibility) }}</span>
              <span v-if="file.sensitive" class="sensitive-tag">敏感</span>
              <span v-if="file.imported" class="imported-tag">已导入</span>
              <button type="button" class="file-download" @click="downloadFile(file)">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <path d="M7 10l5 5 5-5" />
                  <path d="M12 15V3" />
                </svg>
                下载
              </button>
              <button
                v-if="file.can_import && !file.imported"
                type="button"
                class="file-import"
                :disabled="importingFileId === file.file_id"
                @click="importLibraryFile(file)"
              >
                {{ importingFileId === file.file_id ? '导入中…' : '导入知识库' }}
              </button>
            </div>
          </li>
        </ul>
        <p v-else-if="!libraryLoading && !libraryError" class="feedback">
          当前账号没有可查看的文件。
        </p>
        <div class="modal-actions">
          <button type="button" class="ghost" @click="fileLibraryOpen = false">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="employeeModalOpen" class="modal-mask" @click.self="employeeModalOpen = false">
      <div class="modal">
        <h3>添加员工卡片</h3>
        <p class="modal-hint">
          姓名和部门必填。不勾选「对新人可见」时，只有员工能查到这张卡片。
        </p>
        <div class="form-grid">
          <label class="model-field">
            <span>姓名 *</span>
            <input v-model="employeeForm.name" placeholder="例如 王小明" />
          </label>
          <label class="model-field">
            <span>部门 *</span>
            <input v-model="employeeForm.department" placeholder="例如 行政部" />
          </label>
        </div>
        <label class="model-field">
          <span>职务</span>
          <input v-model="employeeForm.title" placeholder="例如 行政专员" />
        </label>
        <label class="model-field">
          <span>人员类型</span>
          <select v-model="employeeForm.employee_type">
            <option value="主管">主管</option>
            <option value="员工">员工</option>
          </select>
        </label>
        <label class="model-field">
          <span>负责内容</span>
          <input v-model="employeeForm.responsibilities" placeholder="例如 办公用品、门禁补办" />
        </label>
        <label class="model-field">
          <span>联系方式</span>
          <input v-model="employeeForm.contact" placeholder="例如 分机 8021 / name@example.com" />
        </label>
        <label class="model-field">
          <span>关键词（用 | 分隔，便于按事项检索）</span>
          <input v-model="employeeForm.keywords" placeholder="办公用品|门禁|工牌" />
        </label>
        <label class="model-field">
          <span>适用范围（留空=通用）</span>
          <input v-model="employeeForm.scope" placeholder="例如 行政部" />
        </label>
        <label class="checkbox-field">
          <input v-model="employeeForm.visibleToNewcomers" type="checkbox" />
          <span>对新人可见</span>
        </label>
        <p v-if="employeeError" class="feedback error">{{ employeeError }}</p>
        <div class="modal-actions">
          <button type="button" class="ghost" :disabled="savingEmployee" @click="employeeModalOpen = false">取消</button>
          <button type="button" :disabled="savingEmployee" @click="submitEmployee">
            {{ savingEmployee ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="loginOpen" class="modal-mask" @click.self="loginOpen = false">
      <div class="modal">
        <h3>登录公司内部助手</h3>
        <p class="modal-hint">
          部门主管可以导入本部门资料、查看盲区和审计；人事主管可以跨部门管理资料；
          新人可以导入本部门或公开资料，并提问、查通讯录。
        </p>
        <div class="demo-accounts">
          <p><span class="demo-role">客服主管</span> staff / staff123</p>
          <p><span class="demo-role">人事主管</span> hr / hr123</p>
          <p><span class="demo-role newcomer">客服新人</span> newbie / newbie123</p>
        </div>
        <label class="model-field">
          <span>用户名</span>
          <input v-model="loginForm.username" autocomplete="username" />
        </label>
        <label class="model-field">
          <span>密码</span>
          <input v-model="loginForm.password" type="password" autocomplete="current-password" @keyup.enter="submitLogin" />
        </label>
        <p v-if="loginError" class="feedback error">{{ loginError }}</p>
        <div class="modal-actions">
          <button type="button" class="ghost" @click="loginOpen = false">取消</button>
          <button type="button" @click="submitLogin">登录</button>
        </div>
      </div>
    </div>
  </main>
</template>
