(function(){
  const currentScript = document.currentScript;
  const tenant = currentScript.getAttribute('data-tenant');
  const role = currentScript.getAttribute('data-role');
  const color = currentScript.getAttribute('data-color') || '#2563eb';
  const avatar = currentScript.getAttribute('data-avatar') || '';

  // … instancie le widget avec ces options
  // Exemple minimal :
  const bubble = document.createElement('div');
  bubble.style = `
    position:fixed; right:20px; bottom:20px; width:64px; height:64px;
    border-radius:50%; background:${color}; display:flex; align-items:center; justify-content:center;
    box-shadow:0 10px 30px rgba(0,0,0,.35); cursor:pointer; z-index:999999;`;
  bubble.title = `Betty (${role}) — ${tenant}`;
  bubble.innerHTML = avatar ? `<img src="${avatar}" alt="Betty" style="width:60%;height:60%;border-radius:50%;">` : '💬';
  document.body.appendChild(bubble);

  // TODO: ouvrir ton panneau/chat, charger le profil selon role/tenant, etc.
})();
