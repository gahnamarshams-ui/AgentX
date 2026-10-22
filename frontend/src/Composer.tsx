import { useLayoutEffect, useRef } from 'react';
import type { FormEvent } from 'react';
import Icon from './Icon';

interface Props { value: string; onChange: (value: string) => void; onSubmit: () => void; submitting: boolean; error: string; hasTask: boolean; responding?: boolean }
export default function Composer({ value, onChange, onSubmit, submitting, error, hasTask, responding=false }: Props) {
  const input = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    if (input.current) { input.current.style.height = 'auto'; input.current.style.height = `${Math.min(input.current.scrollHeight || 56, 200)}px`; }
  }, [value]);
  function submit(event: FormEvent) { event.preventDefault(); if (!submitting) onSubmit(); }
  return <div className="composer-dock">
    <form className={`composer${error ? ' composer-invalid' : ''}`} onSubmit={submit} noValidate aria-label="New task">
      <label htmlFor="description" className="sr-only">Task description</label>
      <textarea id="description" ref={input} rows={1} maxLength={5000} value={value} disabled={submitting}
        placeholder={hasTask ? 'Message AgentX…' : 'Message AgentX…'}
        aria-invalid={Boolean(error)} aria-describedby={`composer-hint${error ? ' form-error' : ''}`}
        onChange={event => onChange(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (!submitting) onSubmit(); } }} />
      <div className="composer-bottom"><span className="composer-mode"><Icon name="spark" />AgentX</span>
        <div className="composer-controls"><span className="character-count" aria-label={`${value.length} of 5000 characters`}>{value.length ? `${value.length.toLocaleString()} / 5,000` : 'Enter to send'}</span>
          <button type="submit" className="send-button" disabled={submitting} aria-label={submitting ? responding ? 'AgentX is responding…' : 'Saving task…' : 'Send task'} title="Send task">
            {submitting ? <span className="spinner" /> : <Icon name="arrow" />}
          </button></div></div>
    </form>
    {error && <p id="form-error" role="alert" className="composer-error">{error}</p>}
    <p id="composer-hint" className="composer-hint">{hasTask ? 'Continue this conversation. Each turn keeps its own agent activity. ' : 'Shift + Enter for a new line. '}Answers use OpenAI. Code is shown, not run. Tools require approval.</p>
  </div>;
}
