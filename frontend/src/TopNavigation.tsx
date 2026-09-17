export function TopNavigation({ current }: { current: 'home' | 'workspace' | 'control' }) {
  return <nav className="top-navigation" aria-label="Main navigation">
    {([['home', '/', 'Home'], ['workspace', '/workspace', 'Workspace'], ['control', '/control', 'Control']] as const).map(([id, href, label]) =>
      <a key={id} href={href} className={`context-item context-link${current === id ? ' active' : ''}`} aria-current={current === id ? 'page' : undefined}>{label}</a>,
    )}
  </nav>
}
