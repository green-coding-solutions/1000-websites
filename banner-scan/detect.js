// Find consent banners and other overlays that cover a page.
//
// worker.py sends this file through Marionette's WebDriver:ExecuteScript with
// "return detectBanner();" appended, so it runs in the page with the page's own
// globals in reach. detectBanner() resolves to one plain JSON object.
//
// It looks in four ways, and the verdict uses all of them.
//
//   1. Known CMPs. Globals and DOM markers of about 40 consent management
//      platforms. A marker in the DOM is not a banner, because most CMPs leave
//      their markup in place after a choice and only hide it. A known CMP counts
//      only when one of its banner elements is visible AND on top, checked with
//      elementFromPoint.
//   2. What is on top. A grid of points over the viewport, each resolved with
//      elementFromPoint (descending into open shadow roots). Every hit is mapped
//      to its innermost fixed or sticky ancestor (or an absolute one with a
//      z-index of 1000 and more). Those ancestors are the overlays, and the
//      share of grid points each one wins is its coverage. Their text, the
//      text of their open shadow roots and the src of their iframes are matched
//      against consent vocabulary in the languages of the site lists.
//   3. Inline notices. Elements with cookie, consent, gdpr or cmp in their id
//      or class that sit in the viewport in the normal flow of the page.
//   4. Side signals. The IAB TCF API's ping (displayStatus "visible" means the
//      CMP says its UI is up), a consent page in the final URL (zeit.de and
//      Yahoo redirect to one), a scroll lock on html or body, and the wording
//      of bot walls.
//
// The verdict is "banner" if any consent evidence is on screen. It is "unsure"
// if something big covers the page without consent wording or the page looks
// broken. It is "none" only when neither holds. "none" is meant to be
// trustworthy, so doubtful cases go to "unsure" and not to "none".

