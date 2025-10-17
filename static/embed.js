(function () {
  // ------- SAFE GUARD: trouver le <script> et empêcher les doubles inits -------
  /** @type {HTMLScriptElement|null} */
  var script =
    document.currentScript ||
    (function () {
      var all = document.getElementsByTagName("script");
      for (var i = all.length - 1; i >= 0; i--) {
        if ((all[i].src || "").includes("embed.js")) return all[i];
      }
      return null;
    })();

  if (!script) return;

  // Espace global (évite double init par tenant)
  window.__BETTY__ = window.__BETTY__ || { widgets: {} };

  // ------- Lecture des attributs data-* avec fallback -------
  function ds(attr, def) {
    var val = script.getAttribute("data-" + attr);
    return (val && String(val).trim()) || def;
  }

  var tenant = ds("tenant", "demo-tenant");
  var role = ds("role", "psychologue");
  var color = ds("color", "#2563eb");
  var avatar = ds("avatar", "");

  // Si déjà chargé pour ce tenant, on ne réinjecte pas
  if (window.__BETTY__.widgets[tenant]) return;

  // ------- Utilitaires -------
  function createEl(tag, props, children) {
    var el = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        if (k === "style") {
          Object.assign(el.style, props.style || {});
        } else if (k in el) {
          el[k] = props[k];
        } else {
          el.setAttribute(k, props[k]);
        }
      });
    }
    (children || []).forEach(function (c) {
      if (typeof c === "string") el.appendChild(document.createTextNode(c));
      else if (c) el.appendChild(c);
    });
    return el;
  }

  function ensureStyle(id, css) {
    if (document.getElementById(id)) return;
    var s = document.createElement("style");
    s.id = id;
    s.textContent = css;
    document.head.appendChild(s);
  }

  // Contraste icône si couleur très claire
  function isLight(hex) {
    var h = (hex || "").replace("#", "");
    if (h.length === 3) {
      h = h.split("").map(function (c) { return c + c; }).join("");
    }
    if (h.length !== 6) return false;
    var r = parseInt(h.slice(0, 2), 16);
    var g = parseInt(h.slice(2, 4), 16);
    var b = parseInt(h.slice(4, 6), 16);
    var l = (0.299 * r + 0.587 * g + 0.114 * b); // luminance approx
    return l > 186;
  }

  // ------- Styles (scopés par classes .betty-*) -------
  ensureStyle(
    "betty-widget-styles",
    ".betty-bubble{position:fixed;right:20px;bottom:20px;width:64px;height:64px;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 10px 30px rgba(0,0,0,.35);cursor:pointer;z-index:2147483646;border:none}"+
    ".betty-bubble img{width:60%;height:60%;border-radius:50%;object-fit:cover}"+
    ".betty-panel{position:fixed;right:20px;bottom:96px;width:360px;max-width:calc(100vw - 40px);height:520px;background:#0b1220;color:#e5e7eb;border:1px solid #1f2937;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.45);display:flex;flex-direction:column;overflow:hidden;z-index:2147483646}"+
    ".betty-header{display:flex;align-items:center;gap:10px;padding:12px 14px;color:#fff}"+
    ".betty-ava{width:32px;height:32px;border-radius:50%;overflow:hidden;flex:0 0 auto;border:2px solid rgba(255,255,255,.6)}"+
    ".betty-ava img{width:100%;height:100%;object-fit:cover}"+
    ".betty-title{font-weight:700;line-height:1.1}"+
    ".betty-sub{font-size:12px;opacity:.85}"+
    ".betty-body{flex:1;overflow:auto;padding:14px;background:linear-gradient(180deg,rgba(255,255,255,0.02),rgba(255,255,255,0));}"+
    ".betty-msg{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:10px 12px;margin:6px 0;max-width:85%}"+
    ".betty-msg.me{margin-left:auto;background:#0f172a}"+
    ".betty-input{display:flex;gap:8px;padding:10px;border-top:1px solid #1f2937;background:#0b1220}"+
    ".betty-input input{flex:1;padding:12px 12px;border-radius:10px;border:1px solid #334155;background:#0a0f1c;color:#e5e7eb}"+
    ".betty-input button{padding:12px 14px;border-radius:10px;border:none;background:#334155;color:#fff;cursor:pointer}"+
    ".betty-close{margin-left:auto;background:rgba(0,0,0,.15);border:none;border-radius:8px;color:#fff;padding:6px 8px;cursor:pointer}"+
    ".betty-badge{font-size:11px;opacity:.9}"
  );

  // ------- Bulle flottante -------
  var bubble = createEl("button", {
    className: "betty-bubble",
    title: "Ouvrir Betty (" + role + ") — " + tenant,
    "aria-label": "Ouvrir le chat Betty",
    style: {
      background: color,
      color: isLight(color) ? "#111827" : "#ffffff"
    }
  });

  if (avatar) {
    var img = createEl("img", { src: avatar, alt: "Betty" });
    bubble.appendChild(img);
  } else {
    // petit icône chat en SVG (contraste auto)
    var svg = createEl("div", {
      innerHTML:
        '<svg viewBox="0 0 24 24" width="28" height="28" fill="currentColor" aria-hidden="true"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9.5l-4.2 3.15A1 1 0 0 1 4 20.35V18H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>'
    });
    bubble.style.color = isLight(color) ? "#111827" : "#ffffff";
    bubble.appendChild(svg);
  }

  document.body.appendChild(bubble);

  // ------- Panneau (drawer) -------
  var panel = createEl("div", { className: "betty-panel", style: { display: "none" } });

  var header = createEl("div", {
    className: "betty-header",
    style: { background: color }
  });

  var ava = createEl("div", { className: "betty-ava" }, [
    avatar
      ? createEl("img", { src: avatar, alt: "Betty" })
      : createEl("div", {
          style: {
            width: "100%",
            height: "100%",
            background: isLight(color) ? "#111827" : "#ffffff",
            borderRadius: "50%"
          }
        })
  ]);

  var titleWrap = createEl("div", { style: { display: "grid" } }, [
    createEl("div", { className: "betty-title", textContent: "Betty — " + role }),
    createEl("div", {
      className: "betty-sub",
      innerHTML: 'Assistant·e gentille <span class="betty-badge">tenant: ' + tenant + "</span>"
    })
  ]);

  var closeBtn = createEl("button", {
    className: "betty-close",
    innerHTML: "✕",
    title: "Fermer",
    "aria-label": "Fermer le chat"
  });

  header.appendChild(ava);
  header.appendChild(titleWrap);
  header.appendChild(closeBtn);

  var body = createEl("div", { className: "betty-body" });
  // Message d'accueil
  body.appendChild(
    createEl("div", {
      className: "betty-msg",
      innerHTML:
        "Bonjour ✨ Je suis <strong>Betty</strong>. Posez-moi une question, je vous réponds avec bienveillance."
    })
  );

  var input = createEl("div", { className: "betty-input" });
  var field = createEl("input", {
    type: "text",
    placeholder: "Écrivez votre message…"
  });
  var send = createEl("button", { textContent: "Envoyer" });

  input.appendChild(field);
  input.appendChild(send);

  panel.appendChild(header);
  panel.appendChild(body);
  panel.appendChild(input);
  document.body.appendChild(panel);

  // ------- Interactions -------
  function openPanel() {
    panel.style.display = "flex";
    setTimeout(function () {
      field.focus();
    }, 0);
  }
  function closePanel() {
    panel.style.display = "none";
  }

  bubble.addEventListener("click", openPanel);
  closeBtn.addEventListener("click", closePanel);

  function pushUserMessage(text) {
    var msg = createEl("div", { className: "betty-msg me", textContent: text });
    body.appendChild(msg);
    body.scrollTop = body.scrollHeight;
  }
  function pushBettyMessage(text) {
    var msg = createEl("div", { className: "betty-msg", innerHTML: text });
    body.appendChild(msg);
    body.scrollTop = body.scrollHeight;
  }

  function handleSend() {
    var val = (field.value || "").trim();
    if (!val) return;
    pushUserMessage(val);
    field.value = "";

    // Ici tu peux brancher ton backend (fetch) selon tenant/role
    // Placeholder de réponse:
    setTimeout(function () {
      pushBettyMessage("Je transmets votre demande à <strong>" + role + "</strong>. Comment puis-je vous aider davantage ?");
    }, 300);
  }

  send.addEventListener("click", handleSend);
  field.addEventListener("keydown", function (e) {
    if (e.key === "Enter") handleSend();
  });

  // ------- Expose (debug minimal) -------
  window.__BETTY__.widgets[tenant] = {
    tenant: tenant,
    role: role,
    color: color,
    avatar: avatar,
    open: openPanel,
    close: closePanel
  };
})();
