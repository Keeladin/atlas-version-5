import { useEffect, useRef, useState } from 'react'
import { cancelWorkspaceTask, getWorkspaceTask, listWorkspaceTasks, resumeWorkspaceTask, type WorkspaceTask, type WorkspaceTaskFilter, type WorkspaceTaskList } from './api'
import { TopNavigation } from './TopNavigation'
import './WorkspacePage.css'

function timestamp(value: string | null) {
  return value ? new Date(value).toLocaleString() : 'Not recorded'
}

function label(value: string) { return value.replaceAll('_', ' ') }
function errorMessage(cause: unknown) { return cause instanceof Error ? cause.message : String(cause) }

export function WorkspaceTaskDetails({ task, busy, onCancel, onResume }: {
  task: WorkspaceTask; busy: boolean; onCancel: () => void; onResume: () => void
}) {
  const active = task.status === 'active'
  const checkpoint = task.checkpoints.find((item) => item.id === task.progress.current_checkpoint)
  return <article className="workspace-task" aria-label="Selected task">
    <header className="control-card workspace-task-header">
      <div className="workspace-task-heading"><h2>{task.title}</h2><span className={`workspace-status ${task.status}`}>{label(task.status)}</span></div>
      <dl className="workspace-identities"><div><dt>Project ID</dt><dd>{task.project_id}</dd></div><div><dt>Task ID</dt><dd>{task.task_id}</dd></div></dl>
      <div className="workspace-task-controls">
        <span>Updated <time dateTime={task.updated_at ?? undefined}>{timestamp(task.updated_at)}</time></span>
        {active ? <div className="workspace-buttons">
          {task.controller_state === 'stalled' ? <button type="button" disabled={busy} onClick={onResume}>Resume</button> : null}
          <button type="button" className="workspace-cancel" disabled={busy} onClick={onCancel}>Cancel task</button>
        </div> : <span>Read-only history</span>}
      </div>
    </header>
    <div className="workspace-columns">
      <section className="control-card workspace-contract" aria-label="Agreed work">
        <h3>Objective</h3><p className="workspace-prose">{task.objective}</p>
        <h3>Agreed scope</h3>
        {task.scope.length ? <ul className="workspace-text-list">{task.scope.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p>No additional scope recorded.</p>}
        <h3>Acceptance criteria</h3>
        <ul className="workspace-criteria">{task.acceptance_criteria.map((item) => <li key={item.id}>
          <div><span className={`workspace-status ${item.status}`}>{label(item.status)}</span><strong>{item.id}</strong></div>
          <p>{item.text}</p>
          {item.evidence_refs.length ? <div className="workspace-evidence"><span>Evidence refs</span>{item.evidence_refs.map((ref) => <code key={ref}>{ref}</code>)}</div> : <small>No evidence yet</small>}
        </li>)}</ul>
        <details className="workspace-advanced"><summary>Details & authority grants</summary>
          <p>Granted operations for this task (read-only)</p>
          {task.authority_grants.length ? <ul className="workspace-text-list">{task.authority_grants.map((grant) => <li key={grant}><code>{grant}</code></li>)}</ul> : <p>No task-specific grants.</p>}
          <p>Created {timestamp(task.created_at)}</p>
          <p>Transcript <code>{task.transcript_id}</code></p>
        </details>
      </section>
      <section className="control-card workspace-execution" aria-label="Execution and progress">
        <div className="workspace-task-heading"><h3>Execution</h3><span className="workspace-status">{label(task.controller_state)}</span></div>
        <div className="workspace-progress"><strong>{task.progress.percent == null ? 'Progress not reported' : `${task.progress.percent}%`}</strong><progress aria-label="Task progress" max={100} value={task.progress.percent} /></div>
        {task.findings.length ? <ul className="workspace-text-list">{task.findings.map((finding, index) => <li key={index}>{finding}</li>)}</ul> : null}
        <h3>Next step</h3><p className="workspace-prose">{task.next_step || (active ? 'No next step recorded.' : 'No further work scheduled.')}</p>
        {task.completion_rejected ? <p className="warning-text">{task.completion_rejected}</p> : null}
        {task.cancel_reason ? <p>Cancellation: {task.cancel_reason}</p> : null}
        <h3>Checkpoints</h3>
        {task.progress.current_checkpoint ? <p>Current: {checkpoint?.text ?? task.progress.current_checkpoint}</p> : null}
        {task.checkpoints.length ? <ol className="workspace-checkpoints">{task.checkpoints.map((item) => <li key={item.id}><span className={`workspace-status ${item.status}`}>{label(item.status)}</span><span>{item.text}</span></li>)}</ol> : <p>No checkpoints recorded.</p>}
        <h3>Pending actions <span className="workspace-count">{task.pending_actions.length}</span></h3>
        {task.pending_actions.length ? <ul className="workspace-actions">{task.pending_actions.map((item) => <li key={item.action_id}><strong>{item.operation}</strong><span>{label(item.phase)}</span><code>{item.action_id}</code>{item.evidence_id ? <small>Evidence: {item.evidence_id}</small> : null}</li>)}</ul> : <p>No pending actions.</p>}
        <dl><div><dt>No-progress retries</dt><dd>{task.retry_count}</dd></div><div><dt>Transient retries</dt><dd>{task.transient_retry_count}</dd></div><div><dt>Next wake</dt><dd>{task.next_wake_at ? timestamp(task.next_wake_at) : 'Not scheduled'}</dd></div></dl>
      </section>
    </div>
  </article>
}

export function WorkspacePage() {
  const [filter, setFilter] = useState<WorkspaceTaskFilter>('all')
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const [listState, setListState] = useState<{ key: string; result: WorkspaceTaskList | null; error: string | null } | null>(null)
  const [selection, setSelection] = useState<string | null>(null)
  const [taskState, setTaskState] = useState<{ key: string; result: WorkspaceTask | null; error: string | null } | null>(null)
  const [actionBusy, setActionBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const actionLock = useRef(false)
  const listKey = `${filter}:${offset}:${revision}`
  const loading = listState?.key !== listKey
  const list = loading ? null : listState?.result
  const listError = loading ? null : listState?.error
  const selectedId = list?.items.some((item) => item.task_id === selection) ? selection : list?.items[0]?.task_id ?? null
  const taskKey = `${selectedId}:${revision}`
  const detailLoading = selectedId !== null && taskState?.key !== taskKey
  const task = taskState?.key === taskKey ? taskState.result : null
  const taskError = taskState?.key === taskKey ? taskState.error : null

  useEffect(() => {
    let current = true
    listWorkspaceTasks(filter, offset).then((result) => {
      if (current) {
        setListState({ key: listKey, result, error: null })
        setSelection((id) => result.items.some((item) => item.task_id === id) ? id : result.items[0]?.task_id ?? null)
      }
    }).catch((cause) => {
      if (current) setListState({ key: listKey, result: null, error: errorMessage(cause) })
    })
    return () => { current = false }
  }, [filter, offset, listKey])

  useEffect(() => {
    let current = true
    if (!selectedId) return
    getWorkspaceTask(selectedId).then((result) => {
      if (current) setTaskState({ key: taskKey, result, error: null })
    }).catch((cause) => {
      if (current) setTaskState({ key: taskKey, result: null, error: errorMessage(cause) })
    })
    return () => { current = false }
  }, [selectedId, taskKey])

  async function lifecycle(kind: 'cancel' | 'resume') {
    if (!task || actionLock.current) return
    if (kind === 'cancel' && !window.confirm(`Cancel “${task.title}”? Atlas will stop this managed task and request cancellation of its active execution. This task cannot be resumed after cancellation.`)) return
    actionLock.current = true
    setActionBusy(true)
    setNotice(null)
    try {
      const result = await (kind === 'cancel' ? cancelWorkspaceTask(task.task_id) : resumeWorkspaceTask(task.task_id))
      const warnings = result.cleanup?.warnings ?? []
      setNotice(`${kind === 'cancel' ? 'Task cancelled.' : 'Task resumed.'}${warnings.length ? ` Cleanup needs attention: ${warnings.join(' ')}` : ''}`)
    } catch (cause) { setNotice(errorMessage(cause)) }
    finally {
      actionLock.current = false
      setActionBusy(false)
      setRevision((value) => value + 1)
    }
  }

  return <div className="control-shell workspace-shell">
    <header className="control-topbar"><div className="control-title-cluster"><img className="control-avatar" src="/atlas-icon.webp" alt="" /><div><div className="eyebrow">ATLAS V5</div><h1>Workspace</h1></div></div><TopNavigation current="workspace" /></header>
    <main className="workspace-main">
      <div className="workspace-intro"><p>Follow the work agreed with Atlas in chat.</p><button type="button" disabled={loading || detailLoading || actionBusy} onClick={() => setRevision((value) => value + 1)}>Refresh</button></div>
      {notice ? <p className="workspace-notice" role="status">{notice}</p> : null}
      <div className="workspace-layout">
        <aside className="control-card workspace-history" aria-label="Task history">
          <h2>Tasks</h2>
          <label className="workspace-filter">Show <select value={filter} disabled={actionBusy} onChange={(event) => { setFilter(event.target.value as WorkspaceTaskFilter); setOffset(0); setSelection(null); setNotice(null) }}><option value="all">All</option><option value="active">Active</option><option value="terminal">Terminal</option></select></label>
          {listError ? <p className="warning-text" role="alert">{listError}</p> : null}
          {loading ? <p role="status">Loading tasks…</p> : <>
            <div className="workspace-task-list">{list?.items.map((item) => <button type="button" key={item.task_id} aria-pressed={selectedId === item.task_id} disabled={actionBusy} onClick={() => { setSelection(item.task_id); setNotice(null) }}><strong>{item.title}</strong><span>{label(item.status)} · {label(item.controller_state)}</span><small>{timestamp(item.updated_at)}</small></button>)}</div>
            {list && (offset > 0 || list.next_offset !== null) ? <div className="workspace-pagination"><button type="button" disabled={offset === 0 || actionBusy} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous</button><button type="button" disabled={list.next_offset === null || actionBusy} onClick={() => setOffset(list.next_offset ?? offset)}>Next</button></div> : null}
          </>}
        </aside>
        <div className="workspace-detail" aria-busy={loading || detailLoading || actionBusy}>
          {taskError ? <p className="control-card warning-text" role="alert">{taskError}</p> : detailLoading ? <p role="status">Loading task…</p> : task && task.task_id === selectedId ? <WorkspaceTaskDetails task={task} busy={loading || actionBusy} onCancel={() => { void lifecycle('cancel') }} onResume={() => { void lifecycle('resume') }} /> : !loading && !listError ? <section className="control-card workspace-empty"><h2>{filter === 'all' && offset === 0 ? 'No managed tasks yet' : 'No tasks in this view'}</h2><p>Discuss and agree the scope with Atlas in chat. Atlas then creates a managed task, and you can follow its progress here.</p><a className="control-link" href="/">Open chat</a></section> : null}
        </div>
      </div>
    </main>
  </div>
}