async function detectBanner() {
  const VW = window.innerWidth;
  const VH = window.innerHeight;
  const out = {
    viewport: [VW, VH],
    url: location.href,
    title: document.title,
    cmps: [],            // CMPs whose globals or markup are present
    cmp_visible: [],     // known CMP banner elements visible and on top
    overlays: [],        // fixed or sticky things that win grid points
    inline: [],          // in-flow consent notices in the viewport
    tcf: null,
    consent_url: false,
    scroll_locked: false,
    bot_wall: '',
    blank: false,
    verdict: 'none',
    evidence: [],
  };
  if (!document.body || !VW || !VH) {
    out.verdict = 'unsure';
    out.evidence.push('no body or no viewport');
    return out;
  }

  // ---- vocabulary --------------------------------------------------------

  // Words that name consent or cookies. One of them is enough.
  const STRONG = new RegExp([
    'cookie', 'consent', 'gdpr', 'dsgvo', 'einwillig', 'datenschutzeinstellung',
    'consentement', 'traceurs', 'consentimiento', 'consentimento', 'consenso',
    'toestemming', 'zgod[aęy]', 'ciasteczk', 'plików cookie', 'согласи', 'куки',
    'cookie-файл', 'クッキー', '同意', '쿠키', 'çerez', 'kvkk', 'souhlas', 'súhlas',
    'hozzájárul', 'süti', 'samtyk', 'samtyck', 'evästee', 'suostum', 'συγκατάθεσ',
    'consimț', 'privacy choices', 'your privacy', 'ihre privatsphäre',
    'votre vie privée', 'su privacidad', 'la tua privacy', 'uw privacy',
  ].join('|'), 'i');
  // Words that only hint at it. Two different ones are needed.
  const WEAK = /privacy|datenschutz|privacidad|privacidade|confidentialité|riservatezza|prywatno|конфиденциальн|プライバシー|隐私|隱私|개인정보|gizlilik|personal data|personenbezogen|données personnelles|dati personali|datos personales|partner|tracking|werbung|advertis|publicité|publicidad|pubblicità/gi;
  // What the buttons of a consent banner say.
  const ACTION = new RegExp([
    '\\baccept', '\\bagree', '\\ballow', '\\bgot it', '\\breject', '\\bdecline', '\\bdeny',
    '\\brefuse', '\\bi understand', '\\bunderstood', '\\bmanage (options|preferences|settings|choices|cookies)',
    '\\bcustomi[sz]e', '\\bcontinue without', '\\bok\\b', '\\bokay\\b', '\\bclose\\b',
    'akzeptier', 'zustimm', 'einverstanden', 'ablehnen', 'erlauben', 'verstanden', 'annehmen',
    'einstellungen', 'accepter', "j'accepte", 'refuser', 'continuer sans', 'paramétrer',
    'acepto', 'aceptar', 'rechazar', 'configurar', 'accett', 'rifiut', 'aceit', 'rejeit',
    'recus', 'accepteer', 'akkoord', 'weiger', 'zaakceptuj', 'akceptuj', 'odrzu', 'zgadzam',
    'принять', 'принимаю', 'согласен', 'отклонить', 'хорошо', 'kabul', 'reddet', 'anladım',
    'tamam', 'přijmout', 'odmítnout', 'elfogad', 'godta', 'acceptera', 'hyväksy', 'συμφων',
    'αποδοχ', '同意', '接受', '拒绝', '拒絕', '拒否', '承諾', '동의', '수락', '거부', '허용',
  ].join('|'), 'i');
  const IFRAME_CMP = /sp_message|privacy-mgmt|sourcepoint|consensu\.org|consent|\bcmp|cookie|gdpr|trustarc|truste|fundingchoices|didomi|usercentrics|onetrust|cookielaw|quantcast|evidon|cookiebot|privacy/i;
  // Bot walls and error pages. They are not banners, but a page showing one is
  // not the site either, so "none" would be wrong for it. All three seen in the
  // validation run (zeit.de, nytimes.com, ebay.de) also show up in Firefox
  // without Marionette, so they come from the proxy path and not from
  // navigator.webdriver.
  const BOT_WALL = /verify you are human|are you a robot|are you a human|confirm that you are human|prove your humanity|unusual traffic|checking your browser|just a moment\.\.\.|access denied|access to this page has been denied|pardon our interruption|bot or not|captcha|request blocked|you have been blocked|slide right to secure|anfrage wurde blockiert|zugriff verweigert|etwas schiefgelaufen|something went wrong|error page|page not found|seite nicht gefunden|403 forbidden|502 bad gateway|503 service|accès refusé|acceso denegado|accesso negato/i;

  function consentMatch(text, extra) {
    const t = text || '';
    const strong = t.match(STRONG);
    const weak = new Set((t.match(WEAK) || []).map(s => s.toLowerCase()));
    const action = t.match(ACTION);
    const vocab = !!strong || weak.size >= 2 || !!extra;
    return {
      ok: vocab && (!!action || !!extra),
      words: [strong && strong[0], ...weak, action && action[0], extra].filter(Boolean).slice(0, 5),
    };
  }

  // ---- DOM helpers -------------------------------------------------------

  const styles = new Map();
  function style(el) {
    let s = styles.get(el);
    if (!s) { s = getComputedStyle(el); styles.set(el, s); }
    return s;
  }
  // The parent in the composed (rendered) tree. Slotted content goes to its
  // slot first. reddit.com puts its banner into a <dialog> inside a shadow
  // root through a slot, and the plain parentElement walk never sees that
  // dialog, which is the fixed element.
  function parentOf(el) {
    if (el.assignedSlot) return el.assignedSlot;
    if (el.parentElement) return el.parentElement;
    const p = el.parentNode;
    return p && p.host ? p.host : null;
  }
  function contains(root, el) {
    for (let e = el; e; e = parentOf(e)) if (e === root) return true;
    return false;
  }
  function hitAt(x, y) {
    let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }
  function shown(el) {
    if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < VW && r.top < VH;
  }
  function visibleRect(el) {
    const r = el.getBoundingClientRect();
    const x0 = Math.max(0, r.left), y0 = Math.max(0, r.top);
    const x1 = Math.min(VW, r.right), y1 = Math.min(VH, r.bottom);
    return { x0, y0, x1, y1, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0) };
  }
  function areaPct(el) {
    const v = visibleRect(el);
    return Math.round(1000 * v.w * v.h / (VW * VH)) / 10;
  }
  // Share of sample points inside the element's visible box where the element
  // (or something inside it) is what the user sees.
  function onTop(el) {
    const v = visibleRect(el);
    if (v.w < 2 || v.h < 2) return 0;
    let n = 0, hit = 0;
    for (let i = 1; i <= 5; i++) {
      for (let j = 1; j <= 5; j++) {
        const e = hitAt(v.x0 + v.w * i / 6, v.y0 + v.h * j / 6);
        n++;
        if (e && contains(el, e)) hit++;
      }
    }
    return hit / n;
  }
  // Visible text, including open shadow roots and button labels.
  function deepText(root, max) {
    const parts = [];
    let len = 0;
    (function walk(node) {
      if (len > max) return;
      if (node.nodeType === 3) {
        const t = node.nodeValue.replace(/\s+/g, ' ').trim();
        if (t) { parts.push(t); len += t.length + 1; }
        return;
      }
      if (node.nodeType === 1) {
        const tag = node.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE' || tag === 'SVG') return;
        const s = style(node);
        if (s.display === 'none') return;
        if (tag === 'INPUT' && /^(button|submit)$/i.test(node.type) && node.value) parts.push(node.value);
        if ((tag === 'BUTTON' || node.getAttribute('role') === 'button') && node.getAttribute('aria-label')) {
          parts.push(node.getAttribute('aria-label'));
        }
        // A shadow host shows its shadow tree, and its own children only
        // through the slots in there.
        if (node.shadowRoot) {
          walk(node.shadowRoot);
          return;
        }
        // A slot shows what is assigned to it, not its own children.
        if (tag === 'SLOT') {
          const assigned = node.assignedNodes({ flatten: true });
          if (assigned.length) {
            for (const c of assigned) walk(c);
            return;
          }
        }
      } else if (node.nodeType !== 11) {
        return;
      }
      for (const c of node.childNodes) walk(c);
    })(root);
    return parts.join(' ').slice(0, max);
  }
  function deepQueryAll(selector, root, acc) {
    acc = acc || [];
    root = root || document;
    try { acc.push(...root.querySelectorAll(selector)); } catch (e) { return acc; }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) deepQueryAll(selector, el.shadowRoot, acc);
    }
    return acc;
  }
  function describe(el) {
    let d = el.tagName.toLowerCase();
    if (el.id) d += '#' + el.id;
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
    if (cls) d += '.' + cls;
    return d.slice(0, 80);
  }
  function snippet(text) {
    return (text || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  }

  // ---- 1. known CMPs -----------------------------------------------------

  // name, globals, markers (present at all), banner selectors (the part that
  // is visible while the CMP asks).
  const CMPS = [
    ['OneTrust', ['OneTrust', 'Optanon'], '#onetrust-consent-sdk', '#onetrust-banner-sdk, #onetrust-pc-sdk, .onetrust-pc-dark-filter'],
    ['Cookiebot', ['Cookiebot'], '#CybotCookiebotDialog, script[src*="cookiebot.com"]', '#CybotCookiebotDialog'],
    ['Didomi', ['Didomi'], '#didomi-host', '#didomi-popup, #didomi-notice, .didomi-popup-backdrop'],
    ['Usercentrics', ['UC_UI', 'usercentrics', '__ucCmp'], '#usercentrics-root, #usercentrics-cmp-ui', '#usercentrics-root, #usercentrics-cmp-ui'],
    ['Sourcepoint', ['_sp_', '_sp_queue'], '[id^="sp_message_container_"], iframe[id^="sp_message_iframe_"]', '[id^="sp_message_container_"], iframe[id^="sp_message_iframe_"]'],
    ['Google Funding Choices', ['googlefc'], '.fc-consent-root', '.fc-consent-root .fc-dialog-container, .fc-consent-root .fc-dialog-overlay'],
    ['consentmanager', ['__cmp', 'cmpmngr'], '#cmpbox, #cmpwrapper', '#cmpbox, #cmpbox2, .cmpboxBG'],
    ['Quantcast', ['__qc', 'quantserve'], '#qc-cmp2-container, .qc-cmp2-container', '#qc-cmp2-container .qc-cmp2-summary-section, #qc-cmp2-ui, .qc-cmp-cleanslate'],
    ['TrustArc', ['truste', 'PrivacyManagerAPI'], '#truste-consent-track, #consent_blackbar, .truste_box_overlay, iframe[src*="trustarc.com"]', '#truste-consent-track, #truste-consent-content, #consent_blackbar > *, .truste_box_overlay, .truste_popframe, iframe[src*="consent-pref.trustarc.com"]'],
    ['CookieYes', ['getCkyConsent', 'cookieyes'], '.cky-consent-container, .cky-modal', '.cky-consent-container:not(.cky-hide), .cky-modal.cky-modal-open'],
    ['Borlabs', ['BorlabsCookie', 'borlabsCookieConfig'], '#BorlabsCookieBox, #BorlabsCookieBoxWrap', '#BorlabsCookieBox ._brlbs-bar, #BorlabsCookieBox ._brlbs-box, #BorlabsCookieBox .brlbs-cmpnt-dialog, #BorlabsCookieBox [role="dialog"]'],
    ['Complianz', ['complianz', 'cmplz_set_cookie'], '#cmplz-cookiebanner-container, .cmplz-cookiebanner', '.cmplz-cookiebanner.cmplz-show, #cmplz-cookiebanner-container .cmplz-cookiebanner:not(.cmplz-hidden)'],
    ['iubenda', ['_iub'], '#iubenda-cs-banner', '#iubenda-cs-banner'],
    ['Osano', ['Osano'], '.osano-cm-window', '.osano-cm-dialog:not(.osano-cm-dialog--hidden), .osano-cm-info-dialog-open'],
    ['Axeptio', ['axeptioSDK', '_axcb', 'axeptioSettings'], '#axeptio_overlay, .axeptio_widget', '#axeptio_overlay .axeptio_mount, #axeptio_main_button, .axeptio_widget'],
    ['Klaro', ['klaro', 'klaroConfig'], '.klaro', '.klaro .cookie-notice:not(.cookie-notice-hidden), .klaro .cookie-modal'],
    ['CookieConsent (orestbida)', ['CookieConsent', 'initCookieConsent'], '#cc-main, #cc--main, #cc_div', '#cc-main .cm, #cc-main .pm, #cc--main #cm, #cc_div #cm'],
    ['Cookie Consent (Osano legacy)', ['cookieconsent'], '.cc-window', '.cc-window:not(.cc-invisible), .cc-banner:not(.cc-invisible)'],
    ['Termly', ['Termly'], '#termly-code-snippet-support, [class^="termly-styles-root"]', '#termly-code-snippet-support > div, [class^="termly-styles-root"]'],
    ['Civic Cookie Control', ['CookieControl'], '#ccc', '#ccc-notify, #ccc-overlay, #ccc-module'],
    ['Sirdata', ['Sddan'], '#sd-cmp', '#sd-cmp > div'],
    ['Ketch', ['ketch', 'semaphore'], '#lanyard_root', '#lanyard_root > div'],
    ['Evidon / Crownpeak', ['evidon'], '#_evidon_banner, #_evidon-barrier-wrapper', '#_evidon_banner, #_evidon-barrier-wrapper, #_evidon-message'],
    ['TrustCommander', ['tC'], '#footer_tc_privacy, #tc-privacy-wrapper, #privacy-iframe', '#footer_tc_privacy, #tc-privacy-wrapper, #privacy-iframe'],
    ['Cookie Information', ['CookieInformation'], '#coiOverlay, #cookie-information-template-wrapper', '#coiOverlay, #coiConsentBanner'],
    ['CookieFirst', ['CookieFirst'], '.cookiefirst-root, [data-cookiefirst-widget]', '.cookiefirst-root > div, [data-cookiefirst-widget="modal"]'],
    ['HubSpot', ['_hsp'], '#hs-eu-cookie-confirmation', '#hs-eu-cookie-confirmation'],
    ['Shopify', [], '#shopify-pc__banner', '#shopify-pc__banner'],
    ['Ezoic', ['ezCMP'], '#ez-cookie-dialog-wrapper', '#ez-cookie-dialog-wrapper, #ez-cookie-dialog'],
    ['Truendo', ['Truendo'], '#truendo_container', '#truendo_container > div'],
    ['CCM19', ['CCM'], '.ccm-root, #ccm-widget', '.ccm-root .ccm-modal--open, .ccm-root .ccm-widget, #ccm-widget'],
    ['Admiral', ['admiral'], '[class*="admiral-"]', ''],
    ['Wix', [], '[data-hook="consent-banner-root"]', '[data-hook="consent-banner-root"]'],
    ['Microsoft WCP', ['WcpConsent'], '#wcpConsentBannerCtrl', '#wcpConsentBannerCtrl'],
    ['Amazon', [], '#sp-cc, #sp-cc-wrapper', '#sp-cc, #sp-cc-wrapper'],
    ['Google', [], '#L2AGLb, #W0wltc', '[role="dialog"]:has(#L2AGLb), [role="dialog"]:has(#W0wltc)'],
    ['YouTube', [], 'ytd-consent-bump-v2-lightbox', 'ytd-consent-bump-v2-lightbox tp-yt-paper-dialog'],
    ['Meta', [], '[data-testid="cookie-policy-manage-dialog"], [data-cookiebanner="banner"]', '[data-testid="cookie-policy-manage-dialog"], [data-cookiebanner="banner"]'],
    ['BBC', [], '#bbccookies', '#bbccookies-banner, #bbccookies .cookie-banner'],
    ['Reddit', [], 'reddit-cookie-banner, shreddit-async-loader[bundlename*="cookie"]', 'reddit-cookie-banner'],
    ['DPG Media', [], '#pg-host-shadow-root, #pg-root', '#pg-host-shadow-root, #pg-root'],
    ['LiveRamp', ['__lxG__consent__v2', 'faktor'], '#_lr_cmp, [class*="faktor"]', ''],
    ['Contentpass', ['contentpass'], '[id*="contentpass"]', ''],
  ];
  const known = [];
  for (const [name, globals, markers, banners] of CMPS) {
    let present = globals.some(g => { try { return window[g] !== undefined; } catch (e) { return false; } });
    const markerEls = markers ? deepQueryAll(markers) : [];
    if (markerEls.length) present = true;
    if (!present) continue;
    out.cmps.push(name);
    if (!banners) continue;
    for (const el of deepQueryAll(banners)) {
      known.push([name, el]);
    }
  }
  if (typeof window.__tcfapi === 'function') out.cmps.push('IAB TCF v2 (__tcfapi)');
  if (typeof window.__gpp === 'function') out.cmps.push('IAB GPP (__gpp)');
  if (typeof window.__uspapi === 'function') out.cmps.push('IAB USP (__uspapi)');

  // The banner element of a known CMP may be a host without a box of its own
  // (Usercentrics, Sourcepoint's container) with the visible part inside. So a
  // host counts as visible when it or a descendant is on top.
  for (const [name, el] of known) {
    if (!shown(el)) {
      // display: contents hosts have no box, so look at their shadow content.
      if (!(el.shadowRoot && style(el).display === 'contents')) continue;
    }
    let top = onTop(el);
    let area = areaPct(el);
    if (el.shadowRoot && (top === 0 || area === 0)) {
      for (const inner of el.shadowRoot.querySelectorAll('*')) {
        if (!shown(inner)) continue;
        const a = areaPct(inner);
        if (a < 1) continue;
        const t = onTop(inner);
        if (t > top) { top = t; area = a; }
      }
    }
    if (top >= 0.2 && area >= 0.5) {
      out.cmp_visible.push({
        cmp: name, element: describe(el), area_pct: area, on_top: Math.round(top * 100),
        text: snippet(deepText(el, 400)),
      });
    }
  }

  // ---- 2. what is on top --------------------------------------------------

  function overlayRoot(el) {
    for (let e = el; e && e !== document.body && e !== document.documentElement; e = parentOf(e)) {
      const s = style(e);
      if (s.position === 'fixed' || s.position === 'sticky') return e;
      if (s.position === 'absolute' && parseInt(s.zIndex, 10) >= 1000) return e;
    }
    return null;
  }
  const COLS = 36, ROWS = 24;
  const hits = new Map();
  let gridPoints = 0;
  function sample(x, y, counted) {
    const el = hitAt(x, y);
    if (!el) return;
    const root = overlayRoot(el);
    if (!root) return;
    let h = hits.get(root);
    if (!h) { h = { n: 0, iframes: new Set() }; hits.set(root, h); }
    if (counted) h.n++;
    if (el.tagName === 'IFRAME') h.iframes.add(el);
  }
  for (let i = 0; i < COLS; i++) {
    for (let j = 0; j < ROWS; j++) {
      gridPoints++;
      sample((i + 0.5) * VW / COLS, (j + 0.5) * VH / ROWS, true);
    }
  }
  // Thin bars along the edges fall between grid rows. They are sampled too,
  // without counting towards coverage.
  for (let i = 0; i < COLS; i++) {
    for (const y of [3, 12, VH - 12, VH - 3]) sample((i + 0.5) * VW / COLS, y, false);
  }

  for (const [root, h] of hits) {
    const coverage = Math.round(1000 * h.n / gridPoints) / 10;
    const area = areaPct(root);
    const text = deepText(root, 4000);
    const iframes = [...new Set([...h.iframes, ...root.querySelectorAll('iframe')])];
    const iframePct = Math.max(0, ...iframes.filter(shown).map(areaPct));
    const iframeInfo = iframes.map(f =>
      [f.id, f.name, f.title, f.getAttribute('src')].filter(Boolean).join(' ')).filter(Boolean);
    const cmpIframe = iframeInfo.find(s => IFRAME_CMP.test(s));
    let cmp = '';
    for (const [name, el] of known) if (contains(el, root) || contains(root, el)) { cmp = name; break; }
    const m = consentMatch(text, cmpIframe ? 'iframe ' + cmpIframe.slice(0, 80) : (cmp ? 'cmp ' + cmp : ''));
    // A fixed container that holds the whole page (an app shell) is not an
    // overlay. It has a lot of text and many links, and a banner has neither.
    const links = root.querySelectorAll('a[href]').length;
    const shell = coverage >= 60 && (text.length > 3000 || links > 40) && !m.ok;
    out.overlays.push({
      element: describe(root), cmp, coverage, area_pct: area, consent: m.ok, words: m.words,
      iframes: iframeInfo.slice(0, 3).map(s => s.slice(0, 120)), iframe_pct: iframePct, shell, links,
      position: style(root).position, text: snippet(text),
    });
  }
  out.overlays.sort((a, b) => b.coverage - a.coverage);
  out.overlays = out.overlays.slice(0, 8);

  // ---- 3. inline notices ---------------------------------------------------

  const seen = new Set();
  // id and class only. An aria-label is running text: youtube.com labels a
  // paragraph about watch history with a sentence that mentions cookies.
  const inlineSel = '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], [id*="gdpr" i], [class*="gdpr" i], [id*="cmp" i]';
  for (const el of deepQueryAll(inlineSel).slice(0, 400)) {
    if (seen.has(el)) continue;
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.top >= VH || r.bottom <= 0) continue;
    const area = areaPct(el);
    if (area < 1.5 || area > 70) continue;
    if (overlayRoot(el)) continue;           // part 2 covers it
    // Only the outermost match counts.
    let outer = false;
    for (let e = parentOf(el); e; e = parentOf(e)) if (seen.has(e)) { outer = true; break; }
    seen.add(el);
    if (outer) continue;
    const text = deepText(el, 3000);
    if (text.length > 2500) continue;
    const m = consentMatch(text, '');
    if (!m.ok) continue;
    if (onTop(el) < 0.3) continue;
    out.inline.push({ element: describe(el), area_pct: area, words: m.words, text: snippet(text) });
  }

  // ---- 4. side signals -----------------------------------------------------

  if (typeof window.__tcfapi === 'function') {
    out.tcf = await new Promise(resolve => {
      let done = false;
      const finish = v => { if (!done) { done = true; resolve(v); } };
      try {
        window.__tcfapi('ping', 2, p => finish(p ? {
          cmpLoaded: p.cmpLoaded, cmpStatus: p.cmpStatus, displayStatus: p.displayStatus, cmpId: p.cmpId,
        } : { empty: true }));
      } catch (e) {
        finish({ error: String(e).slice(0, 100) });
      }
      setTimeout(() => finish({ timeout: true }), 1500);
    });
  }

  const u = new URL(location.href);
  if (/^(consent|myprivacy|guce|privacy|cmp)\./i.test(u.hostname) ||
      /zustimmung|\/consent\b|privacy-gate|cookiewall|cookie-wall|\/gdpr\b|consent-wall/i.test(u.pathname)) {
    out.consent_url = true;
  }

  const html = style(document.documentElement), body = style(document.body);
  out.scroll_locked = html.overflow === 'hidden' || html.overflowY === 'hidden' ||
    body.overflow === 'hidden' || body.overflowY === 'hidden' || body.position === 'fixed';

  // The title counts on any page. The body text only on short pages, because a
  // news site can run a headline saying "access denied" and still be the site.
  const bodyText = (document.body.innerText || '').trim();
  const botHit = (document.title + ' ' + (bodyText.length < 5000 ? bodyText.slice(0, 2000) : '')).match(BOT_WALL);
  if (botHit) out.bot_wall = botHit[0];
  // DataDome's captcha on nytimes.com is an empty page with one iframe the size
  // of the viewport, titled "DataDome CAPTCHA". Its text is out of reach, so
  // the iframe's src and title decide. A page that is mostly one iframe of
  // any other kind is not the site we can judge either.
  for (const f of document.querySelectorAll('iframe')) {
    if (!shown(f)) continue;
    const label = (f.getAttribute('src') || '') + ' ' + (f.title || '');
    const pct = areaPct(f);
    if (pct >= 10 && /captcha|challenge|perimeterx|px-cloud|datadome|hcaptcha|turnstile/i.test(label)) {
      out.bot_wall = out.bot_wall || ('iframe ' + label.trim().slice(0, 80));
    } else if (pct >= 60 && bodyText.length < 200 && !overlayRoot(f)) {
      out.bot_wall = out.bot_wall || ('page is one iframe: ' + label.trim().slice(0, 80));
    }
  }

  const media = [...document.querySelectorAll('img, video, canvas, svg, iframe')].some(e => shown(e) && areaPct(e) > 2);
  out.blank = bodyText.length < 20 && !media;

  // ---- verdict -------------------------------------------------------------

  const banner = [], unsure = [];
  if (out.consent_url) banner.push('consent page URL ' + u.hostname + u.pathname);
  if (out.tcf && out.tcf.displayStatus === 'visible') banner.push('TCF ping: displayStatus visible, cmpId ' + out.tcf.cmpId);
  for (const c of out.cmp_visible) banner.push(`${c.cmp} ${c.element} on top, ${c.area_pct}% of viewport: "${c.text.slice(0, 80)}"`);
  for (const o of out.overlays) {
    if (o.consent && (o.area_pct >= 1 || o.coverage >= 1)) {
      banner.push(`${o.position} ${o.element}${o.cmp ? ' (' + o.cmp + ')' : ''} covers ${o.coverage}% [${o.words.join(', ')}]: "${o.text.slice(0, 80)}"`);
    }
  }
  // In-flow notices only make the verdict "unsure". The first validation run
  // had youtube.com's "Your YouTube history is off ... accept the use of
  // cookies" box as one, which is page content and covers nothing.
  for (const i of out.inline) unsure.push(`inline ${i.element} ${i.area_pct}% [${i.words.join(', ')}]: "${i.text.slice(0, 80)}"`);

  // Sticky elements sit in the flow of the page and cover nothing at the top
  // of it, so without consent words they do not count. stackoverflow.com has
  // a sticky ad column.
  for (const o of out.overlays) {
    if (o.consent || o.shell || o.position === 'sticky') continue;
    if (o.coverage >= 25) unsure.push(`${o.position} ${o.element} covers ${o.coverage}% without consent words: "${o.text.slice(0, 80)}"`);
    // A cross-origin iframe has no text to read, so a big one on top is
    // doubtful (an interstitial ad, an unknown CMP). The size is the iframe's
    // own: reddit.com's fixed sign-up column holds a small Google button.
    else if (o.iframe_pct >= 10 && o.coverage >= 10) unsure.push(`${o.position} iframe overlay ${o.iframe_pct}%: ${o.iframes[0]}`);
  }
  if (out.scroll_locked && (out.cmps.length || out.overlays.some(o => !o.shell && o.coverage >= 5))) {
    unsure.push('scroll locked on html/body');
  }
  if (out.bot_wall) unsure.push('bot wall or error page: ' + out.bot_wall);
  if (out.blank) unsure.push('page looks blank');

  if (banner.length) out.verdict = 'banner';
  else if (unsure.length) out.verdict = 'unsure';
  out.evidence = banner.length ? banner.concat(unsure.map(s => 'also: ' + s)) : unsure;
  out.evidence = out.evidence.slice(0, 6);
  return out;
}
