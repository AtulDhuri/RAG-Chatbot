/*!
 * ChatOS — premium embeddable website assistant
 *
 * Usage: <script src="https://your-host/widget.js" data-chatbot-id="CHATBOT_ID" defer></script>
 */
(function () {
  "use strict";

  if (window.__ragChatbotLoaded) return;

  var current = document.currentScript || (function () {
    var scripts = document.getElementsByTagName("script");
    return scripts[scripts.length - 1];
  })();
  var API_BASE;
  try { API_BASE = new URL(current.src).origin; } catch (e) { API_BASE = ""; }

  var CHATBOT_ID = (current.getAttribute("data-chatbot-id") || "").trim();
  if (!CHATBOT_ID) return;
  var WIDGET_VERSION = "portal-analytics-1";
  if (window.console && console.info) console.info("ChatOS widget loaded: " + WIDGET_VERSION);
  var ACCENT = (current.getAttribute("data-accent") || "#6366F1").trim();
  var SUGGESTIONS = (current.getAttribute("data-suggestions") || "Summarize this website,Contact or support").split(",").map(function (item) { return item.trim(); }).filter(Boolean).slice(0, 6);
  var sessionToken = null;
  var initialized = false;
  var greeted = false;
  var conversationId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();

  function startWidget() {
    var css = `
      .ragc-launcher{position:fixed;right:24px;bottom:24px;width:60px;height:60px;border:1px solid rgba(255,255,255,.14);border-radius:50%;background:#111318;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:2147483000;box-shadow:0 12px 34px rgba(0,0,0,.42),0 0 0 5px rgba(99,102,241,.08);transition:transform .22s cubic-bezier(.2,.8,.2,1),box-shadow .22s ease,background .22s ease;animation:ragc-launcher-in .55s ease both}
      .ragc-launcher:hover{transform:scale(1.07);background:#181b24;box-shadow:0 16px 38px rgba(0,0,0,.48),0 0 0 7px rgba(99,102,241,.12)}
      .ragc-launcher:focus-visible,.ragc-close:focus-visible,.ragc-chip:focus-visible,.ragc-send:focus-visible{outline:2px solid #818CF8;outline-offset:3px}
      .ragc-launcher.ragc-first-visit{animation:ragc-launcher-in .55s ease both,ragc-once-pulse 1.2s .8s ease-out 1}
      .ragc-launcher svg{width:25px;height:25px}
      .ragc-panel{position:fixed;right:24px;bottom:98px;width:390px;max-width:calc(100vw - 32px);height:440px;max-height:calc(100vh - 122px);background:#111318;border:1px solid rgba(255,255,255,.08);border-radius:24px;box-shadow:0 24px 70px rgba(0,0,0,.55);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;color:#fff;font-family:Geist,Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;opacity:0;transform:translateY(12px) scale(.98)}
      .ragc-panel.ragc-open{display:flex;animation:ragc-panel-in .24s cubic-bezier(.2,.8,.2,1) forwards}
      .ragc-header{display:flex;align-items:center;gap:11px;padding:18px 18px 16px;background:#111318;border-bottom:1px solid rgba(255,255,255,.07)}
      .ragc-brand-mark{width:34px;height:34px;display:grid;place-items:center;border:1px solid rgba(129,140,248,.35);border-radius:11px;background:rgba(99,102,241,.15);color:#A5B4FC;flex:none}
      .ragc-brand-mark svg{width:19px;height:19px}.ragc-heading{min-width:0;flex:1}.ragc-title{font-size:18px;line-height:1.15;font-weight:600;letter-spacing:-.02em}.ragc-subtitle{color:#A1A1AA;font-size:12px;margin-top:4px}
      .ragc-close{width:44px;height:44px;flex:none;border:0;border-radius:10px;background:transparent;color:#A1A1AA;cursor:pointer;font-size:23px;line-height:1;transition:background .18s ease,color .18s ease}.ragc-close:hover{background:rgba(255,255,255,.07);color:#fff}
      .ragc-status{display:flex;align-items:center;gap:7px;padding:8px 20px;color:#A1A1AA;font-size:11px;min-height:29px}.ragc-status.ragc-hidden{display:none}.ragc-status.ragc-ready{color:#86EFAC}.ragc-status.ragc-error{color:#FCA5A5}.ragc-dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}.ragc-status.ragc-busy .ragc-dot{animation:ragc-dot-pulse 1.1s ease-in-out infinite}
      .ragc-body{flex:1;min-height:0;padding:22px 18px 16px;overflow-y:auto;background:#0B0B0F;display:flex;flex-direction:column;gap:13px;scroll-behavior:smooth}.ragc-body::-webkit-scrollbar{width:6px}.ragc-body::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:10px}
      .ragc-welcome{padding:5px 2px 4px;animation:ragc-message-in .3s ease both}.ragc-welcome h2{margin:0 0 8px;font-size:20px;line-height:1.25;letter-spacing:-.025em;font-weight:600}.ragc-welcome p{margin:0;color:#A1A1AA;font-size:14px;line-height:1.5}.ragc-suggestion-label{margin:0 0 10px;color:#71717A;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em}.ragc-chips{display:flex;flex-wrap:wrap;gap:8px}.ragc-chip{padding:9px 12px;border:1px solid rgba(255,255,255,.1);border-radius:999px;background:#111318;color:#D4D4D8;font:inherit;font-size:13px;cursor:pointer;transition:border-color .18s ease,background .18s ease,color .18s ease,transform .18s ease}.ragc-chip:hover{border-color:rgba(129,140,248,.65);background:rgba(99,102,241,.12);color:#fff;transform:translateY(-1px)}
      .ragc-msg{max-width:87%;padding:12px 14px;border-radius:17px;font-size:16px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;word-wrap:break-word;animation:ragc-message-in .24s ease both}.ragc-user{align-self:flex-end;background:#4F46B5;color:#fff;border-bottom-right-radius:6px}.ragc-bot{align-self:flex-start;background:#111318;color:#F4F4F5;border:1px solid rgba(255,255,255,.1);border-bottom-left-radius:6px}.ragc-sources{margin-top:11px;padding-top:9px;border-top:1px solid rgba(255,255,255,.08);color:#71717A;font-size:11px;line-height:1.5}.ragc-sources a{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#A5B4FC;text-decoration:none;margin-top:3px}.ragc-sources a:hover{text-decoration:underline;color:#C7D2FE}
      .ragc-typing{align-self:flex-start;display:flex;align-items:center;gap:4px;padding:13px 15px;border:1px solid rgba(255,255,255,.1);border-radius:17px;border-bottom-left-radius:6px;background:#111318;animation:ragc-message-in .24s ease both}.ragc-typing i{width:5px;height:5px;border-radius:50%;background:#A1A1AA;animation:ragc-typing 1s ease-in-out infinite}.ragc-typing i:nth-child(2){animation-delay:.15s}.ragc-typing i:nth-child(3){animation-delay:.3s}
      .ragc-skeleton{height:13px;border-radius:8px;background:linear-gradient(90deg,#171920 25%,#242733 50%,#171920 75%);background-size:200% 100%;animation:ragc-shimmer 1.5s infinite;margin-top:auto;width:62%;opacity:.8}.ragc-skeleton.short{width:43%;margin-top:0}
      .ragc-footer{padding:12px 14px 8px;background:#111318;border-top:1px solid rgba(255,255,255,.07)}.ragc-composer{display:flex;align-items:center;gap:8px;padding:5px 5px 5px 14px;border:1px solid rgba(255,255,255,.12);border-radius:16px;background:#0B0B0F;transition:border-color .18s ease,box-shadow .18s ease}.ragc-composer:focus-within{border-color:rgba(129,140,248,.75);box-shadow:0 0 0 3px rgba(99,102,241,.13)}.ragc-input{flex:1;min-width:0;border:0;outline:0;background:transparent;color:#fff;caret-color:#A5B4FC;font:inherit;font-size:16px;line-height:1.4;padding:8px 0;appearance:none}.ragc-input::placeholder{color:#71717A;opacity:1}.ragc-send{width:44px;height:44px;flex:none;border:0;border-radius:50%;background:#6366F1;color:#fff;display:grid;place-items:center;cursor:pointer;transition:transform .18s ease,background .18s ease,opacity .18s ease}.ragc-send:hover{background:#818CF8;transform:scale(1.06)}.ragc-send:active{transform:scale(.94)}.ragc-send:disabled{opacity:.4;cursor:not-allowed;transform:none}.ragc-send svg{width:17px;height:17px}.ragc-powered{display:block;padding-top:8px;color:#71717A;font-size:10px;text-align:center;text-decoration:none}.ragc-powered:hover{color:#A1A1AA;text-decoration:underline}
      @keyframes ragc-panel-in{to{opacity:1;transform:translateY(0) scale(1)}}@keyframes ragc-launcher-in{from{opacity:0;transform:scale(.7)}to{opacity:1;transform:scale(1)}}@keyframes ragc-once-pulse{0%{box-shadow:0 12px 34px rgba(0,0,0,.42),0 0 0 0 rgba(99,102,241,.35)}100%{box-shadow:0 12px 34px rgba(0,0,0,.42),0 0 0 14px rgba(99,102,241,0)}}@keyframes ragc-message-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}@keyframes ragc-dot-pulse{0%,100%{opacity:.35}50%{opacity:1}}@keyframes ragc-typing{0%,60%,100%{opacity:.35;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}@keyframes ragc-shimmer{to{background-position:-200% 0}}
      @media(max-width:520px){.ragc-launcher{right:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom))}.ragc-panel{right:0;bottom:0;width:100%;max-width:none;height:var(--ragc-vh,100vh);max-height:var(--ragc-vh,100vh);border-radius:0;border-left:0;border-right:0}.ragc-header{padding: max(14px,env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) 14px max(16px,env(safe-area-inset-left))}.ragc-footer{padding:12px max(14px,env(safe-area-inset-right)) max(14px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left))}.ragc-body{padding-left:16px;padding-right:16px}.ragc-msg{max-width:91%}}
      @media(prefers-reduced-motion:reduce){.ragc-launcher,.ragc-panel,.ragc-msg,.ragc-typing,.ragc-skeleton,.ragc-typing i{animation:none!important;transition:none!important}}
    `;
    var styleEl = document.createElement("style"); styleEl.textContent = css; document.head.appendChild(styleEl);

    var launcher = document.createElement("button");
    launcher.className = "ragc-launcher ragc-first-visit";
    launcher.setAttribute("aria-label", "Open assistant");
    launcher.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 8.8 8.8 0 0 1-3.3-.65L4 20l1.65-3.9A7.2 7.2 0 0 1 4.5 12C4.5 7.86 8 4.5 12.5 4.5S20 7.64 20 11.5Z"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01"/></svg>';

    var panel = document.createElement("div");
    panel.className = "ragc-panel";
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "Website assistant");
    panel.innerHTML = '<div class="ragc-header"><div class="ragc-heading"><div class="ragc-title">Website Assistant</div><div class="ragc-subtitle">Ask anything about this website</div></div><button class="ragc-close" aria-label="Close assistant">×</button></div><div class="ragc-status ragc-hidden" role="status" aria-live="polite"><span class="ragc-dot"></span><span class="ragc-status-text"></span></div><div class="ragc-body" role="log" aria-live="polite"></div><div class="ragc-footer"><div class="ragc-composer"><input class="ragc-input" type="text" placeholder="Ask anything..." aria-label="Message"/><button class="ragc-send" aria-label="Send message"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 14-8-3 16-4-6-7-2Z"/><path d="M12 14 19 4"/></svg></button></div><a class="ragc-powered" href="https://chatos.cloud" target="_blank" rel="noopener noreferrer">Powered by ChatOS · Analytics enabled</a></div>';

    document.body.appendChild(launcher); document.body.appendChild(panel);

    function updateViewportHeight() {
      if (window.visualViewport && window.innerWidth <= 520) {
        panel.style.setProperty("--ragc-vh", window.visualViewport.height + "px");
      }
    }
    updateViewportHeight();
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", updateViewportHeight);
      window.visualViewport.addEventListener("scroll", updateViewportHeight);
    }
    window.addEventListener("resize", updateViewportHeight);
    var body = panel.querySelector(".ragc-body"), input = panel.querySelector(".ragc-input"), sendBtn = panel.querySelector(".ragc-send"), closeBtn = panel.querySelector(".ragc-close"), statusEl = panel.querySelector(".ragc-status"), statusText = panel.querySelector(".ragc-status-text");

    launcher.addEventListener("click", togglePanel); closeBtn.addEventListener("click", togglePanel); sendBtn.addEventListener("click", send);
    input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });

    initialize().then(function () { pollStatus(); }).catch(function () {});

    function togglePanel() {
      var open = panel.classList.toggle("ragc-open"); launcher.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) { launcher.classList.remove("ragc-first-visit"); input.focus(); if (!greeted) { greeted = true; addWelcome(); } }
    }
    function initialize() {
      if (initialized || !API_BASE || !CHATBOT_ID) return Promise.reject(new Error("unavailable")); initialized = true;
      return fetch(API_BASE + "/api/chat/init", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chatbot_id: CHATBOT_ID }) }).then(function (r) { if (!r.ok) throw new Error("unavailable"); return r.json(); }).then(function (data) { sessionToken = data.sessionToken; return data; }).catch(function (err) { initialized = false; setStatus("Chatbot unavailable", "error"); throw err; });
    }
    function authorizedHeaders() { return { "Content-Type": "application/json", "Authorization": "Bearer " + (sessionToken || "") }; }
    function setStatus(text, state) { statusText.textContent = text; statusEl.className = "ragc-status" + (state === "ready" ? " ragc-ready" : "") + (state === "error" ? " ragc-error" : "") + (state === "busy" ? " ragc-busy" : ""); }
    function pollStatus() { if (!API_BASE || !CHATBOT_ID || !sessionToken) return; fetch(API_BASE + "/api/status", { headers: authorizedHeaders() }).then(function (r) { if (r.status === 403) throw new Error("unavailable"); return r.json(); }).then(function (s) { removeSkeleton(); if (s && (s.phase === "ready" || s.indexed)) setStatus("Online and ready", "ready"); else setStatus("Getting things ready", "busy"); }).catch(function () { removeSkeleton(); setStatus("Chatbot unavailable", "error"); }); }

    function send() {
      var text = input.value.trim(); if (!text || sendBtn.disabled) return; input.value = ""; addUser(text); setBusy(true); var typing = addTyping();
      fetch(API_BASE + "/api/chat/stream", { method: "POST", headers: authorizedHeaders(), body: JSON.stringify({ message: text, conversation_id: conversationId }) }).then(function (r) { if (r.status === 429) throw new Error("rate"); if (!r.ok) throw new Error("unavailable"); return r.body.getReader(); }).then(function (reader) {
        typing.remove(); var decoder = new TextDecoder(), buffer = "", botEl = document.createElement("div"), sources = []; botEl.className = "ragc-msg ragc-bot"; body.appendChild(botEl);
        function consume() { return reader.read().then(function (part) { buffer += decoder.decode(part.value || new Uint8Array(), { stream: !part.done }); var blocks = buffer.split("\n\n"); buffer = blocks.pop(); blocks.forEach(function (block) { var line = block.split("\n").filter(function (x) { return x.indexOf("data: ") === 0; })[0]; if (!line) return; var raw = line.slice(6); if (raw === "[DONE]") return; try { var event = JSON.parse(raw); if (event.type === "token") { botEl.appendChild(document.createTextNode(event.text)); scroll(); } if (event.type === "final") sources = event.sources || []; } catch (e) {} }); if (part.done) { if (sources.length) appendSources(botEl, sources); checkLeadIntent(text); return; } return consume(); }); }
        return consume();
      }).catch(function (err) { if (typing.parentNode) typing.remove(); addBot(err.message === "rate" ? "You're sending messages too quickly. Please wait a moment." : "Something went wrong reaching the assistant. Please try again."); }).finally(function () { setBusy(false); input.focus(); });
    }
    function checkLeadIntent(message) { fetch(API_BASE + "/api/chat/lead-intent", { method: "POST", headers: authorizedHeaders(), body: JSON.stringify({ message: message, conversation_id: conversationId }) }).then(function (r) { return r.ok ? r.json() : null; }).then(function (result) { if (result && result.offer_contact) offerLead(); }).catch(function () {}); }
    function offerLead() { var el = document.createElement("div"); el.className = "ragc-msg ragc-bot"; el.appendChild(document.createTextNode("Would you like our team to contact you?")); var button = document.createElement("button"); button.className = "ragc-chip"; button.type = "button"; button.textContent = "Yes, contact me"; button.style.marginTop = "10px"; button.addEventListener("click", function () { var name = window.prompt("Your name:"); if (!name) return; var email = window.prompt("Your email:"); if (!email) return; var phone = window.prompt("Your phone number:"); if (!phone) return; var interest = window.prompt("What are you interested in?"); if (!interest) return; fetch(API_BASE + "/api/leads", { method: "POST", headers: authorizedHeaders(), body: JSON.stringify({ conversation_id: conversationId, name: name, email: email, phone: phone, interested_in: interest }) }).then(function (r) { if (!r.ok) throw new Error(); return r.json(); }).then(function () { button.disabled = true; button.textContent = "Thanks, we'll be in touch"; }).catch(function () { addBot("We couldn't save your details. Please try again."); }); }); el.appendChild(document.createElement("br")); el.appendChild(button); body.appendChild(el); scroll(); }
    function setBusy(b) { sendBtn.disabled = b; input.disabled = b; }
    function addWelcome() { var el = document.createElement("div"); el.className = "ragc-welcome"; el.innerHTML = '<div class="ragc-suggestion-label">Try asking</div><div class="ragc-chips"></div>'; var chips = el.querySelector(".ragc-chips"); SUGGESTIONS.forEach(function (suggestion) { var chip = document.createElement("button"); chip.className = "ragc-chip"; chip.type = "button"; chip.textContent = suggestion; chip.addEventListener("click", function () { input.value = suggestion; send(); }); chips.appendChild(chip); }); body.appendChild(el); var skeleton = document.createElement("div"); skeleton.className = "ragc-skeleton"; skeleton.setAttribute("aria-hidden", "true"); body.appendChild(skeleton); var shortSkeleton = document.createElement("div"); shortSkeleton.className = "ragc-skeleton short"; shortSkeleton.setAttribute("aria-hidden", "true"); body.appendChild(shortSkeleton); scroll(); } function removeSkeleton() { Array.prototype.forEach.call(body.querySelectorAll(".ragc-skeleton"), function (el) { el.remove(); }); }
    function addUser(text) { var el = document.createElement("div"); el.className = "ragc-msg ragc-user"; el.textContent = text; body.appendChild(el); scroll(); }
    function appendSources(el, sources) { var box = document.createElement("div"); box.className = "ragc-sources"; box.appendChild(document.createTextNode("Sources")); sources.slice(0, 3).forEach(function (u) { var a = document.createElement("a"); a.href = u; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = u; box.appendChild(a); }); el.appendChild(box); }
    function addBot(text, sources) { var el = document.createElement("div"); el.className = "ragc-msg ragc-bot"; el.textContent = text; if (sources && sources.length) appendSources(el, sources); body.appendChild(el); scroll(); }
    function addTyping() { var el = document.createElement("div"); el.className = "ragc-typing"; el.setAttribute("aria-label", "Assistant is typing"); el.innerHTML = "<i></i><i></i><i></i>"; body.appendChild(el); scroll(); return el; }
    function scroll() { body.scrollTop = body.scrollHeight; }
  }

  var currentDomain = (window.location.hostname || "").toLowerCase().replace(/^www\./, "");
  fetch(API_BASE + "/api/widget/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chatbot_id: CHATBOT_ID, current_domain: currentDomain, widget_version: "2.0.0" }) }).then(function (response) { if (!response.ok) throw new Error("unavailable"); return response.json(); }).then(function (result) { if (!result || result.success !== true) return; window.__ragChatbotLoaded = true; startWidget(); }).catch(function () {});
})();
