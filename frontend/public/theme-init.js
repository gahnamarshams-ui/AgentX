// Runs before the application and its first paint. Storage may be unavailable.
(function () {
  var preference = 'system';
  try {
    var saved = window.localStorage.getItem('agentx.theme');
    if (saved === 'light' || saved === 'dark' || saved === 'system') preference = saved;
  } catch (_) { /* System remains usable when browser storage is blocked. */ }
  var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = preference === 'system' ? (dark ? 'dark' : 'light') : preference;
  document.documentElement.dataset.themePreference = preference;
}());
