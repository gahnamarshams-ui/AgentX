import Icon from './Icon';
import type { ThemePreference, ResolvedTheme } from './theme';
const choices = [
  { value: 'light', label: 'Light', icon: 'sun', description: 'A fresh, lighter space' },
  { value: 'dark', label: 'Dark', icon: 'moon', description: 'Easy on the evening' },
  { value: 'system', label: 'System', icon: 'monitor', description: 'Match your device' },
] as const;
interface Props { preference: ThemePreference; resolved: ResolvedTheme; choose: (theme: ThemePreference) => void }
export function HeaderThemeControl({ preference, choose }: Props) {
  return <label className="header-theme-control" title="Appearance">
    <Icon name={preference === 'system' ? 'monitor' : preference === 'dark' ? 'moon' : 'sun'} />
    <span className="sr-only">Theme preference</span>
    <select value={preference} onChange={event => choose(event.target.value as ThemePreference)}>
      {choices.map(choice => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
    </select>
  </label>;
}
export function ThemeSettings({ preference, resolved, choose }: Props) {
  return <fieldset className="theme-settings"><legend>Appearance</legend>
    <p className="muted" id="theme-description">Choose a space that feels right for you.</p>
    <div className="theme-options">{choices.map(choice => <label className="theme-option" key={choice.value}>
      <input className="sr-only" type="radio" name="appearance" value={choice.value} checked={preference === choice.value}
        aria-label={choice.label} aria-describedby="theme-description" onChange={() => choose(choice.value)} />
      <span className="theme-option-body"><span className="theme-option-top"><Icon name={choice.icon} /><span className="theme-radio-dot" /></span>
        <strong>{choice.label}</strong><span>{choice.description}</span></span>
    </label>)}</div>
    <p className="theme-summary" role="status">{preference === 'system' ? `Following your device · ${resolved === 'dark' ? 'Dark' : 'Light'} mode. Updates automatically.` : `${preference === 'dark' ? 'Dark' : 'Light'} mode. Your device setting won’t override this choice.`}</p>
  </fieldset>;
}
