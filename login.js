document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target, errEl = document.getElementById('login-error');
  errEl.textContent = '';
  try {
    const r = await api('/api/login', { method: 'POST', body: { name: f.name.value, password: f.password.value } });
    location.href = r.redirect;
  } catch (ex) { errEl.textContent = ex.message; }
});
