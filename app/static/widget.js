/*!
 * RAG Website Chatbot — embeddable widget
 *
 * Usage: <script src="https://your-host/widget.js" data-chatbot-id="CHATBOT_ID" defer></script>
 *
 * The chatbot ID identifies a server-side chatbot. The host page's Origin and
 * Referer are validated by the backend; no domain or API key is trusted from JS.
 */
(function () {
  "use strict";

  if (window.__ragChatbotLoaded) return; // guard against double-inject

  // ---- Resolve the backend base URL from this script's own src ----
  var current =
    document.currentScript ||
    (function () {
      var s = document.getElementsByTagName("script");
      return s[s.length - 1];
    })();
  var API_BASE;
  try {
    API_BASE = new URL(current.src).origin;
  } catch (e) {
    API_BASE = "";
  }

  // The opaque public chatbot id is the only tenant identifier in the embed code.
  // The browser supplies Origin and Referer automatically; the widget never sends a domain.
  var CHATBOT_ID = (current.getAttribute("data-chatbot-id") || "").trim();
  if (!CHATBOT_ID) return;
  var TITLE = (current.getAttribute("data-title") || "Ask about this site").trim();
  var ACCENT = (current.getAttribute("data-accent") || "#2563eb").trim();
  var sessionToken = null;
  var initialized = false;
  var greeted = false;

  function startWidget() {

  // ------------------------- styles -------------------------
  var css = `
  .ragc-launcher{position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;
    background:${ACCENT};color:#fff;border:none;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);
    display:flex;align-items:center;justify-content:center;z-index:2147483000;transition:transform .15s ease}
  .ragc-launcher:hover{transform:scale(1.06)}
  .ragc-launcher svg{width:26px;height:26px}
  .ragc-panel{position:fixed;bottom:88px;right:20px;width:360px;max-width:calc(100vw - 32px);height:520px;
    max-height:calc(100vh - 120px);background:#fff;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.22);
    display:none;flex-direction:column;overflow:hidden;z-index:2147483000;font-family:system-ui,-apple-system,
    Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  .ragc-panel.ragc-open{display:flex}
  .ragc-header{background:${ACCENT};color:#fff;padding:14px 16px;font-size:15px;font-weight:600;
    display:flex;align-items:center;justify-content:space-between;gap:8px}
  .ragc-header-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ragc-close{background:none;border:none;color:#fff;font-size:20px;cursor:pointer;line-height:1}
  .ragc-status{padding:6px 16px;font-size:12px;background:#eef2ff;color:#3730a3;border-bottom:1px solid #e5e7eb;
    display:flex;align-items:center;gap:7px}
  .ragc-status.ragc-ready{background:#ecfdf5;color:#065f46}
  .ragc-status.ragc-error{background:#fef2f2;color:#991b1b}
  .ragc-status.ragc-hidden{display:none}
  .ragc-dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex:none}
  .ragc-status.ragc-busy .ragc-dot{animation:ragc-pulse 1s ease-in-out infinite}
  @keyframes ragc-pulse{0%,100%{opacity:.35}50%{opacity:1}}
  .ragc-body{flex:1;padding:14px;overflow-y:auto;background:#f7f8fa;display:flex;flex-direction:column;gap:10px}
  .ragc-msg{max-width:85%;padding:9px 12px;border-radius:12px;font-size:14px;line-height:1.4;white-space:pre-wrap;
    word-wrap:break-word}
  .ragc-user{align-self:flex-end;background:${ACCENT};color:#fff;border-bottom-right-radius:4px}
  .ragc-bot{align-self:flex-start;background:#fff;color:#1f2937;border:1px solid #e5e7eb;border-bottom-left-radius:4px}
  .ragc-sources{font-size:11px;color:#6b7280;margin-top:4px}
  .ragc-sources a{color:${ACCENT};text-decoration:none;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ragc-typing{align-self:flex-start;color:#6b7280;font-size:13px;font-style:italic}
  .ragc-footer{display:flex;border-top:1px solid #e5e7eb;background:#fff;padding:10px}
  .ragc-input{flex:1;border:1px solid #d1d5db;border-radius:8px;padding:9px 11px;font-size:14px;outline:none}
  .ragc-input:focus{border-color:${ACCENT}}
  .ragc-send{margin-left:8px;background:${ACCENT};color:#fff;border:none;border-radius:8px;padding:0 14px;
    cursor:pointer;font-size:14px}
  .ragc-send:disabled{opacity:.5;cursor:not-allowed}
  `;
  var styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ------------------------- DOM -------------------------
  var launcher = document.createElement("button");
  launcher.className = "ragc-launcher";
  launcher.setAttribute("aria-label", "Open chat");
  launcher.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

  var panel = document.createElement("div");
  panel.className = "ragc-panel";
  panel.innerHTML =
    '<div class="ragc-header">' +
    '<span class="ragc-header-title">' + escapeHtml(TITLE) + "</span>" +
    '<button class="ragc-close" aria-label="Close">\u00d7</button>' +
    "</div>" +
    '<div class="ragc-status ragc-hidden" role="status" aria-live="polite">' +
    '<span class="ragc-dot"></span><span class="ragc-status-text"></span>' +
    "</div>" +
    '<div class="ragc-body" role="log" aria-live="polite"></div>' +
    '<div class="ragc-footer">' +
    '<input class="ragc-input" type="text" placeholder="Type your question..." aria-label="Message"/>' +
    '<button class="ragc-send">Send</button>' +
    "</div>";

  document.body.appendChild(launcher);
  document.body.appendChild(panel);

  var body = panel.querySelector(".ragc-body");
  var input = panel.querySelector(".ragc-input");
  var sendBtn = panel.querySelector(".ragc-send");
  var closeBtn = panel.querySelector(".ragc-close");
  var statusEl = panel.querySelector(".ragc-status");
  var statusText = panel.querySelector(".ragc-status-text");

  // Authenticate once. Crawling is an admin operation and never starts from this page load.

  // ------------------------- behavior -------------------------
  launcher.addEventListener("click", togglePanel);
  closeBtn.addEventListener("click", togglePanel);
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") send();
  });

  // The widget only checks readiness. It never starts or retries a crawl.
  initialize().then(function () { pollStatus(); }).catch(function () { /* generic status already shown */ });

  function togglePanel() {
    var open = panel.classList.toggle("ragc-open");
    if (open) {
      input.focus();
      if (!greeted) {
        greeted = true;
        addBot("Hi! Ask me anything about this site.");
      }
    }
  }

  function initialize() {
    if (initialized || !API_BASE || !CHATBOT_ID) return Promise.reject(new Error("unavailable"));
    initialized = true;
    return fetch(API_BASE + "/api/chat/init", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chatbot_id: CHATBOT_ID })
    }).then(function (r) { if (!r.ok) throw new Error("unavailable"); return r.json(); })
      .then(function (data) { sessionToken = data.sessionToken; return data; })
      .catch(function (err) { initialized = false; setStatus("Chatbot unavailable", "error"); throw err; });
  }

  function authorizedHeaders() {
    return { "Content-Type": "application/json", "Authorization": "Bearer " + (sessionToken || "") };
  }

  // Show a status line: "state" is "busy" | "ready" | "error".
  function setStatus(text, state) {
    if (!statusEl) return;
    statusText.textContent = text;
    statusEl.className =
      "ragc-status" +
      (state === "ready" ? " ragc-ready" : "") +
      (state === "error" ? " ragc-error" : "") +
      (state === "busy" ? " ragc-busy" : "");
  }

  // Readiness check only. This endpoint never starts a crawl.
  function pollStatus() {
    if (!API_BASE || !CHATBOT_ID || !sessionToken) return;
    fetch(API_BASE + "/api/status", { headers: authorizedHeaders() })
      .then(function (r) { if (r.status === 403) throw new Error("unavailable"); return r.json(); })
      .then(function (s) {
        if (s && (s.phase === "ready" || s.indexed)) {
          setStatus("Ready to answer", "ready");
        } else {
          setStatus("Knowledge base is being prepared by the site administrator", "busy");
        }
      })
      .catch(function () { setStatus("Chatbot unavailable", "error"); });
  }

  function send() {
    var text=input.value.trim(); if(!text)return;
    input.value=""; addUser(text); setBusy(true); var typing=addTyping();
    fetch(API_BASE+"/api/chat/stream",{method:"POST",headers:authorizedHeaders(),body:JSON.stringify({message:text})})
      .then(function(r){if(r.status===429)throw new Error("rate");if(!r.ok)throw new Error("unavailable");return r.body.getReader();})
      .then(function(reader){
        typing.remove();
        var decoder=new TextDecoder(),buffer="",botEl=document.createElement("div"),sources=[];
        botEl.className="ragc-msg ragc-bot"; body.appendChild(botEl);
        function consume(){return reader.read().then(function(part){
          buffer+=decoder.decode(part.value||new Uint8Array(),{stream:!part.done});
          var blocks=buffer.split("\\n\\n"); buffer=blocks.pop();
          blocks.forEach(function(block){var line=block.split("\\n").filter(function(x){return x.indexOf("data: ")===0;})[0];if(!line)return;var raw=line.slice(6);if(raw==="[DONE]")return;try{var event=JSON.parse(raw);if(event.type==="token"){botEl.appendChild(document.createTextNode(event.text));scroll();}if(event.type==="final")sources=event.sources||[];}catch(e){}});
          if(part.done){if(sources.length){var box=document.createElement("div");box.className="ragc-sources";box.appendChild(document.createTextNode("Sources:"));sources.slice(0,3).forEach(function(u){var a=document.createElement("a");a.href=u;a.target="_blank";a.rel="noopener noreferrer";a.textContent=u;box.appendChild(a);});botEl.appendChild(box);}return;}
          return consume();
        });}
        return consume();
      })
      .catch(function(err){typing.remove();if(err.message==="rate")addBot("You're sending messages too quickly. Please wait a moment.");else addBot("Something went wrong reaching the assistant. Please try again.");})
      .finally(function(){setBusy(false);input.focus();});
  }

  function setBusy(b) {
    sendBtn.disabled = b;
    input.disabled = b;
  }

  function addUser(text) {
    var el = document.createElement("div");
    el.className = "ragc-msg ragc-user";
    el.textContent = text;
    body.appendChild(el);
    scroll();
  }

  function addBot(text, sources) {
    var el = document.createElement("div");
    el.className = "ragc-msg ragc-bot";
    el.textContent = text;
    if (sources && sources.length) {
      var box = document.createElement("div");
      box.className = "ragc-sources";
      box.appendChild(document.createTextNode("Sources:"));
      sources.slice(0, 3).forEach(function (u) {
        var a = document.createElement("a");
        a.href = u;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = u;
        box.appendChild(a);
      });
      el.appendChild(box);
    }
    body.appendChild(el);
    scroll();
  }

  function addTyping() {
    var el = document.createElement("div");
    el.className = "ragc-typing";
    el.textContent = "Assistant is typing...";
    body.appendChild(el);
    scroll();
    return el;
  }

  function scroll() {
    body.scrollTop = body.scrollHeight;
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  }

  var currentDomain = (window.location.hostname || "").toLowerCase().replace(/^www\./, "");
  fetch(API_BASE + "/api/widget/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chatbot_id: CHATBOT_ID, current_domain: currentDomain, widget_version: "1.0.0" })
  }).then(function (response) {
    if (!response.ok) throw new Error("unavailable");
    return response.json();
  }).then(function (result) {
    if (!result || result.success !== true) return;
    window.__ragChatbotLoaded = true;
    startWidget();
  }).catch(function () {
    // Do not create any DOM when the domain is not authorized or is unavailable.
  });
})();
