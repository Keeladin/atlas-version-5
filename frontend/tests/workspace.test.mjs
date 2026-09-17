import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import ts from 'typescript'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import * as api from '../src/api.ts'

async function capture(exercise, status = 200, body = { items: [], next_offset: null }) {
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify(body), { status })
  }
  try { await exercise() } finally { globalThis.fetch = original }
  return request
}

test('Workspace history sends the selected filter and page through authenticated REST', async () => {
  const request = await capture(() => api.listWorkspaceTasks('terminal', 20))
  assert.equal(request.url, '/api/workspace/tasks?status=terminal&offset=20&limit=20')
  assert.equal(request.options.credentials, 'same-origin')
})

test('Workspace reads and lifecycle controls encode task IDs and post without task authorship', async () => {
  for (const [operation, suffix, method] of [[api.getWorkspaceTask, '', 'GET'], [api.cancelWorkspaceTask, '/cancel', 'POST'], [api.resumeWorkspaceTask, '/resume', 'POST']]) {
    const request = await capture(() => operation('task/id'))
    assert.equal(request.url, `/api/workspace/tasks/task%2Fid${suffix}`)
    assert.equal(request.options.method, method)
    assert.equal(request.options.body, undefined)
  }
})

test('Workspace reports lifecycle conflicts and missing tasks using server detail', async () => {
  for (const status of [404, 409]) {
    await capture(() => assert.rejects(api.resumeWorkspaceTask('id'), /Task unavailable/), status, { detail: 'Task unavailable' })
  }
})

test('Workspace validation errors have readable fallback text', async () => {
  await capture(() => assert.rejects(api.getWorkspaceTask('bad'), /Workspace request failed \(422\)/), 422, { detail: [{ msg: 'Invalid UUID' }] })
})

// Render the real TSX without adding a browser-test dependency to this repository.
const require = createRequire(import.meta.url)
function componentModule(name) {
  const source = readFileSync(new URL(`../src/${name}.tsx`, import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  const exports = {}
  vm.runInNewContext(outputText, { exports, require: (id) => id.endsWith('.css') ? {} : id === './api' ? api : id === './TopNavigation' ? componentModule('TopNavigation') : require(id) })
  return exports
}
const { WorkspaceTaskDetails, WorkspacePage } = componentModule('WorkspacePage')
const { TopNavigation } = componentModule('TopNavigation')
const task = {
  task_id: 'task-identity', project_id: 'project-identity', transcript_id: 'transcript', title: 'Ship agreed work',
  status: 'active', controller_state: 'stalled', objective: 'Implement the dashboard', scope: ['Only this worktree'],
  authority_grants: ['coding.agent.start_session'], acceptance_criteria: [{ id: 'A1', text: 'Checks pass', status: 'passed', evidence_refs: ['evidence-1'] }],
  checkpoints: [{ id: 'C1', text: 'Verify changes', status: 'pending' }], progress: { percent: 50, current_checkpoint: 'C1' },
  next_step: 'Run verification', findings: ['Implementation ready'], pending_actions: [{ action_id: 'action-1', operation: 'test.operation', phase: 'uncertain' }],
  retry_count: 3, transient_retry_count: 4, next_wake_at: null,
  live: { worker_state: 'active', current_activity: 'Inspect repository evidence', executor: 'Atlas + Codex', last_activity_at: null, heartbeat_at: null, run_id: 'run-1', recent_activity: [{ timestamp: null, executor: 'Atlas tool', operation: 'storage.projects.status', phase: 'succeeded', summary: 'Checked repository status', detail: null, evidence_id: 'live-evidence-1', targets: { path: '/repo' } }] },
  created_at: null, updated_at: null,
}
const details = (change = {}) => renderToStaticMarkup(React.createElement(WorkspaceTaskDetails, { task: { ...task, ...change }, busy: false, onCancel() {}, onResume() {} }))

test('selected task renders contract and execution, evidence, IDs and advanced read-only grants', () => {
  const html = details()
  for (const text of ['project-identity', 'task-identity', 'Only this worktree', 'evidence-1', 'passed', '50%', 'Verify changes', 'Run verification', 'No-progress retries', 'Transient retries', 'action-1', 'uncertain', 'Live activity', 'Inspect repository evidence', 'Atlas + Codex', 'Checked repository status', 'path: /repo']) assert.ok(html.includes(text), text)
  assert.match(html, /<details[^>]*>.*coding.agent.start_session.*<\/details>/)
  assert.match(html, />Resume<\/button>/)
  assert.match(html, />Cancel task<\/button>/)
})

test('terminal tasks are read-only even if stale controller state says stalled', () => {
  for (const status of ['complete', 'cancelled']) {
    const html = details({ status })
    assert.ok(html.includes('Read-only history'))
    assert.doesNotMatch(html, /<button/)
  }
})

test('Resume is only offered for stalled active tasks', () => {
  for (const controller_state of ['ready', 'running', 'retrying', 'waiting_for_owner']) {
    const html = details({ controller_state })
    assert.doesNotMatch(html, />Resume<\/button>/)
    assert.match(html, />Cancel task<\/button>/)
  }
})

test('Workspace and top navigation expose Home, Workspace and Control without a create form', () => {
  const html = renderToStaticMarkup(React.createElement(WorkspacePage))
  assert.match(html, /Loading tasks/)
  assert.doesNotMatch(html, /<form/)
  for (const current of ['home', 'workspace', 'control']) {
    const nav = renderToStaticMarkup(React.createElement(TopNavigation, { current }))
    for (const href of ['/', '/workspace', '/control']) assert.ok(nav.includes(`href="${href}"`))
    assert.equal((nav.match(/aria-current="page"/g) ?? []).length, 1)
  }
})

test('mobile navigation includes Workspace and preserves the Projects entry', () => {
  const { MobileNavigationDrawer } = componentModule('MobileNavigationDrawer')
  const html = renderToStaticMarkup(React.createElement(MobileNavigationDrawer, { open: true, chats: [], activeChatId: null, busy: false }))
  for (const text of ['Workspace', 'Projects', 'Repositories', 'Storage', 'Scheduled', 'Control']) assert.ok(html.includes(`<span>${text}</span>`), text)
})
