import React, { useState } from 'react'
import apiClient from '../api/client'
import type { AgentProfile } from '../types'
import { useAgentStore, findAgent } from '../store/agentStore'

/**
 * An Agent's face: its uploaded image when it has one, else the product's own
 * logo — exactly like the web console's `agentAvatarHTML`, so a fresh Agent and
 * a solo chat show the same CowAgent avatar the bubble has always shown instead
 * of a generated initial.
 *
 * A null agent means the id no longer resolves — a conversation pinned to a
 * since-deleted Agent. Fall back to the default Agent's face rather than the
 * bare logo, so the deleted Agent visibly degrades to the default one.
 */

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
  const defaultAgentId = useAgentStore((s) => s.defaultAgentId)
  const [failed, setFailed] = useState(false)

  // Pinned to a deleted Agent: degrade to the default Agent's face.
  if (!agent && defaultAgentId) {
    agent = findAgent(defaultAgentId) ?? null
  }

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

  return (
    <img
      src="./logo.jpg"
      alt={agent?.name || agent?.id || 'Agent'}
      draggable={false}
      style={px}
      className={`${radius} object-cover flex-shrink-0 ${className}`}
    />
  )
}

export default AgentAvatar
