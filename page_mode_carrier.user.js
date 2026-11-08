// ==UserScript==
// @name         DeeperSeeker Page Mode Carrier
// @namespace    deeperseeker-page-mode
// @version      0.1
// @description  Pick up pending URLs from the bridge and load them in the logged-in official page, so the page sends the message itself. The server reads the reply back.
// @match        https://chat.deepseek.com/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// ==/UserScript==

const BRIDGE = GM_getValue("BRIDGE", "http://127.0.0.1:4000");
const KEY    = GM_getValue("KEY",    "");

const POLL_MS      = 4000;
const LOAD_GAP_MS  = 12000;
const RELOAD_GUARD = 60000;

(function () {
  "use strict";

  const seen = JSON.parse(GM_getValue("seen_urls", "{}"));
  const now = Date.now();
  for (const u of Object.keys(seen)) if (now - seen[u] > RELOAD_GUARD) delete seen[u];

  function key() {
    return KEY || localStorage.getItem("ds_page_mode_key") || "";
  }

  function pickNext() {
    GM_xmlhttpRequest({
      method: "GET",
      url: BRIDGE + "/page_mode/pending",
      headers: { "Authorization": "Bearer " + key() },
      onload(r) {
        if (r.status !== 200) return;
        let items = [];
        try { items = (JSON.parse(r.responseText).pending) || []; } catch (e) { return; }
        if (!items.length) return;
        const intent = items.find(it => it && it.url && !seen[it.url]) || items[0];
        if (!intent || seen[intent.url]) return;
        seen[intent.url] = now;
        GM_setValue("seen_urls", JSON.stringify(seen));
        console.log("[page-mode carrier] loading autosend url:", intent.prompt_preview);
        setTimeout(() => { location.href = intent.url; }, 300);
      },
      onerror()   {},
      ontimeout() {},
    });
  }

  setInterval(pickNext, POLL_MS);
  if (!/[?&](q|prompt)=/.test(location.search)) pickNext();
})();
