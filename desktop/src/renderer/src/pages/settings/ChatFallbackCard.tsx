import React, { useEffect, useMemo, useState } from 'react'
import { Loader2, ShieldAlert } from 'lucide-react'
import { t } from '../../i18n'
import type { ChatFallbackCapabilityState, ModelsData } from '../../types'
import { Field, Dropdown, TextInput, Toggle, Modal, Btn, type DropdownOption } from './primitives'
import { resolveModels, providerLabel } from './modelsHelpers'

// Backup chat model. Unlike the other capabilities this one is opt-in and
// rarely touched, so it does NOT get its own top-level card: it lives behind a
// small button on the main model card (see ChatFallbackButton) and is edited
// in a modal. It sits idle until the primary model fails a turn for good, so
// the whole form is gated behind an "enabled" toggle and validates provider +
// model together — an incomplete entry is never allowed to hijack a healthy
// setup.

export interface ChatFallbackSavePayload {
  providerId: string
  model: string
  enabled: boolean
  maxSwitches: number
}

export interface ChatFallbackButtonProps {
  state: ChatFallbackCapabilityState | undefined
  data: ModelsData | null
  busy?: boolean
  status?: string
  onSave: (payload: ChatFallbackSavePayload) => void
}

// The small entry point on the main model card header: a shield button that
// opens the modal, plus a subtle badge while the fallback is enabled so an
// active fallback is discoverable at a glance.
export const ChatFallbackButton: React.FC<ChatFallbackButtonProps> = ({ state, data, busy, status, onSave }) => {
  const [open, setOpen] = useState(false)

  // A single entry point that also reflects state: accent-colored + "on" label
  // when the fallback is enabled, muted + "configure" label when off.
  const on = !!state?.enabled
  return (
    <>
      <button
        type="button"
        title={t('models_chat_fallback_button_tip')}
        onClick={() => setOpen(true)}
        className={
          'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn text-xs cursor-pointer transition-colors ' +
          (on
            ? 'text-accent bg-accent-soft hover:bg-accent-soft/70'
            : 'text-content-tertiary hover:text-accent hover:bg-accent-soft/60')
        }
      >
        <ShieldAlert size={12} />
        {on ? t('models_chat_fallback_badge_on') : t('models_chat_fallback_button')}
      </button>
      <ChatFallbackModal
        open={open}
        state={state}
        data={data}
        busy={busy}
        status={status}
        onClose={() => setOpen(false)}
        onSave={onSave}
      />
    </>
  )
}

interface ChatFallbackModalProps extends ChatFallbackButtonProps {
  open: boolean
  onClose: () => void
}

const ChatFallbackModal: React.FC<ChatFallbackModalProps> = ({
  open,
  state,
  data,
  busy,
  status,
  onClose,
  onSave,
}) => {
  const [enabled, setEnabled] = useState(!!state?.enabled)
  const [provider, setProvider] = useState(state?.current_provider || '')
  const [model, setModel] = useState(state?.current_model || '')
  const [customModel, setCustomModel] = useState(
    (state?.current_provider || '').startsWith('custom:') ? state?.current_model || '' : ''
  )
  const [showCustom, setShowCustom] = useState(false)
  const [maxSwitches, setMaxSwitches] = useState(String(state?.max_switches ?? 1))

  // Reset the form to the persisted state each time the modal is (re)opened so
  // a cancelled edit never leaks into the next open.
  useEffect(() => {
    if (!open) return
    setEnabled(!!state?.enabled)
    setProvider(state?.current_provider || '')
    setModel(state?.current_model || '')
    setCustomModel((state?.current_provider || '').startsWith('custom:') ? state?.current_model || '' : '')
    setShowCustom(false)
    setMaxSwitches(String(state?.max_switches ?? 1))
  }, [open, state])

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

  return (
    <Modal
      open={open}
      title={t('models_cap_chat_fallback')}
      onClose={onClose}
      footer={
        <>
          <span className={`text-xs text-accent mr-auto transition-opacity ${status ? 'opacity-100' : 'opacity-0'}`}>
            {status}
          </span>
          <Btn variant="ghost" onClick={onClose}>
            {t('config_cancel')}
          </Btn>
          <Btn
            variant="primary"
            disabled={busy || incomplete}
            onClick={() =>
              onSave({
                providerId: provider,
                model: finalModel,
                enabled,
                maxSwitches: Math.max(1, Math.min(5, parseInt(maxSwitches || '1', 10) || 1)),
              })
            }
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : t('config_save')}
          </Btn>
        </>
      }
    >
      <p className="text-xs text-content-tertiary -mt-1">{t('models_cap_chat_fallback_sub')}</p>

      {/* Label on the left, switch flush to the right. */}
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm text-content-secondary">{t('models_chat_fallback_enable')}</span>
        <Toggle checked={enabled} onChange={setEnabled} />
      </div>

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

          {incomplete && <p className="text-xs text-danger">{t('models_chat_fallback_incomplete')}</p>}
        </>
      )}
    </Modal>
  )
}

export default ChatFallbackButton
