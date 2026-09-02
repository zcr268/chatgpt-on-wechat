import { create } from 'zustand'
import apiClient from '../api/client'
import { ownerOf, readActiveSessionId } from './sessionOwners'
import type { AgentProfile, ChannelInstanceRecord, RosterSnapshot } from '../types'

/**
 * The team roster and the currently selected Agent, mirroring the web console's
 * `agentCatalog` / `activeAgentId` / `defaultAgentId`.
 *
 * Robustness is the whole point of this store. The desktop client must keep
 * working for single-Agent installs that never opt into a team, and a broken or
 * legacy backend must never block startup or chat. So every load is best-effort:
 * on any failure the store simply stays in its single-Agent shape (empty
 * roster, no active id, multiAgent=false) and the rest of the UI renders exactly
 * as it did before this feature existed.
 *
 * "Multi-Agent mode" is derived, not configured: the team affordances (Agents
 * page, channel binding, the composer Agent picker) light up only when the
 * roster actually holds more than one enabled Agent. A fresh install with a
 * single synthesized default Agent looks and behaves like the old client.
 */

const ACTIVE_KEY = 'cow_active_agent'

interface AgentStore {
  /** All Agents in the roster (enabled and disabled), newest snapshot. */
  agents: AgentProfile[]
  /** The backend's default Agent id; the fallback target for everything. */
  defaultAgentId: string
  /** The Agent the console currently acts as. Always a valid enabled id, or ''. */
  activeAgentId: string
  /** Channel bindings from the roster (team.json), for the Channels page. */
  channelInstances: ChannelInstanceRecord[]
  /** Optimistic-locking token echoed back on roster writes. */
  revision: string
  /** True once the first fetch has resolved (success or failure). */
  loaded: boolean

  /** Fetch the roster. Never throws; degrades to single-Agent on error. */
  refresh: () => Promise<void>
  /** Switch the active Agent (persisted, validated, wired into the api client). */
  setActive: (id: string) => void
  /** Run a roster POST action, carrying the current revision and refreshing on
   *  success. Retries once on a stale-revision race. Returns the raw result so
   *  callers can read messages / codes. */
  mutate: (body: Record<string, unknown>) => Promise<{ ok: boolean; message?: string; code?: string }>
}

// Only the enabled Agents are user-selectable; disabled ones stay in the list
// for the Agents page but can't be the active conversation target.
function enabledAgents(agents: AgentProfile[]): AgentProfile[] {
  return agents.filter((a) => a.enabled)
}

// Push the effective active id into the api client so scoped endpoints carry
// it. In single-Agent mode we send an empty id, which the client treats as
// "omit agent_id" — byte-for-byte the legacy requests.
function syncClient(activeId: string, multiAgent: boolean) {
  apiClient.setActiveAgentId(multiAgent ? activeId : '')
}

export const useAgentStore = create<AgentStore>((set, get) => ({
  agents: [],
  defaultAgentId: '',
  activeAgentId: '',
  channelInstances: [],
  revision: '',
  loaded: false,

  refresh: async () => {
    let snap: RosterSnapshot | null = null
    try {
      snap = await apiClient.getAgents()
    } catch {
      // Legacy/broken backend or network hiccup: stay single-Agent. Mark loaded
      // so the UI stops waiting, but keep the roster empty so nothing lights up.
      syncClient('', false)
      set({ loaded: true })
      return
    }
    if (!snap || snap.status === 'error' || !Array.isArray(snap.agents)) {
      syncClient('', false)
      set({ loaded: true })
      return
    }

    const agents = snap.agents
    const defaultAgentId = snap.default_agent_id || agents[0]?.id || ''
    const enabled = enabledAgents(agents)
    const multiAgent = enabled.length > 1

    // Resolve the active id. The active Agent is the owner of the open
    // conversation, so when a session is being restored on launch its owner
    // wins — otherwise the history would be fetched from the wrong Agent's
    // store and come back empty. A restored session with no recorded owner is
    // a pre-team conversation, and those all live with the default Agent. Only
    // with no session to restore does the last persisted choice apply. Never
    // point at a vanished/disabled Agent (deleting the active Agent must not
    // strand the console).
    let active = ''
    try {
      const restoring = readActiveSessionId()
      active = restoring
        ? ownerOf(restoring) || defaultAgentId
        : localStorage.getItem(ACTIVE_KEY) || ''
    } catch {
      /* localStorage unavailable */
    }
    if (!enabled.some((a) => a.id === active)) {
      active = defaultAgentId
      try {
        localStorage.setItem(ACTIVE_KEY, active)
      } catch {
        /* ignore */
      }
    }

    syncClient(active, multiAgent)
    set({
      agents,
      defaultAgentId,
      activeAgentId: active,
      channelInstances: Array.isArray(snap.channel_instances) ? snap.channel_instances : [],
      revision: snap.revision || '',
      loaded: true,
    })
  },

  setActive: (id) => {
    const { agents, activeAgentId } = get()
    if (!id || id === activeAgentId) return
    // Refuse to activate an id that isn't an enabled Agent.
    if (!enabledAgents(agents).some((a) => a.id === id)) return
    try {
      localStorage.setItem(ACTIVE_KEY, id)
    } catch {
      /* ignore */
    }
    syncClient(id, enabledAgents(agents).length > 1)
    set({ activeAgentId: id })
  },

  mutate: async (body) => {
    const send = async (retried: boolean): Promise<{ ok: boolean; message?: string; code?: string }> => {
      try {
        const res = await apiClient.agentAction({ revision: get().revision, ...body })
        if (res.status === 'success') {
          await get().refresh()
          return { ok: true }
        }
        const code = res.code as string | undefined
        // Two quick edits can race: the second still carried the pre-first
        // revision. Re-sync and retry once so a fast click just works.
        if (code === 'stale_roster' && !retried) {
          await get().refresh()
          return send(true)
        }
        return { ok: false, message: res.message as string | undefined, code }
      } catch (e) {
        return { ok: false, message: e instanceof Error ? e.message : String(e) }
      }
    }
    return send(false)
  },
}))

/** True when the install is running a team (more than one enabled Agent). */
export function isMultiAgent(): boolean {
  const { agents } = useAgentStore.getState()
  return enabledAgents(agents).length > 1
}

/** React-friendly selector for multi-Agent mode. */
export function selectMultiAgent(s: AgentStore): boolean {
  return s.agents.filter((a) => a.enabled).length > 1
}

/** Look up an Agent profile by id (any state), or undefined. */
export function findAgent(id: string): AgentProfile | undefined {
  return useAgentStore.getState().agents.find((a) => a.id === id)
}

/** The enabled Agents, in roster order. */
export function useEnabledAgents(): AgentProfile[] {
  return useAgentStore((s) => s.agents.filter((a) => a.enabled))
}

/** The enabled Agents with the default one first — the order every picker
 *  (composer, new chat, scope selectors) lists them in. */
export function enabledDefaultFirst(agents: AgentProfile[], defaultAgentId: string): AgentProfile[] {
  const enabled = enabledAgents(agents)
  const def = enabled.find((a) => a.id === defaultAgentId)
  return def ? [def, ...enabled.filter((a) => a.id !== defaultAgentId)] : enabled
}
