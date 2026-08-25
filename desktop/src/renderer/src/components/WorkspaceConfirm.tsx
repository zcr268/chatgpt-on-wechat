import React, { useEffect } from 'react'
import { t } from '../i18n'
import { Modal, Btn } from '../pages/settings/primitives'
import { useWorkspaceStore } from '../store/workspaceStore'

/**
 * Dialog for the workspace panel's yes/no questions (discard unsaved edits,
 * overwrite a file the agent changed underneath us).
 *
 * Mounted at the app level rather than inside the panel: an unsaved editor
 * outlives the panel while the user is off on another route, and a question
 * asked then would otherwise have nothing to render it and would hang whoever
 * is awaiting the answer.
 */
const WorkspaceConfirm: React.FC = () => {
  const pending = useWorkspaceStore((s) => s.pendingConfirm)
  const answerConfirm = useWorkspaceStore((s) => s.answerConfirm)

  // Keyboard is the fast path out of a dialog like this one.
  useEffect(() => {
    if (!pending) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') answerConfirm(false)
      else if (e.key === 'Enter') answerConfirm(true)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pending, answerConfirm])

  if (!pending) return null

  return (
    <Modal open title={t(pending.titleKey)} onClose={() => answerConfirm(false)}>
      <p className="text-sm text-content-secondary leading-relaxed">{t(pending.msgKey)}</p>
      <div className="flex items-center justify-end gap-2 pt-1">
        <Btn onClick={() => answerConfirm(false)}>{t('config_cancel')}</Btn>
        <Btn variant="danger" onClick={() => answerConfirm(true)} autoFocus>
          {t(pending.okKey)}
        </Btn>
      </div>
    </Modal>
  )
}

export default WorkspaceConfirm
