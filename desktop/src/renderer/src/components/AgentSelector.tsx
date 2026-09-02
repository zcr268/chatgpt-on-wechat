import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, Plus, X } from 'lucide-react'
import { t } from '../i18n'
import ComposerChip from './ComposerChip'
import AgentAvatar from './AgentAvatar'
import { useAgentStore, enabledDefaultFirst } from '../store/agentStore'
import { useSessionStore } from '../store/sessionStore'
import { useChatStore } from '../store/chatStore'
import { useSessionSettingsStore, selectSharedConversation } from '../store/sessionSettingsStore'
import { startNewChat } from '../lib/newChat'

interface AgentSelectorProps {
  sessionId: string
}

/**
 * The composer chip that shows who the conversation talks to, and lets the
 * user change that. Only rendered in multi-Agent mode (ChatInput gates it).
 *
 * Mirrors the web console's composer identity menu:
 *  - Switch the current Agent. A conversation is stored with its owner, so on
 *    an empty chat this simply re-owns it; once there are messages, switching
 *    starts a clean conversation owned by the chosen Agent (rewriting the owner
 *    of an existing history is not possible — the history lives in the first
 *    owner's store). The switch list is hidden once the chat is a group: the
 *    sensible actions there are adding and removing members.
 *  - Add teammates to the current conversation (a group chat). Members share
 *    the context and can be addressed with a leading @name; the owner keeps
 *    receiving everything else and may hand turns to them.
 *  - Create a new Agent, so the team feature is discoverable from the composer.
 */
