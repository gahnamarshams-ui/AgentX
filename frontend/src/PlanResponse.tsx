import { useState } from 'react';
import type { Task, TaskPlan } from './api/client';
import Markdown from './Markdown';
import Icon from './Icon';

export default function PlanResponse({ plan, status = 'awaiting_approval' }: { plan: TaskPlan; status?: Task['status'] }) {
  const [copyState, setCopyState] = useState('Copy plan');
  async function copy() {
    const text = [`# ${plan.goal}`, '## Assumptions', ...plan.assumptions, '## Constraints', ...plan.constraints,
      '## Proposed steps', ...plan.steps.map(step => `${step.number}. ${step.action}\nPurpose: ${step.purpose}\nExpected output: ${step.expected_output}\nDependencies: ${step.dependencies.join(', ') || 'None'}`),
      '## Clarification', ...plan.clarification_questions].join('\n\n');
    try { await navigator.clipboard.writeText(text); setCopyState('Plan copied'); }
    catch { setCopyState('Could not copy. Select the plan text instead.'); }
  }
  return <div className="saved-plan">
    <div className="plan-intro"><span className="plan-label"><Icon name="check" />{['running', 'completed', 'failed', 'cancelled'].includes(status) ? 'SAVED PLAN' : 'PLAN READY FOR REVIEW'}</span><span className="muted">{plan.steps.length} proposed steps</span></div>
    <Markdown>{plan.goal}</Markdown>
    <div className="plan-context">
      <section><h3>Assumptions</h3>{plan.assumptions.length ? <ul>{plan.assumptions.map((item, i) => <li key={i}><Markdown>{item}</Markdown></li>)}</ul> : <p className="muted">None stated.</p>}</section>
      <section><h3>Constraints</h3>{plan.constraints.length ? <ul>{plan.constraints.map((item, i) => <li key={i}><Markdown>{item}</Markdown></li>)}</ul> : <p className="muted">None stated.</p>}</section>
    </div>
    <h3 className="steps-heading">Here’s the proposed approach</h3>
    <ol className="plan-steps">{plan.steps.map(step => <li key={step.number}>
      <span className="step-number" aria-hidden="true">{String(step.number).padStart(2, '0')}</span>
      <div><Markdown>{`### ${step.action}`}</Markdown><Markdown>{step.purpose}</Markdown>
        <div className="step-output"><span>EXPECTED OUTPUT</span><Markdown>{step.expected_output}</Markdown></div>
        <p className="dependencies">Dependencies: {step.dependencies.length ? step.dependencies.map(number => `Step ${number}`).join(', ') : 'None'}</p>
      </div></li>)}</ol>
    {plan.clarification_needed ? <section className="clarification"><h3>A few things to clarify</h3><ul>{plan.clarification_questions.map((question, i) => <li key={i}><Markdown>{question}</Markdown></li>)}</ul>
      <p className="muted">Answer the clarification to continue the original task before any execution.</p></section>
      : <p className="muted">No clarification requested by the planner.</p>}
    <p className="plan-disclaimer">{status === 'completed' ? 'The backend reports verified completion. See the saved result and task activity.' : status === 'running' ? 'Execution is active. Completion depends on actual output verification.' : ['failed', 'cancelled'].includes(status) ? 'Task stopped. Review task activity for actual outputs and errors.' : 'Plan saved for review. No steps have been executed.'}</p>
    <button className="text-action copy-action" onClick={() => void copy()}><Icon name={copyState === 'Plan copied' ? 'check' : 'copy'} />{copyState === 'Plan copied' ? copyState : 'Copy plan'}</button>
    <span className="copy-feedback" role="status">{copyState.startsWith('Could not') ? copyState : ''}</span>
  </div>;
}
