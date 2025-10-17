(function(){
  if (window.__BETTY_WIDGET__) return; // anti double-injection
  window.__BETTY_WIDGET__ = true;

  const currentScript = document.currentScript;
  const tenant = currentScript.getAttribute('data-tenant') || 'demo';
  const role = currentScript.getAttribute('data-role') || 'psychologue';
  const color = currentScript.getAttribute('data-color') || '#2563eb';
  const avatar = currentScript.getAttribute('data-avatar') || '';

  // Bulle
  const bubble = document.createElement('div');
  bubble.style.cssText = `
    position:fixed; right:20px; bottom:20px; width:64px; height:64px; border-radius:50%;
    background:${color}; display:flex; align-items:center; justify-content:center;
    box-shadow:0 10px 30px rgba(0,0,0,.35); cursor:pointer; z-index:2147483647;
  `;
  bubble.title = `Betty (${role}) — ${tenant}`;
  bubble.innerHTML = avatar ? `<img src="${avatar}" alt="Betty" style="width:60%;height:60%;border-radius:50%;">` : '💬';
  document.body.appendChild(bubble);

  // Panneau
  const panel = document.createElement('div');
  panel.style.cssText = `
    position:fixed; right:20px; bottom:100px; width:360px; height:520px; border-radius:16px;
    background:#0b1220; color:#e5e7eb; border:1px solid #1f2937; box-shadow:0 20px 60px rgba(0,0,0,.45);
    display:none; overflow:hidden; z-index:2147483647; font-family:system-ui, -apple-system, Segoe UI, Roboto, Arial;
  `;
  panel.innerHTML = `
    <div style="background:${color};padding:12px 14px;font-weight:700;">Betty — ${role}</div>
    <div style="padding:12px;height:calc(520px - 54px);overflow:auto;">
      <p>Bonjour ! Je suis Betty 🤖. Posez votre question…</p>
      <div style="opacity:.6;font-size:12px">Tenant: ${tenant}</div>
    </div>
  `;
  document.body.appendChild(panel);

  bubble.addEventListener('click', ()=>{
    panel.style.display = (panel.style.display === 'none' || !panel.style.display) ? 'block' : 'none';
  });
})();