const AgentSelector: React.FC<AgentSelectorProps> = ({ sessionId }) => {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const agents = useAgentStore((s) => s.agents)
  const activeAgentId = useAgentStore((s) => s.activeAgentId)
  const defaultAgentId = useAgentStore((s) => s.defaultAgentId)
  const shared = useSessionSettingsStore(selectSharedConversation)
  const team = useSessionSettingsStore((s) => (s.sessionId === sessionId ? s.cfg?.team : undefined))
  const addMember = useSessionSettingsStore((s) => s.addMember)
  const removeMember = useSessionSettingsStore((s) => s.removeMember)
  const hasMessages = useChatStore((s) => (s.sessions[sessionId]?.messages.length ?? 0) > 0)

  const roster = enabledDefaultFirst(agents, defaultAgentId)
  const active = agents.find((a) => a.id === activeAgentId) || roster[0] || null
  const members = (team?.members || []).filter((m) => m.id !== activeAgentId)
  const memberIds = new Set(members.map((m) => m.id))
  const invitable = roster.filter((a) => a.id !== activeAgentId && !memberIds.has(a.id))

  const pick = (agentId: string) => {
    setOpen(false)
    if (!agentId || agentId === activeAgentId) return
    // Only a true draft may change hands. The in-memory message count alone
    // isn't proof of that (history may still be loading), so also require the
    // session list to know nothing persisted for it — otherwise the same
    // session id would end up in two Agents' stores.
    const listed = useSessionStore.getState().sessions.find((s) => s.session_id === sessionId)
    const isDraft = !hasMessages && (!listed || !listed.msg_count)
    if (!isDraft) {
      // The history belongs to the current owner; start fresh for the new one.
      useAgentStore.getState().setActive(agentId)
      startNewChat({ ownerId: agentId, inheritProject: false })
      return
    }
    // Nothing persisted yet: the draft conversation just changes hands.
    useAgentStore.getState().setActive(agentId)
    useSessionStore.getState().setOwner(sessionId, agentId)
    void useSessionSettingsStore.getState().refresh(sessionId)
  }

  const tip = `${t('composer_agent_tip')}${active?.name ? ` · ${active.name}` : ''}${
    members.length ? ` +${members.length}` : ''
  }`

  return (
    <ComposerChip
      icon={
        <span className="relative inline-flex">
          <AgentAvatar agent={active} size={18} />
          {members.length > 0 && (
            <span className="absolute -right-1.5 -bottom-1 min-w-[13px] h-[13px] px-0.5 rounded-full bg-accent text-white text-[9px] font-semibold leading-[13px] text-center ring-2 ring-surface">
              {members.length + 1}
            </span>
          )}
        </span>
      }
      label={active?.name || t('agents_title')}
      tip={tip}
      open={open}
      onToggle={() => setOpen((v) => !v)}
      onClose={() => setOpen(false)}
      align="end"
      menuClassName="w-64"
      labelHidden
    >
      {!shared && (
        <>
          <div className="px-2 py-1.5 text-[11px] font-medium text-content-tertiary uppercase tracking-wide">
            {t('composer_agent_heading')}
          </div>
          {roster.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => pick(a.id)}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[13px] cursor-pointer transition-colors ${
                a.id === activeAgentId ? 'bg-accent-soft text-accent font-medium' : 'text-content-secondary hover:bg-surface-2'
              }`}
            >
              <AgentAvatar agent={a} size={20} />
              <span className="flex-1 min-w-0 text-left truncate">{a.name || a.id}</span>
              {a.id === defaultAgentId && (
                <span className="px-1.5 py-0.5 rounded-full text-[10px] bg-amber-500/10 text-amber-600 flex-shrink-0">
                  {t('channel_team_default')}
                </span>
              )}
              {a.id === activeAgentId && <Check size={13} className="flex-shrink-0" />}
            </button>
          ))}
        </>
      )}

      {/* Members already in the conversation: the owner plus each teammate. A
          plain row (a ✓ marks "in the chat"), not a standing highlight, which
          read as everything being selected. Hover a teammate to remove it; the
          owner can't be removed. */}
      {shared && (
        <>
          <div className="px-2 py-1.5 text-[11px] font-medium text-content-tertiary uppercase tracking-wide">
            {t('team_members')}
          </div>
          {active && (
            <div className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[13px] text-content-secondary">
              <AgentAvatar agent={active} size={20} />
              <span className="flex-1 min-w-0 text-left truncate">{active.name || active.id}</span>
              <span className="px-1.5 py-0.5 rounded-full text-[10px] bg-surface-2 text-content-tertiary flex-shrink-0">
                {t('composer_agent_owner')}
              </span>
            </div>
          )}
          {members.map((m) => (
            <button
              key={m.id}
              type="button"
              title={t('team_remove')}
              onClick={() => void removeMember(sessionId, m.id)}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[13px] text-content-secondary cursor-pointer transition-colors hover:bg-danger-soft hover:text-danger group"
            >
              <AgentAvatar agent={m} size={20} />
              <span className="flex-1 min-w-0 text-left truncate">{m.name || m.id}</span>
              <Check size={13} className="flex-shrink-0 text-content-tertiary group-hover:hidden" />
              <X size={13} className="flex-shrink-0 hidden group-hover:block" />
            </button>
          ))}
        </>
      )}

      {/* Agents that can still be pulled into the conversation. In a solo chat
          this section is what turns it into a group; in a group it lists the
          remaining invitable teammates. */}
      {invitable.length > 0 && (
        <>
          <div className="my-1 h-px bg-default" />
          <div className="px-2 py-1.5 text-[11px] font-medium text-content-tertiary uppercase tracking-wide">
            {t('team_invite')}
          </div>
          {invitable.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => void addMember(sessionId, a.id)}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[13px] text-content-secondary hover:bg-surface-2 cursor-pointer transition-colors"
            >
              <AgentAvatar agent={a} size={20} />
              <span className="flex-1 min-w-0 text-left truncate">{a.name || a.id}</span>
              <Plus size={13} className="flex-shrink-0 text-content-tertiary" />
            </button>
          ))}
        </>
      )}

      <div className="my-1 h-px bg-default" />
      <button
        type="button"
        onClick={() => {
          setOpen(false)
          navigate('/agents?create=1')
        }}
        className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[13px] text-content-secondary hover:bg-surface-2 cursor-pointer transition-colors"
      >
        <span className="w-5 h-5 rounded-full border border-dashed border-strong text-content-tertiary flex items-center justify-center flex-shrink-0">
          <Plus size={11} />
        </span>
        <span className="flex-1 min-w-0 text-left truncate">{t('agents_create')}</span>
      </button>
    </ComposerChip>
  )
}

export default AgentSelector
