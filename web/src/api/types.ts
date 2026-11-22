import type { components } from './schema'

type S = components['schemas']

export type AudioFormat = 'wav' | 'mp3' | 'flac'
export type LoraRank = 16 | 32 | 64 | 128
export type SplitMethod = 'cut0' | 'cut1' | 'cut2' | 'cut3' | 'cut4' | 'cut5'
export type TextLang =
  | 'ja' | 'zh' | 'en' | 'ko' | 'yue' | 'auto' | 'auto_yue'
  | 'all_ja' | 'all_zh' | 'all_ko' | 'all_yue'

export interface CharacterSynthParams {
  speed?: number | null
  fragment_interval?: number | null
  top_k?: number | null
  top_p?: number | null
  temperature?: number | null
  repetition_penalty?: number | null
  text_split_method?: SplitMethod | null
  sample_steps?: number | null
  text_lang?: TextLang | null
}

export interface CharacterPrefs {
  slug: string
  default_ref?: string | null
  synth_params: CharacterSynthParams
  last_ref_id?: string | null
  aux_ref_ids: string[]
}

export interface CharacterPrefsPatch {
  default_ref?: string | null
  synth_params?: CharacterSynthParams | null
  reset_synth_params?: boolean
  last_ref_id?: string | null
  aux_ref_ids?: string[] | null
}

export interface TrainDefaults {
  sovits_epochs: number
  sovits_save_every: number
  sovits_batch: number
  gpt_epochs: number
  gpt_save_every: number
  gpt_batch: number
  lora: boolean
  lora_rank?: LoraRank | null
  lora_ranks: number[]
}

export type AudioItem = S['AudioItem'] & {
  fragment_interval?: number | null
  aux_ref_ids?: string[]
}
export type AudioPage = Omit<S['AudioPage'], 'items'> & { items: AudioItem[] }
export type CandidatesParams = Omit<S['CandidatesParams'], 'format'> & {
  format: AudioFormat
  fragment_interval: number
  aux_ref_ids: string[]
}
export type CompareParams = Omit<S['CompareParams'], 'format'> & {
  format: AudioFormat
  fragment_interval: number
  aux_ref_ids: string[]
}
export type CharacterInfo = Omit<S['CharacterInfo'], 'default_ref'> & {
  default_ref?: string | null
  prefs?: CharacterPrefs | null
}
export type ComboEval = S['ComboEval']
export type DatasetInfo = S['DatasetInfo']
export type DatasetList = S['DatasetList']
export type DatasetPreview = S['DatasetPreview']
export type EngineWeights = S['EngineWeights']
export type Health = S['Health']
export type Job = S['Job']
export type JobList = S['JobList']
export type Engine = NonNullable<CandidatesParams['engine']>
export type EngineInfo = S['EngineInfo'] & {
  supports_aux_refs?: boolean
  train_defaults?: TrainDefaults | null
}
export type ModelsResponse = Omit<S['ModelsResponse'], 'characters' | 'engines'> & {
  characters: CharacterInfo[]
  engines?: EngineInfo[]
}
export type Ref = S['Ref']
export type RefList = S['RefList']
export type RenderResult = S['RenderResult']
export type Review = S['Review']
export type ReviewIn = S['ReviewIn']
export type ReviewStats = S['ReviewStats']
export type Segment = S['Segment']
export type StatsGroup = S['StatsGroup']
export type Step = S['Step']
export type TrainEval = S['TrainEval']
export type TrainRequest = S['TrainRequest'] & { lora_rank?: LoraRank | null }
export type WorkLine = Omit<S['WorkLine'], 'candidates'> & {
  fragment_interval?: number
  aux_ref_ids?: string[]
  candidates: AudioItem[]
}
export type WorkLineIn = S['WorkLineIn'] & {
  fragment_interval?: number
  aux_ref_ids?: string[]
}
export type Work = Omit<S['Work'], 'lines'> & { lines: WorkLine[] }
export type WorkList = S['WorkList']
export type WorkSummary = S['WorkSummary']
export type WeightEntry = S['WeightEntry']
export type WeightList = S['WeightList']
export type WeightPatch = S['WeightPatch']
export type ComboScore = S['ComboScore']
export type DeletedWeight = S['DeletedWeight']
export type EvalSet = S['EvalSet']
export type EvalSetList = S['EvalSetList']
export type EvalSetName = EvalSet['name']
export type EvalRequest = S['EvalRequest']
export type BlindCreate = S['BlindCreate']
export type BlindSide = S['BlindSide']
export type BlindCombo = S['BlindCombo']
export type BlindSummary = S['BlindSummary']
export type BlindList = S['BlindList']
export type BlindTest = S['BlindTest']
export type BlindQuestion = S['BlindQuestion']
export type BlindVote = S['BlindVote']
export type BlindResult = S['BlindResult']
export type BlindComboResult = S['BlindComboResult']
export type BlindRevealQuestion = S['BlindRevealQuestion']
export type ModelVersion = S['ModelVersion']
// 算力、资料库、云端 GPU、版本、存储
export type ComputeDevice = S['ComputeDevice'] & {
  caps?: string[]
  warm?: number
  disk_free_bytes?: number | null
  disk_total_bytes?: number | null
  cache_bytes?: number | null
}
export type ComputeList = Omit<S['ComputeList'], 'devices'> & {
  devices: ComputeDevice[]
}
export interface StorageCategory {
  key: string
  label: string
  bytes: number
  local_bytes?: number
  files?: number
}
export interface DiskUsage {
  path: string
  total_bytes: number
  used_bytes: number
  free_bytes: number
}
export interface LibraryStorage {
  selected: boolean
  kind: string | null
  path: string | null
  total_bytes: number
  local_bytes: number
  categories: StorageCategory[]
}
export interface LocalStorage {
  machine: string
  total_bytes: number
  categories: StorageCategory[]
}
export interface StorageOverview {
  measured_at: string
  disk: DiskUsage
  library: LibraryStorage
  local: LocalStorage
}
export type EngineSpeed = S['EngineSpeed']
export type AutoSettings = S['AutoSettings']
export type PlanRequest = S['PlanRequest'] & {
  character?: string | null
  version?: string | null
  gpt?: string | null
  sovits?: string | null
  ref_id?: string | null
  aux_ref_ids?: string[]
  fragment_interval?: number | null
  cloud_capable?: boolean
}
export type Plan = S['Plan']
export type PlanCandidate = S['PlanCandidate']
export type LibraryInfo = S['LibraryInfo']
export type CloudInfo = S['CloudInfo']
export type CloudAccount = S['CloudAccount']
export type CloudGpu = S['CloudGpu']
export type CloudSettings = S['CloudSettings']
export type CloudLoginStart = S['CloudLoginStart']
export type CloudLoginStatus = S['CloudLoginStatus']
export type WakeConfig = S['WakeConfig']
export type ComputeInvite = S['ComputeInvite']
export type InviteCreate = S['InviteCreate']
export type InviteStatus = S['InviteStatus']
export type InviteCommands = S['InviteCommands']
export type InviteManual = S['InviteManual']
export type SystemInfo = S['SystemInfo']
export type SystemVersions = S['SystemVersions']
export type SweepTrain = S['SweepTrain'] & { lora_rank?: LoraRank | null }
export type SweepRequest = Omit<S['SweepRequest'], 'trains'> & { trains: SweepTrain[] }
export type SweepGroup = S['SweepGroup'] & { lora_rank?: LoraRank | null }
export type SweepPlan = Omit<S['SweepPlan'], 'groups'> & { groups: SweepGroup[] }
