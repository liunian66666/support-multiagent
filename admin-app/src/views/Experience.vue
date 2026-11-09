<template>
  <div>
    <div class="head-row">
      <div>
        <h2>可复用经验</h2>
        <p class="text-2" style="margin-top:6px;">经验只指导客服如何处理问题；订单与政策事实仍以实时查询和已发布知识库为准。</p>
      </div>
      <button class="btn btn-primary btn-sm" v-perm="'experience:edit'" @click="openCreate">新增经验</button>
    </div>

    <div class="card" style="margin:16px 0;display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
      <div style="flex:1;min-width:220px;">
        <b>在客服回答中使用经验</b>
        <p class="text-2" style="margin-top:4px;">仅使用本店已发布的经验；关闭后立即停止向新对话提供经验。</p>
      </div>
      <span class="badge" :class="mode === 'inject' ? 'badge-green' : 'badge-gray'">{{ mode === 'inject' ? '已开启' : (mode === 'shadow' ? '仅观察' : '已关闭') }}</span>
      <button class="btn btn-sm" v-perm="'experience:edit'" :disabled="savingMode || loading" @click="toggleMode">
        {{ savingMode ? '保存中…' : (mode === 'inject' ? '关闭' : '开启') }}
      </button>
    </div>

    <div class="card">
      <div class="head-row" style="margin-bottom:12px;">
        <div class="filters">
        <input v-model="keyword" class="input" style="width:220px;" placeholder="搜索经验或关键词" @keyup.enter="search" />
          <select v-model="status" class="select" style="width:130px;" @change="search">
            <option value="">全部状态</option><option value="candidate">待审核</option>
            <option value="published">已发布</option><option value="suspended">已停用</option>
          </select>
        </div>
        <button class="btn btn-sm" @click="search">搜索</button>
      </div>
      <DataTable :columns="columns" :rows="items">
        <template #cell-content="{ row }"><div style="max-width:460px;white-space:pre-wrap;">{{ row.content }}</div></template>
        <template #cell-keywords="{ row }">{{ (row.applicability?.keywords || []).join('、') || '-' }}</template>
        <template #cell-review_status="{ row }"><span class="badge" :class="row.review_status === 'published' ? 'badge-green' : 'badge-gray'">{{ stateText(row.review_status) }}</span></template>
        <template #cell-actions="{ row }">
          <div style="display:flex;gap:5px;">
            <button class="btn btn-sm" v-perm="'experience:edit'" @click="openEdit(row)">编辑</button>
            <button v-if="row.review_status !== 'published' && row.review_status !== 'expired'" class="btn btn-sm btn-primary" v-perm="'experience:edit'" @click="review(row, 'publish')">发布</button>
            <button v-if="row.review_status === 'published'" class="btn btn-sm" v-perm="'experience:edit'" @click="review(row, 'suspend')">停用</button>
          </div>
        </template>
      </DataTable>
      <Pagination :total="total" :page="page" :size="size" @change="onPage" />
      <p class="text-3" style="font-size:12px;margin-top:10px;">编辑已发布经验会先停用，审核后可重新发布。</p>
    </div>

    <Modal :visible="showEditor" :title="editing ? '编辑经验' : '新增经验'" @close="showEditor = false">
      <div class="form">
        <label class="field"><span class="f-label">处理经验</span><textarea v-model="form.content" class="textarea" style="min-height:140px;" maxlength="1000" placeholder="例如：先核对订单状态，再依据已发布退款政策说明可操作的下一步。" /></label>
        <label class="field"><span class="f-label">触发关键词（用逗号分隔，至少 1 个）</span><input v-model="form.keywords" class="input" placeholder="退款, 未发货" /></label>
        <label class="field"><span class="f-label">适用客服类型</span><select v-model="form.route" class="select"><option value="">通用</option><option value="faq">知识库</option><option value="order">订单</option><option value="general">一般咨询</option></select></label>
      </div>
      <template #footer><button class="btn" @click="showEditor = false">取消</button><button class="btn btn-primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存为待审核' }}</button></template>
    </Modal>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { api, toast } from '../api'
import DataTable from '../components/DataTable.vue'
import Modal from '../components/Modal.vue'
import Pagination from '../components/Pagination.vue'

const columns = [
  { key: 'id', title: '编号', width: '65px' }, { key: 'content', title: '处理经验' },
  { key: 'keywords', title: '触发关键词', width: '180px' },
  { key: 'review_status', title: '状态', width: '90px' },
  { key: 'actions', title: '操作', width: '180px' }
]
const items = ref([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const mode = ref('off')
const loading = ref(true)
const savingMode = ref(false)
const saving = ref(false)
const keyword = ref('')
const status = ref('')
const showEditor = ref(false)
const editing = ref(null)
const form = reactive({ content: '', keywords: '', route: '' })

function stateText(value) { return ({ candidate:'待审核', published:'已发布', suspended:'已停用', expired:'已过期' })[value] || value }
function search() { page.value = 1; load() }
function onPage(next) { page.value = next.page; size.value = next.size; load() }
async function load() {
  loading.value = true
  try {
    const [m, list] = await Promise.all([
      api('/api/admin/experience/mode'),
      api('/api/admin/experience/items', { params:{ page:page.value, size:size.value, q:keyword.value.trim(), status:status.value } })
    ])
    mode.value = m.data?.mode || 'off'
    items.value = list.data?.items || []
    total.value = list.data?.total || 0
  } finally { loading.value = false }
}
async function toggleMode() {
  savingMode.value = true
  try {
    const next = mode.value === 'inject' ? 'off' : 'inject'
    await api('/api/admin/experience/mode', { method:'POST', body:{ mode: next } })
    mode.value = next
    toast(next === 'inject' ? '经验功能已开启' : '经验功能已关闭', 'success')
  } finally { savingMode.value = false }
}
function openCreate() { editing.value = null; Object.assign(form, { content:'', keywords:'', route:'' }); showEditor.value = true }
function openEdit(row) { editing.value = row; Object.assign(form, { content:row.content, keywords:(row.applicability?.keywords || []).join(', '), route:row.route || '' }); showEditor.value = true }
async function save() {
  const keywords = [...new Set(form.keywords.split(/[,，]/).map(s => s.trim()).filter(Boolean))]
  if (!form.content.trim() || !keywords.length) { toast('请填写经验内容和触发关键词', 'error'); return }
  saving.value = true
  try {
    const path = editing.value ? '/api/admin/experience/items/' + editing.value.id : '/api/admin/experience/items'
    await api(path, { method: editing.value ? 'PUT' : 'POST', body:{ content:form.content.trim(), keywords, route:form.route } })
    showEditor.value = false
    toast('经验已保存，请审核发布', 'success')
    await load()
  } finally { saving.value = false }
}
async function review(row, action) {
  await api('/api/admin/experience/review', { method:'POST', body:{ experience_id:row.id, action } })
  toast(action === 'publish' ? '经验已发布' : '经验已停用', 'success')
  await load()
}
onMounted(load)
</script>
