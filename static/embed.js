diff --git a/templates/embed.html b/templates/embed.html
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/templates/embed.html
@@ -0,0 +1,420 @@
+<!doctype html>
+<html lang="fr">
+<head>
+  <meta charset="utf-8">
+  <meta name="viewport" content="width=device-width,initial-scale=1">
+  <title>Betty Bot</title>
+
+  {# Le serveur fournit: bot (id, pack, name, avatar_url), brand flags #}
+  {% set bot_name  = (bot.name if bot and bot.name else 'Mon Betty Bot') %}
+  {% set pack_slug = (bot.slug if bot and bot.slug else 'agent_immobilier') %}
+  {% set avatar_url = (bot.avatar_url if bot and bot.avatar_url else '/avatar/' + pack_slug) %}
+  {% set show_brand = (bot.show_brand if bot and (bot.show_brand is not none) else True) %}
+  {% set brand_text = bot.brand_text if (bot and bot.brand_text) else 'Betty Bot — propulsé par Spectra Media' %}
+  {% set brand_link = bot.brand_link if (bot and bot.brand_link) else 'https://spectramedia.ai' %}
+  {% set welcome = bot.welcome_text if (bot and bot.welcome_text) else 'Bonjour 👋' %}
+
+  <style>
+    :root{
+      --bg:#0d0f13; --card:#0f1116; --line:#21242b;
+      --text:#e7eaf1; --muted:#a8b0c0; --primary:#6366f1;
+    }
+    *{box-sizing:border-box}
+    html,body{height:100%}
+    body{
+      margin:0;background:transparent;color:var(--text);
+      font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif
+    }
+
+    .widget{
+      width:100%;height:100%;
+      display:flex;flex-direction:column;
+      background:var(--card);border:1px solid var(--line);
+      border-radius:18px;overflow:hidden
+    }
+    .brand{
+      height:34px;background:#0c0f15;border-bottom:1px solid rgba(255,255,255,.06);
+      font:500 12px/34px Inter,system-ui,sans-serif;text-align:center;letter-spacing:.2px
+    }
+    .brand a{color:#a5b4fc;text-decoration:none}
+
+    .head{display:flex;gap:10px;align-items:center;padding:12px}
+    .avatar{width:44px;height:44px;border-radius:10px;border:1px solid var(--line);overflow:hidden;background:#0b0e12}
+    .avatar img{width:100%;height:100%;object-fit:cover}
+    .title{display:flex;flex-direction:column}
+    .title b{font-size:15px}
+    .title span{font-size:12px;color:var(--muted)}
+
+    .chat{flex:1;display:flex;flex-direction:column;padding:12px}
+    .messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:10px;padding-right:6px}
+    .bubble{max-width:85%;padding:10px 12px;border-radius:14px;line-height:1.35;border:1px solid var(--line);white-space:pre-wrap}
+    .me{align-self:flex-end;background:#131726}
+    .bot{align-self:flex-start;background:#0f131b}
+    .bubble.warn{color:#ffdb6e;border-color:#3b2f0b;background:#1c1606}
+
+    .input{display:flex;gap:8px;margin-top:12px}
+    .input input{
+      flex:1;padding:12px;border-radius:12px;border:1px solid var(--line);
+      background:#0b0e14;color:var(--text)
+    }
+    .input button{
+      padding:12px 14px;border-radius:12px;border:0;background:var(--primary);
+      color:#fff;font-weight:600;cursor:pointer
+    }
+    .input button[disabled]{opacity:.6;cursor:not-allowed}
+  </style>
+</head>
+<body>
+  <div class="widget" role="region" aria-label="Betty Bot">
+    {% if show_brand %}
+      <div class="brand"><a href="{{ brand_link }}" target="_blank" rel="noopener">{{ brand_text }}</a></div>
+    {% endif %}
+
+    <div class="head">
+      <div class="avatar"><img alt="avatar" src="{{ avatar_url }}"></div>
+      <div class="title">
+        <b>{{ bot_name }}</b>
+        <span>Pack : {{ {'agent_immobilier':'Agent immobilier','avocat':'Avocat','medecin':'Médecin','coiffeur':'Coiffeur','coach_sportif':'Coach sportif'}.get(pack_slug, pack_slug) }}</span>
+      </div>
+    </div>
+
+    <div class="chat">
+      <div id="messages" class="messages" aria-live="polite"></div>
+      <div class="input">
+        <input id="msg" placeholder="Écrivez votre message…" autocomplete="off" />
+        <button id="send">Envoyer</button>
+      </div>
+    </div>
+  </div>
+
+  <script>
+    // Contexte
+    const BOT_ID   = {{ (bot.id if bot else None)|tojson }};
+    const PACK     = {{ pack_slug|tojson }};
+    const GREETING = {{ welcome|tojson }};
+
+    const FIRST_QUESTION = {
+      agent_immobilier: "Souhaitez-vous acheter, vendre ou louer ? Sur quelle zone ?",
+      avocat:           "Quel type de dossier (famille, travail, pénal...) et quel degré d’urgence ?",
+      medecin:          "Quel est votre motif de consultation et vos disponibilités ?",
+      coiffeur:         "Quel service souhaitez-vous et quand êtes-vous disponible ?",
+      coach_sportif:    "Quel est votre objectif (perte de poids, remise en forme…) et vos créneaux ?"
+    }[PACK] || "Pouvez-vous préciser votre besoin ?";
+
+    // Lead
+    const lead = { nom:"", prenom:"", telephone:"", email:"" };
+    let leadStep = 0;
+    const emailRe = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]{2,}$/i;
+
+    const messagesEl = document.getElementById('messages');
+    const msgInput   = document.getElementById('msg');
+    const sendBtn    = document.getElementById('send');
+
+    function addBubble(text, who='bot', cls){
+      const b = document.createElement('div');
+      b.className = 'bubble ' + (who==='me'?'me':'bot') + (cls?(' '+cls):'');
+      b.textContent = String(text||'');
+      messagesEl.appendChild(b); messagesEl.scrollTop = messagesEl.scrollHeight;
+    }
+    function nextLeadQuestion(){
+      if (leadStep===0) return addBubble("Quel est votre NOM ?");
+      if (leadStep===1) return addBubble("Et votre PRÉNOM ?");
+      if (leadStep===2) return addBubble("Votre TÉLÉPHONE (mobile de préférence) ?");
+      if (leadStep===3) return addBubble("Enfin, votre ADRESSE EMAIL ?");
+    }
+    function acceptLeadAnswer(t){
+      const s=(t||'').trim(); if(!s) return false;
+      if (leadStep===0){ lead.nom=s.toUpperCase(); leadStep=1; return true; }
+      if (leadStep===1){ lead.prenom=s.charAt(0).toUpperCase()+s.slice(1); leadStep=2; return true; }
+      if (leadStep===2){
+        const d=s.replace(/[^\\d+]/g,'');
+        if (d.length<8){ addBubble("Le numéro semble trop court. Pouvez-vous le vérifier ?",'bot','warn'); return true; }
+        lead.telephone=d; leadStep=3; return true;
+      }
+      if (leadStep===3){
+        if (!emailRe.test(s)){ addBubble("L’email ne semble pas valide. Pouvez-vous le retaper ?",'bot','warn'); return true; }
+        lead.email=s; leadStep=4; return true;
+      }
+      return false;
+    }
+    function leadComplete(){ return !!(lead.nom && lead.prenom && lead.telephone && lead.email); }
+
+    async function submitLead(){
+      addBubble(`Merci ${lead.prenom} ${lead.nom}. J’ai bien noté :
+- Téléphone : ${lead.telephone}
+- Email : ${lead.email}
+Un conseiller vous rappellera rapidement.`);
+      try{
+        await fetch('/api/lead',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
+          name:`${lead.prenom} ${lead.nom}`,
+          email:lead.email,
+          message:`Lead ${PACK}\\nNom:${lead.nom}\\nPrénom:${lead.prenom}\\nTéléphone:${lead.telephone}\\nEmail:${lead.email}`,
+          extra:{metier:PACK, bot_id:BOT_ID}
+        })});
+      }catch(e){}
+      addBubble(FIRST_QUESTION);
+    }
+
+    async function callChatAPI(text){
+      const res = await fetch('/api/chat',{
+        method:'POST',
+        headers:{'Content-Type':'application/json'},
+        body:JSON.stringify({
+          message:text, pack:PACK, bot_id:BOT_ID,
+          history:Array.from(messagesEl.querySelectorAll('.bubble.me')).map(b=>b.textContent),
+          lead
+        })
+      });
+      if (!res.ok) throw new Error('http '+res.status);
+      const data = await res.json().catch(()=>null);
+      return (data && typeof data.reply==='string') ? data.reply : null;
+    }
+
+    let sending=false;
+    async function send(text){
+      const t=(text||'').trim(); if(!t||sending) return;
+      sending=true; sendBtn.disabled=true;
+      addBubble(t,'me'); msgInput.value='';
+
+      const progressed = acceptLeadAnswer(t);
+      if (progressed){
+        if (leadStep<4){ nextLeadQuestion(); sending=false; sendBtn.disabled=false; msgInput.focus(); return; }
+        if (leadComplete()) await submitLead();
+      }
+      try{
+        const reply = await callChatAPI(t);
+        if (reply) addBubble(reply,'bot');
+        else addBubble('⚠️ Erreur serveur.','bot','warn');
+      }catch(e){
+        addBubble('⚠️ Impossible de joindre le serveur.','bot','warn');
+      }finally{
+        sending=false; sendBtn.disabled=false; msgInput.focus();
+      }
+    }
+
+    sendBtn.addEventListener('click',()=>send(msgInput.value));
+    msgInput.addEventListener('keydown',(e)=>{ if(e.key==='Enter'){ e.preventDefault(); send(msgInput.value); }});
+
+    // Boot
+    (()=>{
+      addBubble(GREETING);
+      nextLeadQuestion();
+      addBubble(FIRST_QUESTION);
+      msgInput.focus();
+    })();
+  </script>
+</body>
+</html>
