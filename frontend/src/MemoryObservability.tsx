import { useEffect, useRef, useState } from 'react'
import {
  getMemoryCandidateDetail,
  getMemoryObservability,
  streamMemoryObservability,
  type DurableMemoryInspection,
  type MemoryCandidate,
  type MemoryCandidateDetail,
  type MemoryObservability,
  type MemoryReconciliationAttempt,
  type MemoryStreamState,
} from './api'

function formatDate(value: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
}

function decisionLabel(candidate: MemoryCandidate): string {
  const decision = typeof candidate.decision.decision === 'string' ? candidate.decision.decision : candidate.latest_attempt?.semantic_decision
  const result = typeof candidate.decision.result === 'string' ? candidate.decision.result : candidate.latest_attempt?.result.result
  if (decision && result && result !== decision) return `${decision} · ${String(result)}`
  return decision ?? (typeof result === 'string' ? result : candidate.status)
}

function resultLabel(attempt: MemoryReconciliationAttempt): string {
  const result = attempt.result.result
  const code = attempt.result.code
  if (typeof result === 'string' && typeof code === 'string') return `${result} · ${code}`
  if (typeof result === 'string') return result
  return attempt.status
}

function memoryMeta(memory: DurableMemoryInspection): string {
  return [memory.authority, memory.status, memory.memory_kind ?? 'memory', memory.scope].join(' · ')
}

