import { useRef, useState, type TouchEvent } from 'react'
import type { Chat } from './api'

type DrawerIconName = 'new' | 'projects' | 'repositories' | 'storage' | 'scheduled' | 'control' | 'workspace'

function DrawerIcon({ name }: { name: DrawerIconName }) {
  const common = { width: 22, height: 22, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  if (name === 'new') return <svg {...common} aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
  if (name === 'workspace') return <svg {...common} aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16M12 9h6M12 14h4" /></svg>
  if (name === 'projects') return <svg {...common} aria-hidden="true"><path d="M3 7.5h7l2 2h9v9.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" /><path d="M3 7.5V5a2 2 0 0 1 2-2h5l2 2h5" /></svg>
  if (name === 'repositories') return <svg {...common} aria-hidden="true"><circle cx="6" cy="5" r="2" /><circle cx="18" cy="19" r="2" /><circle cx="6" cy="19" r="2" /><path d="M6 7v10" /><path d="M8 5h4a4 4 0 0 1 4 4v8" /></svg>
  if (name === 'storage') return <svg {...common} aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 8h10" /><path d="M7 16h.01M11 16h.01" /></svg>
  if (name === 'scheduled') return <svg {...common} aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
  return <svg {...common} aria-hidden="true"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21h-4v-.1A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.2 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H2.4v-4h.1A1.7 1.7 0 0 0 4.2 8.6a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 8.6 4.2a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V2.4h4v.1a1.7 1.7 0 0 0 1 1.7 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 8.6a1.7 1.7 0 0 0 .6 1 1.7 1.7 0 0 0 1.1.4h.1v4h-.1a1.7 1.7 0 0 0-1.7 1Z" /></svg>
}

type MobileNavigationDrawerProps = {
  open: boolean
  chats: Chat[]
  activeChatId: string | null
  busy: boolean
  onClose: () => void
  onNewChat: () => void
  onSelectChat: (chatId: string) => void
  onRenameChat: (chat: Chat) => void
  onDeleteChat: (chat: Chat) => void
  onProjects: () => void
  onRepositories: () => void
  onStorage: () => void
  onScheduled: () => void
  onControl: () => void
  onWorkspace: () => void
}

export function MobileNavigationDrawer(props: MobileNavigationDrawerProps) {
  const touchStartX = useRef<number | null>(null)
  const [chatMenuId, setChatMenuId] = useState<string | null>(null)
  if (!props.open) return null

  function run(action: () => void) {
    props.onClose()
    action()
  }

  function handleTouchEnd(event: TouchEvent<HTMLElement>) {
    const start = touchStartX.current
    touchStartX.current = null
    if (start !== null && event.changedTouches[0] && event.changedTouches[0].clientX < start - 55) props.onClose()
  }

  return <>
    <button className="mobile-nav-backdrop" type="button" aria-label="Close navigation" onClick={props.onClose} />
    <aside className="mobile-nav-drawer" aria-label="Atlas navigation" onTouchStart={(event) => { touchStartX.current = event.touches[0]?.clientX ?? null }} onTouchEnd={handleTouchEnd}>
      <div className="mobile-nav-brand">
        <img src="/atlas-icon.webp" alt="" aria-hidden="true" />
        <div><strong>Atlas</strong><span>V5</span></div>
        <button type="button" aria-label="Close navigation" onClick={props.onClose}>×</button>
      </div>
      <nav className="mobile-nav-primary">
        <button type="button" disabled={props.busy} onClick={() => run(props.onNewChat)}><DrawerIcon name="new" /><span>New chat</span></button>
        <button type="button" onClick={() => run(props.onWorkspace)}><DrawerIcon name="workspace" /><span>Workspace</span></button>
        <button type="button" onClick={() => run(props.onProjects)}><DrawerIcon name="projects" /><span>Projects</span></button>
        <button type="button" onClick={() => run(props.onRepositories)}><DrawerIcon name="repositories" /><span>Repositories</span></button>
        <button type="button" onClick={() => run(props.onStorage)}><DrawerIcon name="storage" /><span>Storage</span></button>
        <button type="button" onClick={() => run(props.onScheduled)}><DrawerIcon name="scheduled" /><span>Scheduled</span></button>
        <button type="button" onClick={() => run(props.onControl)}><DrawerIcon name="control" /><span>Control</span></button>
      </nav>
      <div className="mobile-nav-divider" />
      <section className="mobile-nav-recents" aria-label="Recent chats">
        <h2>Recents</h2>
        <div className="mobile-nav-chat-list">
          {props.chats.map((chat) => (
            <div className={`mobile-nav-chat-entry${chat.id === props.activeChatId ? ' active' : ''}`} key={chat.id}>
              <button className="mobile-nav-chat-select" type="button" disabled={props.busy} title={chat.title} onClick={() => run(() => props.onSelectChat(chat.id))}>
                <span>{chat.title}</span>
              </button>
              <button className="mobile-nav-chat-more" type="button" disabled={props.busy} aria-label={`Options for ${chat.title}`} title="Chat options" onClick={() => setChatMenuId((current) => current === chat.id ? null : chat.id)}>⋯</button>
              {chatMenuId === chat.id ? <div className="mobile-nav-chat-menu">
                <button type="button" onClick={() => { setChatMenuId(null); props.onRenameChat(chat) }}>Rename</button>
                <button type="button" className="danger" onClick={() => { setChatMenuId(null); props.onDeleteChat(chat) }}>Delete</button>
              </div> : null}
            </div>
          ))}
          {!props.chats.length ? <p>No chats yet.</p> : null}
        </div>
      </section>
    </aside>
  </>
}
