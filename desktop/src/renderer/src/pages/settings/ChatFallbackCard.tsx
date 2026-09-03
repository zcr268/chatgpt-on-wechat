import React, { useMemo, useState } from 'react'
import { Loader2, ShieldAlert } from 'lucide-react'
import { t } from '../../i18n'
import type { ChatFallbackCapabilityState, ModelsData } from '../../types'
import { Card, Field, Dropdown, TextInput, Toggle, type DropdownOption } from './primitives'
import { resolveModels, providerLabel } from './modelsHelpers'

// Backup chat model. Unlike the other capability cards this one is opt-in:
// it sits idle until the primary model fails a turn for good, so the whole
// card is gated behind an "enabled" toggle and validates provider + model
// together. An incomplete entry is never allowed to hijack a healthy setup.

export interface ChatFallbackCardProps {
  state: ChatFallbackCapabilityState | undefined
  data: ModelsData | null
  busy?: boolean
  status?: string
  onSave: (payload: {
    providerId: string
    model: string
    enabled: boolean
    maxSwitches: number
  }) => void
}

const ChatFallbackCard: React.FC<ChatFallbackCardProps> = ({ state, data, busy, status, onSave }) => {
  const [enabled, setEnabled] = useState(!!state?.enabled)
  const [provider, setProvider] = useState(state?.current_provider || '')
  const [model, setModel] = useState(state?.current_model || '')
  const [customModel, setCustomModel] = useState(
    (state?.current_provider || '').startsWith('custom:') ? state?.current_model || '' : ''
  )
  const [showCustom, setShowCustom] = useState(false)
  const [maxSwitches, setMaxSwitches] = useState(String(state?.max_switches ?? 1))

  const isCustomProvider = provider.startsWith('custom:')

  const isConfigured = (id: string): boolean => {
    const p = data?.providers?.find((x) => x.id === id)
    if (!p) return true
    return p.configured || (p.is_custom && !!p.custom_name)
  }

  const providerOptions: DropdownOption[] = useMemo(() => {
    const opts = (state?.providers || [])
      .filter((id) => isConfigured(id) || id === provider)
      .map((id) => ({ value: id, label: providerLabel(data, id) }))
    return [{ value: '', label: t('models_select_provider') }, ...opts]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.providers, data, provider])

  const modelOptions: DropdownOption[] = useMemo(() => {
    const list = resolveModels(data, provider, state?.provider_models).map((o) => ({
      value: o.value,
      label: o.value,
      hint: o.hint,
    }))
    if (model && !showCustom && !list.some((o) => o.value === model)) {
      list.unshift({ value: model, label: model, hint: undefined })
    }
    return list
  }, [data, state?.provider_models, provider, model, showCustom])

  const handleProvider = (id: string) => {
    setProvider(id)
    setShowCustom(false)
    if (id.startsWith('custom:')) {
      setCustomModel(id === state?.current_provider ? state?.current_model || '' : '')
      setModel('')
      return
    }
    setCustomModel('')
    setModel(resolveModels(data, id, state?.provider_models)[0]?.value || '')
  }

  const finalModel = showCustom || isCustomProvider ? customModel.trim() : model
  // Enabling is all-or-nothing; the backend rejects a half-filled entry too.
  const incomplete = enabled && (!provider || !finalModel)

  const primaryLabel = [state?.primary_provider, state?.primary_model]
    .filter(Boolean)
    .map((s) => s as string)
    .join(' / ')

  return (
    <Card
      icon={<ShieldAlert size={16} />}
      title={t('models_cap_chat_fallback')}
      subtitle={t('models_cap_chat_fallback_sub')}
    >
      <div className="space-y-4">
        {/* Toggle has no built-in label, so render the text beside it. */}
        <div className="flex items-center gap-2.5">
          <Toggle checked={enabled} onChange={setEnabled} />
          <span className="text-sm text-content-secondary">{t('models_chat_fallback_enable')}</span>
        </div>

        {!enabled && (
          <p className="text-xs text-content-tertiary">
            {primaryLabel
              ? t('models_chat_fallback_off_hint').replace('{model}', primaryLabel)
              : t('models_chat_fallback_off_hint_plain')}
          </p>
        )}

        {enabled && (
          <>
            <Field label={t('models_provider')}>
              <Dropdown
                value={provider}
                options={providerOptions}
                placeholder={t('models_select_provider')}
                onChange={handleProvider}
              />
              {!!provider && !isConfigured(provider) && (
                <p className="text-xs text-danger mt-1.5">{t('config_provider_unconfigured_hint')}</p>
              )}
            </Field>

            <Field label={t('models_model')}>
              {isCustomProvider ? (
                <TextInput
                  className="font-mono"
                  value={customModel}
                  onChange={(e) => setCustomModel(e.target.value)}
                  placeholder={t('config_custom_model_hint')}
                />
              ) : (
                <Dropdown
                  value={model}
                  options={modelOptions}
                  placeholder={t('models_select_model')}
                  onChange={setModel}
                  disabled={!provider}
                />
              )}
            </Field>

            <Field label={t('models_chat_fallback_max_switches')} hint={t('models_chat_fallback_max_switches_hint')}>
              <TextInput
                className="font-mono"
                value={maxSwitches}
                onChange={(e) => setMaxSwitches(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="1"
              />
            </Field>

            {incomplete && (
              <p className="text-xs text-danger">{t('models_chat_fallback_incomplete')}</p>
            )}
          </>
        )}

        <div className="flex items-center justify-end gap-3 pt-1">
          <span className={`text-xs text-accent transition-opacity ${status ? 'opacity-100' : 'opacity-0'}`}>
            {status}
          </span>
          <button
            disabled={busy || incomplete}
            onClick={() =>
              onSave({
                providerId: provider,
                model: finalModel,
                enabled,
                maxSwitches: Math.max(1, Math.min(5, parseInt(maxSwitches || '1', 10) || 1)),
              })
            }
            className="px-4 py-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover text-sm font-medium cursor-pointer transition-colors disabled:opacity-50 inline-flex items-center gap-2"
          >
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t('config_save')}
          </button>
        </div>
      </div>
    </Card>
  )
}

export default ChatFallbackCard
