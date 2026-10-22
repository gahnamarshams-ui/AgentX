import type { SVGProps } from 'react';
export type IconName = 'sun' | 'moon' | 'monitor' | 'new' | 'panel' | 'chat' | 'history' | 'tools' | 'shield' | 'settings' | 'arrow' | 'chevron' | 'refresh' | 'close' | 'search' | 'copy' | 'check' | 'book' | 'spark' | 'compass';
const paths: Record<IconName, React.ReactNode> = {
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></>,
  moon: <path d="M20.7 13.3A9 9 0 0 1 10.7 3.3a9 9 0 1 0 10 10Z"/>,
  monitor: <><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M12 17v4m-4 0h8"/></>,
  new: <><path d="M13.5 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-8.5"/><path d="m15.5 3.5 5 5M10 15l4.5-1 8-8a2 2 0 0 0-5-5l-8 8L8.5 14.5Z" transform="translate(0 1) scale(.9)"/></>,
  panel: <><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/></>,
  chat: <path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2a8.5 8.5 0 0 1 8.5 8.5Z"/>,
  history: <><path d="M3 11a9 9 0 1 1 2.8 7M3 4v7h7"/><path d="M12 7v5l3 2"/></>,
  tools: <><path d="m14.5 6.5 3 3M5 21l-2-2 9-9a6 6 0 0 1 8-7l-4 4 2 2 4-4a6 6 0 0 1-7 8Z"/></>,
  shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/></>,
  settings: <><path d="m9 3-.8 3-2 .9-2.8-.8L1.8 10l2.3 2.1v2L2 16.3l2.4 3.9 3-.8 1.8 1 .8 2.6h4l.8-2.8 2-1 2.7.8 2.4-4-2.1-2v-2l2.1-2.3-2.4-4-3 .8-1.7-.9L14 3Z" transform="translate(1 0) scale(.92)"/><circle cx="12" cy="12" r="3"/></>,
  arrow: <><path d="M12 19V5m-6 6 6-6 6 6"/></>,
  chevron: <path d="m9 5 7 7-7 7"/>,
  refresh: <><path d="M20 7a9 9 0 0 0-15-1L2 10m0-6v6h6M4 17a9 9 0 0 0 15 1l3-4m0 6v-6h-6"/></>,
  close: <path d="m6 6 12 12M6 18 18 6"/>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  copy: <><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  book: <><path d="M12 5C7 1 2 4 2 4v15s5-3 10 1c5-4 10-1 10-1V4s-5-3-10 1Zm0 0v15"/></>,
  spark: <><path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4Z"/></>,
  compass: <><circle cx="12" cy="12" r="9"/><path d="m16 8-2 6-6 2 2-6Z"/></>,
};
export default function Icon({ name, ...props }: SVGProps<SVGSVGElement> & { name: IconName }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>;
}
export function AgentMark({ className = '' }: { className?: string }) {
  return <svg className={`agent-mark ${className}`} width="32" height="32" viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <path d="M8 5H3l10 11L3 27h5l8-9 8 9h5L19 16 29 5h-5l-8 9Z" fill="currentColor"/>
  </svg>;
}
