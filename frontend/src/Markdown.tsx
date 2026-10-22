import { useState } from 'react';
import type { ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

function CodeBlock({ children, ...props }: ComponentPropsWithoutRef<'pre'>) {
  const [copied, setCopied] = useState('Copy code');
  const [code, setCode] = useState<HTMLElement | null>(null);
  async function copy() {
    try { await navigator.clipboard.writeText(code?.textContent || ''); setCopied('Copied'); }
    catch { setCopied('Select code to copy'); }
  }
  return <div className="code-sandbox">
    <div className="code-toolbar"><span>{code?.querySelector('code')?.className.replace('language-', '') || 'Code'}</span><button onClick={() => void copy()} aria-label="Copy code">{copied}</button></div>
    <pre {...props} ref={setCode}>{children}</pre>
    <small>Code shown only · not executed</small>
  </div>;
}

/** Safe Markdown only: fenced code is readable/copyable, never evaluated. */
export default function Markdown({ children }: { children: string }) {
  return <div className="prose"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    img: ({ alt }) => <span className="muted">{alt || 'Image omitted'}</span>,
    pre: ({children}) => <CodeBlock>{children}</CodeBlock>,
    table: ({ children }) => <div className="table-scroll" tabIndex={0} role="region" aria-label="Answer table"><table>{children}</table></div>,
  }}>{children}</ReactMarkdown></div>;
}