export function MemoryObservabilityPanel() {
  const [overview, setOverview] = useState<MemoryObservability | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'candidates' | 'memories'>('candidates')
  const [selected, setSelected] = useState<MemoryCandidateDetail | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [streamState, setStreamState] = useState<MemoryStreamState>('connecting')
  const selectedIdRef = useRef<string | null>(null)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const next = await getMemoryObservability(50)
      setOverview(next)
      const currentSelectedId = selectedIdRef.current
      if (currentSelectedId) {
        const detail = await getMemoryCandidateDetail(currentSelectedId)
        if (selectedIdRef.current === currentSelectedId) setSelected(detail)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let active = true
    let closeStream = () => {}
    getMemoryObservability(50).then((next) => {
      if (!active) return
      setOverview(next)
      setLoading(false)
      closeStream = streamMemoryObservability(
        (snapshot) => {
          if (!active) return
          setOverview(snapshot)
          setError(null)
          const currentSelectedId = selectedIdRef.current
          if (currentSelectedId) {
            void getMemoryCandidateDetail(currentSelectedId).then((detail) => {
              if (active && selectedIdRef.current === currentSelectedId) setSelected(detail)
            }).catch(() => undefined)
          }
        },
        (state) => { if (active) setStreamState(state) },
        50,
        next.stream_token,
      )
    }).catch((cause) => {
      if (!active) return
      setError(cause instanceof Error ? cause.message : String(cause))
      setLoading(false)
      setStreamState('reconnecting')
    })
    return () => { active = false; closeStream() }
  }, [])

  async function inspect(candidateId: string) {
    if (selectedId === candidateId && selected) {
      selectedIdRef.current = null
      setSelectedId(null)
      setSelected(null)
      return
    }
    selectedIdRef.current = candidateId
    setSelectedId(candidateId)
    setDetailLoading(true)
    try {
      setSelected(await getMemoryCandidateDetail(candidateId))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setSelected(null)
    } finally {
      setDetailLoading(false)
    }
  }

  const candidateCounts = overview?.summary.candidate_counts ?? {}
  const memoryCounts = overview?.summary.memory_counts ?? {}
  const lastAttempt = overview?.summary.last_attempt ?? null
  const verifier = overview?.summary.proposal_verifier
  const groundingCounts = overview?.summary.grounding_counts ?? {}
  const obligationCounts = overview?.summary.obligation_counts ?? {}
  const legacyBacklog = groundingCounts.legacy_unverified ?? 0
  const pendingReviews = obligationCounts.memory_review ?? 0
  const openConflicts = obligationCounts.memory_conflict ?? 0
  const pending = (candidateCounts.pending ?? 0) + (candidateCounts.leased ?? 0)
  const retained = candidateCounts.retained_short_term ?? 0

  return <section className="control-card control-full memory-observability">
    <div className="memory-observability-head">
      <div><div className="panel-title">Memory observability</div><p>Read-only view of candidate intake, reconciliation outcomes and durable memory.</p></div>
      <div className="memory-observability-actions"><span className={`memory-live-state ${streamState}`}><i />{streamState === 'live' ? 'LIVE' : streamState === 'connecting' ? 'CONNECTING' : 'RECONNECTING'}</span><span>READ ONLY</span><button type="button" onClick={() => { void refresh() }} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button></div>
    </div>
    {error ? <p className="warning-text">{error}</p> : null}
    <div className="memory-summary-grid">
      <div><span>Reconciliation</span><strong>{overview?.reconciliation.enabled ? 'Enabled' : 'Paused'}</strong><small>{overview?.reconciliation.model ?? '—'}</small></div>
      <div><span>Pending candidates</span><strong>{pending}</strong><small>{retained} retained short-term</small></div>
      <div><span>Durable memories</span><strong>{memoryCounts.active ?? 0}</strong><small>{overview ? `${overview.summary.authority_counts.derived} derived · ${overview.summary.authority_counts.owner} owner` : '—'}</small></div>
      <div><span>Last reconciliation</span><strong>{lastAttempt?.semantic_decision ?? lastAttempt?.status ?? 'No decisions yet'}</strong><small>{lastAttempt ? `${resultLabel(lastAttempt)} · ${formatDate(lastAttempt.completed_at ?? lastAttempt.started_at)}` : 'Waiting for an eligible candidate'}</small></div>
      <div><span>Legacy review backlog</span><strong>{legacyBacklog}</strong><small>{overview ? `${pendingReviews} pending reviews · ${openConflicts} open conflicts` : '—'}</small></div>
      <div><span>Verifier disagreement</span><strong>{verifier?.disagreement_rate != null ? `${Math.round(verifier.disagreement_rate * 100)}%` : '—'}</strong><small>{verifier ? `${verifier.disagree}/${verifier.eligible} disagree · ${verifier.review_excluded} review excluded` : 'No eligible comparisons yet'}</small></div>
    </div>

    <div className="memory-tabs" role="tablist" aria-label="Memory observability views">
      <button type="button" role="tab" aria-selected={tab === 'candidates'} className={tab === 'candidates' ? 'active' : ''} onClick={() => setTab('candidates')}>Candidates <span>{overview?.recent_candidates.length ?? 0}</span></button>
      <button type="button" role="tab" aria-selected={tab === 'memories'} className={tab === 'memories' ? 'active' : ''} onClick={() => setTab('memories')}>Durable memories <span>{overview?.recent_memories.length ?? 0}</span></button>
    </div>

    {tab === 'candidates' ? <div className="memory-table" role="table" aria-label="Recent memory candidates">
      <div className="memory-row memory-row-head" role="row"><span>When</span><span>Candidate</span><span>Scope</span><span>Outcome</span></div>
      {overview?.recent_candidates.length ? overview.recent_candidates.map((candidate) => <button className={`memory-row${selectedId === candidate.id ? ' selected' : ''}`} type="button" role="row" key={candidate.id} onClick={() => { void inspect(candidate.id) }}>
        <span>{formatDate(candidate.created_at)}</span>
        <span><strong>{candidate.content ?? 'Content removed'}</strong><small>{candidate.kind}{candidate.subject ? ` · ${candidate.subject}` : ''}</small></span>
        <span><strong>{candidate.scope}</strong><small>{candidate.durability} · {Math.round(candidate.confidence * 100)}%</small></span>
        <span><strong>{decisionLabel(candidate)}</strong><small>{candidate.status}</small></span>
      </button>) : <div className="memory-empty">No memory candidates have been recorded yet.</div>}
    </div> : <div className="memory-table memory-table-memories" role="table" aria-label="Recent durable memories">
      <div className="memory-row memory-row-head" role="row"><span>Updated</span><span>Memory</span><span>Authority</span><span>Index</span></div>
      {overview?.recent_memories.length ? overview.recent_memories.map((memory) => <div className="memory-row" role="row" key={memory.id}>
        <span>{formatDate(memory.updated_at)}</span>
        <span><strong>{memory.content ?? 'Content deleted'}</strong><small>{memory.memory_kind ?? 'memory'} · {memory.scope}</small></span>
        <span><strong>{memory.authority}</strong><small>{memory.status} · {memory.durability}</small></span>
        <span><strong>{memory.embedded ? 'Embedded' : 'Pending'}</strong><small>{memory.embedding_model ?? '—'}</small></span>
      </div>) : <div className="memory-empty">No durable memories have been recorded yet.</div>}
    </div>}

    {tab === 'candidates' && selectedId ? <CandidateTrace detail={selected} loading={detailLoading} /> : null}
  </section>
}

