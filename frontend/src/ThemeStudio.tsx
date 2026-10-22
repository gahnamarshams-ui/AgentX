import { useState } from 'react';
import type { ThemeState } from './theme';
import { ThemeSettings } from './ThemeControl';
import Icon from './Icon';

export default function ThemeStudio(theme: ThemeState) {
  const [notice, setNotice] = useState('');
  return <section className="theme-studio" aria-labelledby="studio-title">
    <header className="studio-heading"><div><span className="eyebrow">YOUR PREFERENCES</span><h2 id="studio-title">Appearance</h2></div>
      <button className="text-action studio-reset" onClick={() => { theme.reset(); setNotice('Restored System appearance.'); }}><Icon name="refresh" />Reset to default</button></header>
    <ThemeSettings {...theme} />
    <p className="studio-notice" role="status">{notice || 'A timeless black-and-white palette. Your appearance preference is saved in this browser.'}</p>
  </section>;
}
