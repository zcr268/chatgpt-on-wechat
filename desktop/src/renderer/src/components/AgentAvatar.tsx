import React, { useState } from 'react'
import apiClient from '../api/client'
import type { AgentProfile } from '../types'
import { useAgentStore } from '../store/agentStore'

/**
 * An Agent's face: its uploaded image when it has one, else the product's own
 * logo — exactly like the web console's `agentAvatarHTML`, so a fresh Agent and
 * a solo chat show the same CowAgent avatar the bubble has always shown instead
 * of a generated initial.
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
