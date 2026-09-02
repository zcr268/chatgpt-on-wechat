import React, { useState } from 'react'
import apiClient from '../api/client'
import type { AgentProfile } from '../types'
import { useAgentStore } from '../store/agentStore'

/**
 * An Agent's face: its uploaded image when it has one, else a colored disc with
 * the first character of its name. Mirrors the web console's `agentAvatarHTML`,
 * but the fallback here is a generated initial (the web console falls back to a
 * shared logo) so a team of Agents is visually distinguishable at a glance.
 */

// A stable, pleasant color per Agent id. Deterministic so the same Agent always
// gets the same disc across the app and across restarts.
const PALETTE = [
  '#4abe6e', '#3b82f6', '#a855f7', '#f59e0b', '#ef4444',
  '#0ea5e9', '#ec4899', '#14b8a6', '#6366f1', '#f97316',
]

function colorFor(id: string): string {
  let hash = 0
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0
  return PALETTE[hash % PALETTE.length]
}

function initial(name: string): string {
  const ch = (name || '').trim()[0]
  return ch ? ch.toUpperCase() : '?'
}

interface AgentAvatarProps {
  agent?: Pick<AgentProfile, 'id' | 'name' | 'avatar'> | null
  size?: number
  className?: string
  // 'circle' (default) for roster/composer faces; 'square' for chat bubbles,
  // matching the rounded-square logo the assistant bubble used before.
  shape?: 'circle' | 'square'
}

const AgentAvatar: React.FC<AgentAvatarProps> = ({ agent, size = 32, className = '', shape = 'circle' }) => {
  const radius = shape === 'square' ? 'rounded-lg' : 'rounded-full'
  // Bust the <img> cache when the roster revision changes (an avatar was
  // replaced). Reading it here also re-renders faces after an upload.
  const revision = useAgentStore((s) => s.revision)
  const [failed, setFailed] = useState(false)

  const hasImage = !!agent && agent.avatar === 'image' && !failed
  const px = { width: size, height: size }

  if (hasImage && agent) {
    return (
      <img
        src={apiClient.agentAvatarUrl(agent.id, revision || agent.id)}
        alt={agent.name || agent.id}
        draggable={false}
        onError={() => setFailed(true)}
        style={px}
        className={`${radius} object-cover flex-shrink-0 ${className}`}
      />
    )
  }

  const id = agent?.id || ''
  return (
    <span
      style={{ ...px, backgroundColor: colorFor(id), fontSize: Math.round(size * 0.42) }}
      className={`${radius} flex items-center justify-center flex-shrink-0 font-semibold text-white select-none ${className}`}
    >
      {initial(agent?.name || id)}
    </span>
  )
}

export default AgentAvatar
