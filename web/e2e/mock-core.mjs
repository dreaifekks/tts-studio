// 假的主控 core：页面在 /ui/ 下照常由 vite 提供，其余接口都在这里应答，不碰真实服务。
// 模拟主控、局域网算力和云端账号池。
// 用法：const core = mockCore(page, { overrides: { 'POST /plan': (req, world) => ({ ... }) } })；core.world 可以在测试里改。

export const ENGINES = ['v1', 'v2', 'v2pro', 'v2proplus', 'v3', 'v4', 'v5turbo', 'v5dev']
export const ALICE_V4 = 'v4/alice-v4-e15.ckpt/alice-v4-e8.pth'
export const ALICE_PP = 'v2proplus/alice-pp-e15.ckpt/alice-pp-e8.pth'
export const ALICE_BASE_V4 = 'v4/pretrained/pretrained'

const LORA_ENGINES = new Set(['v3', 'v4', 'v5turbo', 'v5dev'])
const AUX_ENGINES = new Set(['v1', 'v2', 'v2pro', 'v2proplus'])
const CAP_LABELS = { aux_refs: '辅助参考', fragment_interval: '分段间隔' }
const trainDefaultsFor = (id) => (LORA_ENGINES.has(id)
  ? { sovits_epochs: 2, sovits_save_every: 1, sovits_batch: 4, gpt_epochs: 15, gpt_save_every: 5, gpt_batch: 8, lora: true, lora_rank: 32, lora_ranks: [16, 32, 64, 128] }
  : { sovits_epochs: 8, sovits_save_every: 4, sovits_batch: 8, gpt_epochs: 15, gpt_save_every: 5, gpt_batch: 8, lora: false, lora_rank: null, lora_ranks: [] })

const speeds = (o) => {
  const out = Object.fromEntries(ENGINES.map((e) => [e, o[e] ? { sec_per_item: o[e][0], source: o[e][1] === 'm' ? 'measured' : 'reference' } : { sec_per_item: null, source: 'untested' }]))
  if (o.train) out.train = { sec_per_item: o.train[0], source: o.train[1] === 'm' ? 'measured' : 'reference' }
  if (o.scorer) out.scorer = { sec_per_item: o.scorer[0], source: o.scorer[1] === 'm' ? 'measured' : 'reference' }
  return out
}
const json = (body, status = 200) => ({ status, body })