function CandidateTrace({ detail, loading }: { detail: MemoryCandidateDetail | null; loading: boolean }) {
  if (loading) return <div className="memory-trace"><p>Loading reconciliation trace…</p></div>
  if (!detail) return null
  const { candidate, trigger, evidence_sources: evidenceSources, verification, attempts, linked_memories: linkedMemories } = detail
  const latestReading = verification.independent_readings[0]
  const latestComparison = verification.comparisons[0]
  const latestReconciliation = verification.reconciliations[0]
  const latestPolicy = verification.policies[0]
  return <div className="memory-trace">
    <div className="memory-trace-heading"><div><span className="eyebrow">TRACE</span><h3>{candidate.kind} candidate</h3></div><span>{candidate.status}</span></div>
    <div className="memory-trace-grid">
      <section><h4>Trigger turn</h4><p className="memory-trace-meta">{trigger.chat_title ?? 'Untitled chat'} · turn {trigger.sequence ?? '—'} · {trigger.actor ?? 'unknown'}</p>{trigger.deleted ? <p className="warning-text">Trigger content was deleted and is not projected here.</p> : <blockquote>{trigger.text ?? 'No trigger text available.'}</blockquote>}</section>
      <section><h4>Candidate</h4><p>{candidate.content ?? 'Candidate content removed.'}</p><dl><div><dt>Canonical evidence</dt><dd>{evidenceSources.length} referenced turn{evidenceSources.length === 1 ? '' : 's'}</dd></div><div><dt>Scope</dt><dd>{candidate.scope} · {candidate.durability}</dd></div><div><dt>Confidence</dt><dd>{Math.round(candidate.confidence * 100)}% <small>(ordering only)</small></dd></div></dl></section>
    </div>
    <section className="memory-attempts"><div className="memory-trace-subhead"><h4>Canonical evidence</h4><span>Runtime resolves references; it does not decide their meaning.</span></div>{evidenceSources.length ? evidenceSources.map((source) => <div className="memory-attempt" key={`${source.turn_id}:${source.span_ref ?? ''}`}><span>{source.principal ?? source.actor ?? 'source'}</span><div><strong>turn {source.sequence ?? '—'}{source.span_ref ? ` · ${source.span_ref}` : ''}</strong><small>{source.deleted ? 'Content deleted' : source.text ?? 'No text available'}</small></div></div>) : <p>No canonical evidence references are attached.</p>}</section>
    <section className="memory-attempts"><div className="memory-trace-subhead"><h4>Verification state</h4><span>Blind reading is persisted before proposal comparison.</span></div><div className="memory-attempt"><span>READ</span><div><strong>{String(latestReading?.category ?? '—')}</strong><small>{Array.isArray(latestReading?.extracted_claims) ? `${latestReading.extracted_claims.length} extracted claim(s)` : 'No persisted blind reading yet'}</small></div></div><div className="memory-attempt"><span>DIFF</span><div><strong>{String(latestComparison?.verdict ?? '—')}</strong><small>{latestComparison ? 'proposal compared with immutable reading' : 'not compared yet'}</small></div></div><div className="memory-attempt"><span>GRAPH</span><div><strong>{String(latestReconciliation?.relation ?? '—')}</strong><small>{latestReconciliation ? `memory rev ${String(latestReconciliation.evaluated_memory_revision ?? '—')}` : 'not reconciled yet'}</small></div></div><div className="memory-attempt"><span>POLICY</span><div><strong>{String(latestPolicy?.decision ?? '—')}</strong><small>{String(latestPolicy?.reason_code ?? 'no policy reason')}</small></div></div></section>
    <section className="memory-attempts"><div className="memory-trace-subhead"><h4>Reconciliation attempts</h4><span>Free-form model reasoning is intentionally not retained.</span></div>{attempts.length ? attempts.map((attempt) => <div className="memory-attempt" key={attempt.id}><span>#{attempt.attempt_number}</span><div><strong>{attempt.semantic_decision ?? attempt.status}</strong><small>{resultLabel(attempt)} · memory rev {attempt.evaluated_memory_revision ?? '—'} · source rev {attempt.evaluated_source_revision ?? '—'}</small></div><time>{formatDate(attempt.completed_at ?? attempt.started_at)}</time></div>) : <p>No reconciliation attempt has claimed this candidate yet.</p>}</section>
    <section className="memory-linked"><h4>Resulting / linked memory</h4>{linkedMemories.length ? linkedMemories.map((memory) => <div className="memory-linked-card" key={memory.id}><strong>{memory.content ?? 'Content deleted'}</strong><span>{memoryMeta(memory)}</span><small>{memory.provenance.length} provenance record{memory.provenance.length === 1 ? '' : 's'}</small></div>) : <p>No durable memory is linked to this candidate.</p>}</section>
  </div>
}
