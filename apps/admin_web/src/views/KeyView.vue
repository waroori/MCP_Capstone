<script setup>
// API 사용 승인 뒤 Slack으로 받은 일회용 링크. 본인만, 한 번만, 24시간 안에 키 전체를 본다.
// 링크 미리보기 등으로 소모되지 않게 열자마자 부르지 않고 [키 보기]를 눌러야 서버가 내준다(그 순간 서버에서 지워짐).
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { post, errorLabel } from '../request.js'

const id = String(useRoute().params.id)
const key = ref(null), error = ref(''), copied = ref(false), busy = ref(false)

async function reveal() {
  busy.value = true
  try { key.value = await post(`/keys/handoff/${encodeURIComponent(id)}/reveal`) } catch (e) { error.value = e.message || errorLabel(e) }
  busy.value = false
}
async function copy() {
  try { await navigator.clipboard.writeText(key.value.secret); copied.value = true } catch { copied.value = false }
}
</script>

<template>
  <main id="main" class="key-page">
    <section class="card key-card" aria-labelledby="key-h">
      <h1 id="key-h">API 키 확인</h1>
      <template v-if="!key">
        <p class="small" style="margin: 0">이 키는 <strong>한 번만</strong> 볼 수 있어요. 누르면 서버에서 지워지니, 바로 안전한 곳(환경변수·사내 비밀 저장소)에 옮겨 두세요.</p>
        <p v-if="error" class="status" role="alert">{{ error }}</p>
        <button v-else type="button" class="btn btn-dark" :disabled="busy" @click="reveal">키 보기</button>
      </template>
      <template v-else>
        <span class="small muted">{{ key.product }} · {{ key.project_name }}</span>
        <label class="small" for="secret">키 전체 (앞부분 {{ key.key_prefix }}…)</label>
        <input id="secret" class="secret" :value="key.secret" readonly @focus="$event.target.select()" />
        <button type="button" class="btn btn-dark" @click="copy">{{ copied ? '복사했어요' : '복사' }}</button>
        <p class="status" role="status">이 페이지를 닫으면 다시 볼 수 없어요. Slack·메일·코드 저장소에 붙여 넣지 마세요.</p>
      </template>
    </section>
  </main>
</template>

<style scoped>
.key-page { min-height: 100vh; display: grid; place-items: center; padding: 16px; background: var(--paper); box-sizing: border-box; }
.key-card { width: 100%; max-width: 480px; }
.key-card h1 { margin: 0; font-size: 22px; }
.secret { width: 100%; box-sizing: border-box; min-height: 48px; padding: 0 12px; border: 1px solid var(--line-strong); border-radius: 10px; font: 14px ui-monospace, monospace; }
</style>