export function makeWorld() {
  const GB = 1024 ** 3
  const MB = 1024 ** 2
  const cloudSpeeds = {
    L4: speeds({ v2pro: [3.9, 'm'], v2proplus: [4, 'm'], v4: [7, 'm'], v5turbo: [10, 'r'], v5dev: [40, 'r'], train: [1.1, 'r'], scorer: [0, 'r'] }),
    T4: speeds({ v2pro: [6, 'r'], v2proplus: [7, 'r'], v4: [12, 'r'], v5turbo: [16, 'r'], v5dev: [60, 'r'], train: [1.75, 'r'], scorer: [0, 'r'] }),
    A100: speeds({ v2pro: [3, 'r'], v2proplus: [3.5, 'r'], v4: [5, 'r'], v5turbo: [7, 'r'], v5dev: [28, 'r'], train: [0.66, 'r'], scorer: [0, 'r'] }),
    G4: speeds({ v2pro: [2.8, 'r'], v2proplus: [3, 'r'], v4: [4, 'r'], v5turbo: [6, 'r'], v5dev: [22, 'r'], train: [0.55, 'r'], scorer: [0, 'r'] }),
  }
  const devices = [
    {
      id: 'local', name: 'studio-host', type: 'local', status: 'idle', activity: null, machine: 'studio-host', engines: ENGINES, loaded: [], wakeable: false, caps: ['aux_refs', 'fragment_interval'],
      disk_free_bytes: 128 * GB, disk_total_bytes: 512 * GB, cache_bytes: Math.round(18.4 * GB),
      hw: { cpu: 'Apple M4', cores: 10, ram_gb: 32, accel: 'mps', gpu: null, os: 'macOS', torch: '2.5' },
      speeds: speeds({ v1: [2.2, 'r'], v2: [2.3, 'r'], v2pro: [2.4, 'm'], v2proplus: [2.9, 'm'], v4: [31, 'm'], train: [5.5, 'r'], scorer: [4.3, 'm'] }),
    },
    {
      id: 'lan-gpu-worker', name: 'gpu-worker', type: 'lan', status: 'busy', activity: '正在评测', machine: 'gpu-worker', address: 'gpu-worker.local', engines: ['v2pro', 'v2proplus', 'v4'],
      loaded: ['v2pro'], wakeable: false, caps: ['aux_refs', 'fragment_interval'],
      disk_free_bytes: 210 * GB, disk_total_bytes: 500 * GB, cache_bytes: Math.round(6.2 * GB),
      hw: { cpu: 'Intel N100', cores: 4, ram_gb: 16, accel: 'cpu', gpu: null, os: 'Linux', torch: '2.5' },
      speeds: speeds({ v1: [6, 'r'], v2: [7, 'r'], v2pro: [8, 'm'], v2proplus: [10, 'r'], v4: [60, 'r'], scorer: [6, 'r'] }),
    },
    ...[['L4', 1.94], ['T4', 1.15], ['A100', 6.73], ['G4', 10.05]].map(([gpu, rate]) => ({
      id: `colab-${gpu}`, name: `云端 ${gpu === 'G4' ? 'RTX PRO 6000' : gpu}`, type: 'cloud', status: 'idle', activity: null, warm: 0, gpu, rate, premium: gpu === 'G4', engines: ENGINES,
      speeds: cloudSpeeds[gpu], accounts: 2, hours: 100,
    })),
  ]
  const account = (id, email, balance, used, gpus) => ({ id, label: email.split('@')[0], email, balance, month_used_estimate: used, gpus, disabled: false, error: null })
  const gpuRow = (gpu, label, rate, count, hours, sp) => ({ gpu, label, account_count: count, units_per_hour: rate, hours_left: hours, available: count > 0, reason: count ? null : '账号池里没有账号能开这张卡', premium: gpu === 'G4', speeds: sp })
  const slugs = ['alice', 'bob', 'carol', 'dave', 'erin', 'frank']
  const prefs = Object.fromEntries(slugs.map((slug) => [slug, {
    slug, default_ref: `${slug}-ref-1`, synth_params: {}, last_ref_id: null, aux_ref_ids: [],
  }]))
  const world = {
    health: { status: 'ok', version: '0.2.0', commit: 'mock', training_job: null, workers: {} },
    prefs,
    models: {
      characters: slugs.map((slug) => ({
        slug, source: 'builtin', dataset_id: slug === 'alice' ? 'ds-alice' : null, default_ref: `${slug}-ref-1`,
        prefs: prefs[slug],
        engines: Object.fromEntries(ENGINES.map((e) => [e, { gpt: [], sovits: [], default_gpt: `${e}-base.ckpt`, default_sovits: `${e}-base.pth` }])),
      })),
      engines: ENGINES.map((id) => ({
        id, version: id, code: id.startsWith('v5') ? 'v5' : 'fork', code_path: '', zero_shot: true, trainable: true, train_method: 'gsv',
        pretrained_gpt: '', pretrained_sovits: '', has_july_weights: false,
        supports_aux_refs: AUX_ENGINES.has(id),
        train_defaults: trainDefaultsFor(id),
      })),
    },
    versions: {
      alice: [
        { id: ALICE_V4, label: '11 月训练', engine: 'v4', gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth', score: 94.6, score_set: 'hard', recommended: true, source_run: 'run-1120', created: '2026-11-18T09:00:00' },
        { id: ALICE_PP, label: '10 月训练', engine: 'v2proplus', gpt: 'alice-pp-e15.ckpt', sovits: 'alice-pp-e8.pth', score: 92.1, score_set: 'hard', recommended: false, source_run: 'run-1031', created: '2026-10-31T09:00:00' },
        { id: ALICE_BASE_V4, label: '底模（零样本）', engine: 'v4', gpt: 'v4-base.ckpt', sovits: 'v4-base.pth', score: null, score_set: null, recommended: false, source_run: null, created: '2026-09-01T00:00:00' },
      ],
    },
    compute: { devices, auto: { mode: 'auto', device: null, ask_over: 2, ask_month: 500 }, engines: ENGINES, month_used: 151 },
    library: {
      selected: true, error: null, path: '/Users/demo/Library/CloudStorage/GoogleDrive-demo/My Drive/ai/tts-library', kind: 'drive', id: 'lib-1',
      owner: { machine_id: 'm-studio-host', name: 'studio-host' }, owned: true, this_machine: { machine_id: 'm-studio-host', name: 'studio-host' },
      counts: { characters: 6, refs: 2140, datasets: 4, weights: 31, evals: 58, blind: 7, works: 3, runs: 12, bench: 4 },
      conflicts: 1, conflict_files: ['characters/alice/refs (conflict 1).json'], broken_files: [], scanned_at: new Date(Date.now() - 30_000).toISOString(),
    },
    cloud: {
      installed: true,
      accounts: [
        account('acct-main', 'studio.main@example.com', 2828, 31, ['L4', 'T4', 'A100', 'G4']),
        account('acct-alt1', 'studio.alt1@example.com', 1240, 120, ['L4', 'T4']),
      ],
      gpus: [
        gpuRow('L4', 'L4', 1.94, 2, 2096, cloudSpeeds.L4),
        gpuRow('T4', 'T4', 1.15, 2, 3537, cloudSpeeds.T4),
        gpuRow('A100', 'A100', 6.73, 1, 420, cloudSpeeds.A100),
        gpuRow('G4', 'RTX PRO 6000', 10.05, 1, 281, cloudSpeeds.G4),
        gpuRow('H100', 'H100', null, 0, null, speeds({})),
      ],
      total_balance: 4068, month_used_estimate: 151,
      usage_monthly: ['06', '07', '08', '09', '10', '11'].map((m, i) => ({ month: `2026-${m}`, by_account: { 'acct-main': [12, 48, 20, 8, 35, 31][i], 'acct-alt1': [0, 30, 60, 15, 40, 120][i] } })),
      account_labels: { 'acct-main': 'studio.main@example.com', 'acct-alt1': 'studio.alt1@example.com' },
      settings: { gpu_pref: 'auto', max_minutes: null, keep_minutes: 10 },
    },
    systemInfo: { web_url: 'http://studio-host.local/ui/', host: 'studio-host.local', port: 80, lan_reachable: true, web_available: true, machine: 'studio-host' },
    systemVersions: { studio: { current: '0.2.0', latest: '0.2.0', notes_url: 'https://example.com/releases/0.2.0', error: null }, core: { current: '0.2.0' }, checked_at: '2026-11-20T10:00:00Z' },
    storage: {
      measured_at: new Date(Date.now() - 120_000).toISOString(),
      disk: {
        path: '/Users/demo',
        total_bytes: 512 * GB,
        used_bytes: 384 * GB,
        free_bytes: 128 * GB,
      },
      library: {
        selected: true,
        kind: 'drive',
        path: '/Users/demo/Library/CloudStorage/GoogleDrive-demo/My Drive/ai/tts-library',
        total_bytes: Math.round(24.6 * GB),
        local_bytes: Math.round(4.2 * GB),
        categories: [
          { key: 'weights', label: '模型权重', bytes: Math.round(12.4 * GB), local_bytes: Math.round(1.8 * GB), files: 31 },
          { key: 'datasets', label: '训练数据集', bytes: Math.round(6.5 * GB), local_bytes: Math.round(0.6 * GB), files: 4 },
          { key: 'runs', label: '训练产出', bytes: Math.round(3.1 * GB), local_bytes: Math.round(0.4 * GB), files: 12 },
          { key: 'works', label: '作品音频', bytes: Math.round(1.2 * GB), local_bytes: Math.round(0.8 * GB), files: 45 },
          { key: 'refs', label: '参考音频', bytes: Math.round(1.1 * GB), local_bytes: Math.round(0.5 * GB), files: 2140 },
          { key: 'other', label: '其他', bytes: Math.round(310 * MB), local_bytes: Math.round(105 * MB), files: 18 },
        ],
      },
      local: {
        machine: 'studio-host',
        total_bytes: Math.round(18.4 * GB),
        categories: [
          { key: 'engines', label: '语音引擎与基础模型', bytes: Math.round(11.2 * GB), files: 42 },
          { key: 'datasets_cache', label: '数据集解包缓存', bytes: Math.round(3.6 * GB), files: 810 },
          { key: 'runtime', label: '历史音频与运行数据', bytes: Math.round(2.1 * GB), files: 320 },
          { key: 'train_tmp', label: '训练暂存', bytes: Math.round(980 * MB), files: 14 },
          { key: 'model_cache', label: '缓存模型', bytes: Math.round(540 * MB), files: 6 },
        ],
      },
    },
    jobs: [],
    refs: [{ id: 'alice-ref-1', label: '', audio_url: '/refs/alice-ref-1/audio', character: 'alice', text: 'おはよう。', lang: 'ja', duration: 3.2, tags: ['平静'] }],
    weights: [],
    evalSets: [
      { name: 'hard', description: '难句：长句、专有名词、语气词', default: true, candidates: 3, sentences: Array.from({ length: 12 }, (_, i) => ({ id: `h${i}`, text: `难句 ${i + 1}` })) },
      { name: 'mini', description: '快速：3 句常用台词', default: false, candidates: 3, sentences: Array.from({ length: 3 }, (_, i) => ({ id: `m${i}`, text: `快速 ${i + 1}` })) },
    ],
    datasets: [{ id: 'ds-alice', name: 'alice', path: '', character: 'alice', lines: 800, total_minutes: 65 }],
    blind: { total: 0, tests: [] },
    works: { works: [] },
    plan: (input) => planFor(input, world),
  }
  return world
}

export function planFor(input, world) {
  const train = input.kind === 'train'
  const requiredCaps = []
  if (!train && Array.isArray(input.aux_ref_ids) && input.aux_ref_ids.length > 0) requiredCaps.push('aux_refs')
  if (!train && input.fragment_interval != null && Math.abs(Number(input.fragment_interval) - 0.3) > 1e-9) requiredCaps.push('fragment_interval')

  const cand = (device, name, type, seconds, units, extra = {}) => {
    const base = { device, name, type, gpu: type === 'cloud' ? device.replace('colab-', '') : null, seconds, units, speed_source: 'measured', available: true, reason_code: null, reason: null, idle: true, premium: false, wake: false, vms: null, ...extra }
    if (type === 'cloud' && input.cloud_capable === false) {
      return { ...base, available: false, reason_code: 'kind_unsupported' }
    }
    if (type === 'lan' && requiredCaps.length > 0) {
      const dev = world.compute.devices.find((d) => d.id === device)
      const have = new Set(dev?.caps ?? [])
      const missing = requiredCaps.filter((c) => !have.has(c))
      if (missing.length > 0) {
        const labels = missing.map((c) => CAP_LABELS[c] ?? c).join('/')
        return { ...base, available: false, reason_code: 'worker_outdated', reason: `局域网设备版本较旧，请更新后再用${labels}` }
      }
    }
    return base
  }
  const candidates = train
    ? [cand('colab-L4', '云端 L4', 'cloud', 4200, 2.3, { prep_sec: 250 }), cand('colab-T4', '云端 T4', 'cloud', 7200, 2.3, { prep_sec: 250 }), cand('local', 'studio-host', 'local', 32400, null)]
    : [cand('local', 'studio-host', 'local', 60, null), cand('lan-gpu-worker', 'gpu-worker', 'lan', 240, null, { idle: false }), cand('colab-L4', '云端 L4', 'cloud', 420, 0.3, { prep_sec: 360 })]
  const recommended = candidates.find((c) => c.available)?.device ?? candidates[0].device
  const chosen = input.compute ?? recommended
  const chosenCand = candidates.find((c) => c.device === chosen)
  const confirm = []
  if (chosenCand?.type === 'cloud' && chosenCand.units != null) {
    if (world.compute.auto.ask_over === 0 || chosenCand.units > world.compute.auto.ask_over) confirm.push('ask_over')
    if (world.compute.auto.ask_month === 0 || world.compute.month_used + chosenCand.units > world.compute.auto.ask_month) confirm.push('ask_month')
  }
  return {
    recommended, reason_code: train ? 'train_needs_gpu' : 'local_fast_enough', reason: '', chosen, candidates,
    confirm, month_used: world.compute.month_used, ask_over: world.compute.auto.ask_over, ask_month: world.compute.auto.ask_month,
  }
}

export function job(id, kind, status, extra = {}) {
  return {
    id, kind, target: 'alice', status, progress: status === 'succeeded' ? 1 : 0.4, stage: status === 'running' ? '评测中' : '', created: '2026-11-20T10:00:00',
    started: '2026-11-20T10:00:01', finished: status === 'running' ? null : '2026-11-20T10:01:00', error: null, log_tail: [], completed: [], ...extra,
  }
}

export const weight = (id, type, extra) => ({
  id, name: `${id}.${type === 'gpt' ? 'ckpt' : 'pth'}`, engine: 'v4', type, character: 'alice', source: 'user', epoch: type === 'gpt' ? 15 : 8, step: null, job_id: 'run-1120',
  run_name: 'run-1120', dataset_id: 'ds-alice', filter: null, lines_used: 800, minutes_used: 65, created_at: '2026-11-18T09:00:00', size_bytes: type === 'gpt' ? 155e6 : 85e6,
  path: '', sha256: '', display_name: null, note: '', hidden: false, default_for: ['alice'], is_default: true, deletable: true,
  evals: [
    { gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth', n: 36, cer_median: 0, spk_sim_median: 0.82, score_median: 94.6, eval_set: 'hard', character: 'alice', ref_id: 'alice-ref-1', job_id: 'e1', evaluated_at: '2026-11-18T10:00:00' },
    { gpt: 'v4-base.ckpt', sovits: 'v4-base.pth', n: 36, cer_median: 0.04, spk_sim_median: 0.71, score_median: 86.2, eval_set: 'hard', character: 'alice', ref_id: 'alice-ref-1', job_id: 'e2', evaluated_at: '2026-11-17T10:00:00' },
  ],
  best_eval: { gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth', n: 36, cer_median: 0, spk_sim_median: 0.82, score_median: 94.6 }, ...extra,
})

export function candidate(id, score, text, extra = {}) {
  return {
    id, created: '2026-11-20T10:00:00', audio_url: `/audio/${id}`, mel_url: null, text: 'おはよう。今日はいい天気だね。', text_lang: 'ja', character: 'alice', engine: 'v4',
    gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth', ref_id: 'alice-ref-1', seed: 1234567 + score, duration: 2.6, infer_sec: 1.2, synth_wall_sec: 1.4, semantic_tokens: 120,
    top_k: 15, top_p: 1, temperature: 1, repetition_penalty: 1.35, speed: 1, fragment_interval: 0.3, text_split_method: 'cut5', sample_steps: null, aux_ref_ids: [],
    vram_peak_mb: 0, ratio_vs_ref: [], asr_text: text, cer: 0, cer_kana: 0, spk_sim: 0.82, score, segments: null, work_id: null, compute: 'local', review: null,
    ...extra,
  }
}

export async function mockCore(page, { world = makeWorld(), overrides = {} } = {}) {
  const unexpected = []
  const posts = []
  const getPrefs = (slug) => {
    if (!world.prefs[slug]) world.prefs[slug] = { slug, default_ref: null, synth_params: {}, last_ref_id: null, aux_ref_ids: [] }
    return world.prefs[slug]
  }
  const validateRef = (slug, rid, requirePrimary = false) => {
    const found = world.refs.find((r) => r.id === rid)
    if (!found) return null
    if (found.character !== slug) return `参考 ${rid} 属于 ${found.character}，不属于 ${slug}`
    if (requirePrimary && (found.duration < 3 || found.duration > 10)) return `默认主参考时长需在 3–10 秒之间（当前 ${found.duration} 秒）`
    return null
  }
  const patchPrefs = (slug, body) => {
    if (body.default_ref) {
      const err = validateRef(slug, body.default_ref, true)
      if (err) return json({ detail: err }, 422)
    }
    if (body.last_ref_id) {
      const err = validateRef(slug, body.last_ref_id, false)
      if (err) return json({ detail: err }, 422)
    }
    if (Array.isArray(body.aux_ref_ids)) {
      for (const rid of body.aux_ref_ids) {
        if (!rid) continue
        const err = validateRef(slug, rid, false)
        if (err) return json({ detail: err }, 422)
      }
    }
    const cur = getPrefs(slug)
    const next = { ...cur, synth_params: { ...(cur.synth_params ?? {}) }, aux_ref_ids: [...(cur.aux_ref_ids ?? [])] }
    if ('default_ref' in body) next.default_ref = body.default_ref || null
    if ('last_ref_id' in body) next.last_ref_id = body.last_ref_id || null
    if ('aux_ref_ids' in body) next.aux_ref_ids = Array.isArray(body.aux_ref_ids) ? [...new Set(body.aux_ref_ids.filter(Boolean))] : []
    if (body.reset_synth_params) next.synth_params = {}
    if ('synth_params' in body) {
      if (body.synth_params === null) next.synth_params = {}
      else {
        for (const [k, v] of Object.entries(body.synth_params ?? {})) {
          if (v === null) delete next.synth_params[k]
          else next.synth_params[k] = v
        }
      }
    }
    world.prefs[slug] = next
    const ch = world.models.characters.find((c) => c.slug === slug)
    if (ch) { ch.default_ref = next.default_ref; ch.prefs = next }
    return json(next)
  }
  const patchWeight = (id, body) => {
    const w = world.weights.find((x) => x.id === id)
    if (!w) return json({ detail: `权重 ${id} 不存在` }, 404)
    if ('display_name' in body) w.display_name = body.display_name || null
    if ('note' in body) w.note = body.note ?? ''
    if ('hidden' in body) w.hidden = !!body.hidden
    if (body.is_default === true) {
      const charSlug = body.character ?? w.character
      for (const other of world.weights) {
        if (other.engine === w.engine && other.type === w.type && (!other.character || other.character === charSlug)) {
          other.is_default = other.id === w.id
        }
      }
      const ch = world.models.characters.find((c) => c.slug === charSlug)
      if (ch && ch.engines[w.engine]) {
        if (w.type === 'gpt') ch.engines[w.engine].default_gpt = w.name
        else ch.engines[w.engine].default_sovits = w.name
        const engDef = ch.engines[w.engine]
        const vs = world.versions[charSlug]
        if (vs) {
          const matched = vs.find((v) => v.engine === w.engine && v.gpt === engDef.default_gpt && v.sovits === engDef.default_sovits)
          if (matched) {
            for (const v of vs) v.recommended = v.id === matched.id
          }
        }
      }
    }
    return json(w)
  }

  const table = {
    'GET /health': () => json(world.health),
    'GET /models': () => json(world.models),
    'GET /jobs': (req, w, url) => {
      const kind = url.searchParams.get('kind')
      const jobs = world.jobs.filter((j) => !kind || j.kind === kind)
      return json({ jobs, total: jobs.length })
    },
    'GET /compute': () => json(world.compute),
    'GET /cloud': () => json(world.cloud),
    'GET /library': () => json(world.library),
    'GET /system/info': () => json(world.systemInfo),
    'GET /system/versions': () => json(world.systemVersions),
    'GET /system/storage': (req, w, u) => {
      if (w.storage === null) return json({ detail: 'Not Found' }, 404)
      if (u.searchParams.get('refresh') === 'true') {
        w.storage = { ...w.storage, measured_at: new Date().toISOString() }
      }
      return json(w.storage)
    },
    'GET /refs': (req, w, url) => {
      const ch = url.searchParams.get('character')
      const refs = ch ? world.refs.filter((r) => r.character === ch) : world.refs
      return json({ count: refs.length, refs })
    },
    'GET /weights': () => json({ total: world.weights.length, weights: world.weights }),
    'GET /eval-sets': () => json({ sets: world.evalSets }),
    'GET /datasets': () => json({ datasets: world.datasets }),
    'GET /blind': () => json(world.blind),
    'GET /works': () => json(world.works),
    'GET /history': () => json({ total: 0, items: [] }),
    'GET /reviews/stats': () => json({ overall: { n: 0, avg_rating: null, good_ratio: 0, avg_score: null, tag_ratios: {} }, dimensions: [] }),
    'POST /plan': (req) => json(world.plan(req.postDataJSON() ?? {})),
    'POST /train/sweep/plan': (req) => json({ plan: world.plan({ ...(req.postDataJSON() ?? {}), kind: 'train' }), groups: [] }),
    'PUT /compute/auto': (req) => { world.compute.auto = { ...world.compute.auto, ...req.postDataJSON() }; return json(world.compute.auto) },
  }
  await page.route('**/*', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (url.pathname === '/ui' || url.pathname.startsWith('/ui/') || url.pathname.startsWith('/@') || url.pathname.startsWith('/node_modules/') || url.pathname.startsWith('/src/')) return route.continue()
    const key = `${req.method()} ${url.pathname}`
    if (req.method() !== 'GET') posts.push({ key, body: req.postData() ? safeJSON(req.postData()) : null })
    const versions = /^\/characters\/([^/]+)\/versions$/.exec(url.pathname)
    const prefsMatch = /^\/characters\/([^/]+)\/prefs$/.exec(url.pathname)
    const weightMatch = /^\/weights\/([^/]+)$/.exec(url.pathname)
    const audioMatch = /^\/audio\/([^/]+)$/.exec(url.pathname)
    const refAudioMatch = /^\/refs\/([^/]+)\/audio$/.exec(url.pathname)
    const job = /^\/jobs\/([^/]+)(\/events)?$/.exec(url.pathname)
    const found = job && world.jobs.find((j) => j.id === decodeURIComponent(job[1]))
    const handler = overrides[key] ?? table[key]
      ?? (versions && req.method() === 'GET' ? () => json(world.versions[decodeURIComponent(versions[1])] ?? []) : null)
      ?? (prefsMatch && req.method() === 'GET' ? () => json(getPrefs(decodeURIComponent(prefsMatch[1]))) : null)
      ?? (prefsMatch && req.method() === 'PATCH' ? () => patchPrefs(decodeURIComponent(prefsMatch[1]), req.postDataJSON() ?? {}) : null)
      ?? (weightMatch && req.method() === 'PATCH' ? () => patchWeight(decodeURIComponent(weightMatch[1]), req.postDataJSON() ?? {}) : null)
      ?? ((audioMatch || refAudioMatch) && req.method() === 'GET' ? () => ({ binary: Buffer.from('RIFFmockaudio'), contentType: 'audio/wav' }) : null)
      ?? (found && req.method() === 'GET' ? () => (job[2] ? { sse: `data: ${JSON.stringify(found)}\n\n` } : json(found)) : null)
    if (!handler) {
      unexpected.push(key)
      return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }) })
    }
    const result = await handler(req, world, url)
    if (result === 'abort') return route.abort()
    if (result.sse) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: result.sse })
    if (result.binary) return route.fulfill({ status: result.status ?? 200, contentType: result.contentType ?? 'application/octet-stream', body: result.binary })
    return route.fulfill({ status: result.status ?? 200, contentType: 'application/json', headers: result.headers, body: JSON.stringify(result.body ?? {}) })
  })
  return { world, unexpected, posts }
}

function safeJSON(text) { try { return JSON.parse(text) } catch { return text } }
