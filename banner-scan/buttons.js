// Find the buttons of a consent UI in one document and say what each one does.
//
// clicks.py sends this file through Marionette's WebDriver:ExecuteScript with
// "return findButtons(arguments[0]);" appended. It runs once in the top
// document and once in every visible iframe on top, because the consent UIs of
// Sourcepoint, TrustArc and others live in cross-origin iframes. clicks.py
// switches into each frame with WebDriver:SwitchToFrame and adds the frame's
// offset to the coordinates found here.
//
// It works in three steps.
//
//   1. Consent containers. Elements that hold a consent UI: the fixed, sticky
//      or high z-index overlays on top (found with a grid of elementFromPoint
//      probes, as in detect.js), the banner elements of known CMPs, open
//      dialogs, and elements with cookie, consent, gdpr or cmp in their id or
//      class. Each one counts only if its text has consent wording. In a frame
//      that clicks.py marked as a consent frame, the whole body counts.
//   2. Candidates. Everything clickable inside a container (button, a,
//      [role=button], input buttons, onclick, cursor: pointer), visible, with
//      its click point inside the viewport and actually on top there. The test
//      for "on top" is elementFromPoint, descending into open shadow roots.
//   3. Labels. The visible text, then aria-label, title and value, normalised
//      and short, are matched against a vocabulary in about 25 languages and
//      sorted into reject, save, accept, subscribe, settings and close.
//
// The result is plain JSON with client coordinates. Screen coordinates are
// window.mozInnerScreenX + clientX and window.mozInnerScreenY + clientY at a
// devicePixelRatio of 1, and clicks.py computes them.

function findButtons(opts) {
  opts = opts || {};
  const VW = window.innerWidth;
  const VH = window.innerHeight;
  const out = {
    url: location.href,
    title: document.title,
    inner_screen: [window.mozInnerScreenX, window.mozInnerScreenY],
    viewport: [VW, VH],
    dpr: window.devicePixelRatio,
    scroll_locked: false,
    consent_frame: !!opts.consentFrame,
    containers: [],
    candidates: [],
    frames: [],
    toggles: { total: 0, on: 0, on_labels: [] },
    price_offer: '',
    cover_pct: 0,
    offscreen: 0,
  };
  if (!document.body || !VW || !VH) return out;

  // ---- vocabulary ----------------------------------------------------------

  // B and E are word boundaries that also work for letters outside ASCII.
  // Scripts without spaces between words (Chinese, Japanese, Thai) get no
  // boundaries, see re() below.
  const B = '(?<![\\p{L}\\p{N}])';
  const E = '(?![\\p{L}\\p{N}])';
  // A term starting with "=" must be the whole label. A term starting with "^"
  // must start the label. Other Latin, Greek and Cyrillic terms need a word
  // boundary on their left, so "accept" does not match "unacceptable". A term
  // ending in "$" needs a boundary on its right too.
  function re(terms) {
    const parts = terms.map(t => {
      if (t.startsWith('=')) return '^' + t.slice(1) + '$';
      if (t.startsWith('^')) return '^' + t.slice(1);
      const end = t.endsWith('$') ? E : '';
      const body = t.endsWith('$') ? t.slice(0, -1) : t;
      const spaced = /^[\p{Script=Latin}\p{Script=Greek}\p{Script=Cyrillic}0-9'’ ]/u.test(body);
      return (spaced ? B : '') + body + end;
    });
    return new RegExp(parts.join('|'), 'iu');
  }

  // Consent wording of a container, from detect.js and extended. One STRONG
  // word, or two different WEAK ones.
  const STRONG = re([
    'cookie', 'consent', 'gdpr', 'dsgvo', 'einwillig', 'datenschutzeinstellung', 'consentement', 'traceurs',
    'consentimiento', 'consentimento', 'consenso', 'toestemming', 'zgod[aęy]', 'ciasteczk', 'согласи', 'куки',
    'cookie-файл', 'クッキー', '同意', '쿠키', 'çerez', 'kvkk', 'souhlas', 'súhlas', 'hozzájárul', 'süti',
    'samtyk', 'samtyck', 'evästee', 'suostum', 'συγκατάθεσ', 'consimț', 'privacy choices', 'your privacy',
    'ihre privatsphäre', 'votre vie privée', 'su privacidad', 'la tua privacy', 'uw privacy',
    'ความยินยอม', 'คุกกี้', 'ملفات تعريف الارتباط', 'कुकी', 'persetujuan',
  ]);
  const WEAK = /privacy|datenschutz|privacidad|privacidade|confidentialité|riservatezza|prywatno|конфиденциальн|プライバシー|隐私|隱私|개인정보|gizlilik|personal data|personenbezogen|données personnelles|dati personali|datos personales|partner|tracking|werbung|advertis|publicité|publicidad|pubblicità/gi;
  function consentText(text) {
    if (STRONG.test(text)) return true;
    const weak = new Set((text.match(WEAK) || []).map(s => s.toLowerCase()));
    return weak.size >= 2;
  }

  // Price per period: the paid option of a pay-or-consent wall.
  const PRICE = /(\d+([.,]\d{1,2})?\s?(€|eur|euro|\$|£|chf|kr|sek|nok|dkk|zł|kč|ft)|(€|\$|£)\s?\d+[.,]?\d*)[^.]{0,40}?(monat|month|mois|mese|mes\b|mês|maand|miesi|месяц|ay\b|měsíc|hónap|månad|måned|kuukau|μήνα|月|월|tháng|bulan|เดือน|شهر|महीन|woche|week|semaine|jahr|year|an\b|año|anno)/i;

  const NEG = re([
    "don'?t$", 'do not$', 'not$', 'nicht$', 'kein', 'ne pas$', "n'accepte", 'no$', 'non$', 'não$', 'niet$', 'nie$',
    'не$', 'nem$', 'inte$', 'ikke$', 'ei$', 'δεν$', 'nesouhlas', 'không', 'tidak', 'しない', '不', '않', 'ไม่', 'لا', 'नहीं',
  ]);
  const ONLY = re([
    'only$', 'nur$', 'uniquement', 'seulement', 'solo$', 'sólo$', 'solamente', 'somente', 'apenas', 'alleen',
    'tylko', 'только', 'sadece', 'yalnızca', 'pouze', 'jen$', 'iba$', 'csak', 'endast', 'bara$', 'kun$', 'bare$',
    'vain$', 'μόνο', 'のみ', 'だけ', '仅', '僅', '只', '만', 'chỉ', 'hanya', 'เฉพาะ', 'فقط', 'केवल',
  ]);
  const NECESSARY = re([
    'necessary', 'essential', 'required', 'strictly', 'functional', 'technical', 'notwendig', 'erforderlich',
    'essenziell', 'essentiell', 'technisch', 'nécessaire', 'essentiel', 'indispensable', 'necesari', 'imprescindible',
    'necessar', 'essenzial', 'tecnic', 'necessári', 'essencia', 'noodzakelijk', 'essentieel', 'functionele',
    'niezbędn', 'wymagan', 'необходим', 'обязательн', 'gerekli', 'zorunlu', 'nezbytn', 'nutn', 'szükséges',
    'nödvändig', 'nødvendig', 'välttämät', 'απαραίτητ', '必要', '必須', '필수', 'cần thiết', 'diperlukan',
    'จำเป็น', 'الضرورية', 'आवश्यक',
  ]);
  const REJECT = re([
    'reject', 'decline', 'deny$', 'refuse', 'disagree', 'opt[ -]?out', 'no,? thanks', 'continue without',
    'ablehnen', 'ablehnung', 'verweiger', 'ohne (zustimmung|einwilligung|akzeptieren)', 'nein,? danke', 'widersprech', 'widerruf', 'withdraw', 'revoke',
    'refuser', 'je refuse', 'continuer sans', 'non merci',
    'rechaz', 'deneg', 'continuar sin', 'rifiut', 'nega$', 'continua senza', 'rejeit', 'recus', 'continuar sem',
    'weiger', 'afwijz', 'doorgaan zonder', 'odrzu', 'kontynuuj bez', 'отклон', 'отказ', 'запрет',
    'reddet', 'odmít', 'odmietn', 'elutasít', 'avvis', 'avslå', 'avböj', 'neka$', 'afvis', 'afslå', 'nej tak',
    'nei takk', 'hylkää', 'kiellä', 'en hyväksy', 'απόρριψ', 'απορρίπτ', 'từ chối', 'tolak', 'ปฏิเสธ', 'رفض', 'أرفض', 'अस्वीकार',
    '拒否', '拒绝', '拒絕', '同意しない', '不同意', '不接受', '거부', '거절', '동의하지 않', '동의 안',
  ]);
  const ACCEPT = re([
    'accept(?!ab|anc)', 'agree(?!ment)', 'allow', 'consent$', 'akzeptier', 'zustimm', 'stimme zu', 'einverstanden', 'erlaub',
    'annehmen', 'einwillig', 'accepter', "j'accepte", 'accepte$', 'autoriser', "d'accord", 'acept', 'permitir',
    'consentir', 'consiento', 'accett', 'acconsent', 'consenti', 'aceit', 'concordo', 'accepteer', 'accepteren',
    'akkoord', 'toestaan', 'toestemming geven', 'akceptuj', 'zaakceptuj', 'zgadzam', 'zgoda$', 'zezwól', 'zezwalam',
    'przejdź do serwisu', 'przejdź do strony', 'принять', 'принима', 'соглас', 'разреш', 'kabul', 'izin ver',
    'přijmout', 'přijímám', 'souhlasím', 'povolit', 'prijať', 'súhlasím', 'elfogad', 'egyetért', 'engedélyez',
    'hozzájárulok', 'acceptera', 'godkänn', 'tillåt', 'godta', 'aksepter', 'tillat', 'acceptér', 'tillad',
    'hyväksy', 'salli', 'αποδοχ', 'αποδέχ', 'συμφων', 'chấp nhận', 'đồng ý', 'cho phép', 'terima', 'setuju',
    'izinkan', 'ยอมรับ', 'อนุญาต', 'قبول', 'أقبل', 'أوافق', 'موافق', 'السماح', 'स्वीकार', 'सहमत',
    '同意', '接受', '允许', '允許', '承諾', '許可', '受け入れ', '동의', '수락', '허용',
    // the accept button of a pay-or-consent wall
    'with (ads|advertising)', 'mit werbung', 'avec (pub|publicité)', 'con pubblicità', 'con publicidad',
    'com publicidade', 'met advertenties', 'z reklamami', 'с рекламой', 'reklamlı', 's reklamou', 'reklámmal',
    'med (annonser|reklame)', 'mainoksilla',
  ]);
  // Words that only mean "accept" when they are the whole label.
  const ACCEPT_WEAK = re([
    '=(ok|okay|okey|oké|okej|ок|ok!|ok,? got it)', '=got it!?', '=i understand', '=understood', '=continue',
    '=(alright|fine|sounds good|yes|yes,? i agree|that\'s ok)', '=verstanden', '=alles klar', '=weiter',
    '=(ja|oui|sì|sí|sim|tak|да|evet|ano|igen|kyllä|ναι)', "=j'ai compris", '=compris', '=entendido', '=entendi',
    '=ho capito', '=va bene', '=prima', '=begrepen', '=rozumiem', '=понятно', '=хорошо', '=anladım', '=tamam',
    '=rozumím', '=rendben', '=jag förstår', '=forstået', '=greit', '=selvä', '=ymmärrän', '=εντάξει',
    '=(了解|了解しました|知道了|确定|確定|確認|好的|확인|알겠습니다|mengerti|baik|ตกลง|حسنا|ठीक है|đã hiểu)',
  ]);
  const ALL = re([
    'all$', 'alle$', 'alles$', 'tout$', 'tous$', 'toutes$', 'todo$', 'todas$', 'todos$', 'tutto$', 'tutti$',
    'tudo$', 'allen$', 'wszystk', 'все$', 'всё$', 'tümü', 'hepsi', 'vše$', 'všechn', 'összes', 'mind', 'alla$',
    'kaikki', 'όλα', 'όλων', 'すべて', '全部', '全て', '모두', 'tất cả', 'semua', 'ทั้งหมด', 'الكل', 'الجميع', 'सभी',
  ]);
  const SAVE = re([
    'save', 'speicher', 'enregistr', 'sauvegard', 'guardar', 'salva', 'salvar', 'opslaan', 'zapisz', 'сохран',
    'kaydet', 'uložit', 'uložiť', 'mentés', 'spara', 'lagre', 'gem$', 'tallenna', 'αποθήκευ', 'lưu', 'simpan',
    'บันทึก', 'حفظ', 'सहेज', '保存', '儲存', '저장',
    'confirm', 'bestätig', 'valider', 'confirmer', 'confirmar', 'conferma', 'bevestig', 'potwierdź', 'подтверд',
    'onayla', 'potvrd', 'megerősít', 'bekräfta', 'bekreft', 'bekræft', 'vahvista', 'επιβεβαί', 'xác nhận',
    'konfirmasi', 'ยืนยัน', 'تأكيد', 'पुष्टि', '确认', '確定する', 'submit',
  ]);
  // "Accept selection" and "Allow selection" confirm the toggles as they are.
  const SELECTION = re([
    'selection', 'selected', 'my choices', 'auswahl', 'ausgewählt', 'sélection', 'mes choix', 'selección',
    'seleccionad', 'selezion', 'seleção', 'selecionad', 'selectie', 'geselecteerd', 'wybran', 'wybór', 'выбран',
    'seçim', 'seçil', 'výběr', 'vybran', 'kiválasztott', 'valda', 'valgte', 'valitut', 'επιλεγμέν', 'επιλογή',
    '選択', '选择', '選擇', '선택',
  ]);
  const SUBSCRIBE = re([
    'subscri', 'abo$', 'abonn', 'pur[ -]?abo', 'pur$', 'werbefrei', 'ohne werbung', 'ad[ -]?free', 'without (ads|advertising)',
    'no ads', 'sans pub', 'suscri', 'sin publicidad', 'sin anuncios', 'abbona', 'senza pubblicità', 'assin',
    'sem publicidade', 'sem anúncios', 'zonder advertenties', 'advertentievrij', 'subskryb', 'prenumer',
    'bez reklam', 'подпис', 'без реклам', 'abone', 'reklamsız', 'předplat', 'előfiz', 'reklámmentes',
    'utan annonser', 'reklamfri', 'uten reklame', 'uden reklamer', 'tilaa', 'mainokseton', 'ilman mainoksia',
    'συνδρομ', 'contentpass', 'consentless', 'đăng ký', 'berlangganan', 'สมัครสมาชิก', 'اشترك', 'सदस्यता',
    '購読', '订阅', '訂閱', '구독', 'pay$', 'bezahl', 'kaufen', 'buy$',
    // a paid alternative written on the button: aftonbladet.se's "reject" is one
    'betaltjänst', 'betalingstjeneste', 'betalt tjeneste', 'paid service', 'kostenpflichtig', 'payant', 'a pagamento',
    'maksullinen',
  ]);
  const SETTINGS = re([
    'setting', 'preferenc', 'manage', 'customi[sz]', 'personali[sz]', 'configur', 'options', 'more choices',
    'let me choose', 'purposes', 'show details', 'details$', 'einstellung', 'anpassen', 'verwalten', 'präferenz',
    'optionen', 'zwecke', 'individuell', 'konfigur', 'paramètr', 'paramétr', 'personnalis', 'gérer', 'préférences',
    "plus d'options", 'choisir', 'ajustes', 'gestion', 'administrar', 'más opciones', 'opciones', 'impostazion',
    'personalizza', 'gestisci', 'preferenze', 'opzioni', 'definições', 'gerir', 'gerenciar', 'preferências',
    'opções', 'instelling', 'aanpassen', 'beheren', 'voorkeur', 'opties', 'zelf instellen', 'ustawie', 'dostosuj',
    'zarządzaj', 'preferencj', 'opcje', 'więcej opcji', 'настро', 'управл', 'параметр', 'ayarlar', 'ayarla',
    'özelleştir', 'yönet', 'tercih', 'seçenek', 'nastaven', 'přizpůsob', 'spravovat', 'předvolb', 'možnost',
    'beállítás', 'testreszab', 'kezel', 'lehetőség', 'inställning', 'anpassa', 'hantera', 'alternativ',
    'innstilling', 'tilpass', 'administrer', 'indstilling', 'tilpas', 'præferencer', 'asetuk', 'mukauta',
    'hallinnoi', 'hallitse', 'vaihtoehto', 'ρυθμίσ', 'προσαρμογ', 'διαχείρισ', 'επιλογές', 'cài đặt',
    'tùy chỉnh', 'quản lý', 'tùy chọn', 'pengaturan', 'sesuaikan', 'kelola', 'opsi$', 'preferensi', 'ตั้งค่า',
    'ปรับแต่ง', 'จัดการ', 'إعدادات', 'الإعدادات', 'تخصيص', 'إدارة', 'خيارات', 'सेटिंग', 'प्रबंधित', 'अनुकूलित',
    'विकल्प', '設定', '设置', 'カスタマイズ', '管理', '自定义', '偏好', '详细', '詳細', '选项', 'オプション',
    '설정', '사용자 지정', '관리', '옵션',
  ]);
  const CLOSE = re([
    '=[×✕✖✗╳xX]', 'close', 'dismiss', 'schließen', 'schliessen', 'fermer', 'cerrar', 'chiudi', 'fechar',
    'sluiten', 'zamknij', 'закрыть', 'kapat', 'zavřít', 'zavrieť', 'bezár', 'stäng', 'lukk', 'luk$', 'sulje',
    'κλείσ', 'đóng', 'tutup', 'ปิด', 'إغلاق', 'बंद', '閉じる', '关闭', '關閉', '닫기',
  ]);
  // Sign-in prompts are not consent UIs, even when they mention the privacy
  // policy. Google's One Tap sat over linkedin.com and stackoverflow.com.
  const SIGNIN = /accounts\.google\.com\/gsi|credential_picker|one[-_ ]?tap|sign in with google|appleid\.apple\.com|facebook\.com\/plugins/i;
  const IFRAME_CMP = /sp_message|privacy-mgmt|sourcepoint|consensu\.org|consent|\bcmp|cookie|gdpr|trustarc|truste|fundingchoices|didomi|usercentrics|onetrust|cookielaw|quantcast|evidon|cookiebot|privacy/i;

  // The kind of a label, in this order: subscribe before reject ("weiter ohne
  // Werbung" is an offer, not a refusal), reject before accept ("do not
  // accept", "accept only necessary"), save before accept ("accept
  // selection"), settings before accept ("cookie settings").
  function classify(raw) {
    const t = raw.toLowerCase().replace(/[\s ]+/g, ' ').replace(/^[\s"'“”‘’«»›‹>→←·•*:!.,]+|[\s"'“”‘’«»›‹>→←·•*:!.,]+$/g, '');
    if (!t || t.length > 70) return null;
    // Button labels are short. Longer ones are purpose and vendor names in a
    // settings layer, such as the TCF purpose "Speichern von oder Zugriff auf
    // Informationen auf einem Endgerät" on chefkoch.de, which starts with a
    // save word.
    const words = t.split(' ').length;
    if (words > 8) return { kind: '', strength: 0, norm: t };
    const accept = ACCEPT.test(t);
    if (SUBSCRIBE.test(t) && !/^(log ?in|sign ?in|anmelden|einloggen|connexion|se connecter|accedi|iniciar sesión|entrar)/.test(t)) {
      return { kind: 'subscribe', strength: 1, norm: t };
    }
    if (REJECT.test(t)) return { kind: 'reject', strength: 3, norm: t };
    if (accept && NEG.test(t)) return { kind: 'reject', strength: 3, norm: t };
    if (NECESSARY.test(t) && (ONLY.test(t) || accept || SAVE.test(t))) return { kind: 'reject', strength: 2, norm: t };
    if (SAVE.test(t) && words <= 5) {
      // "Accept all and save" is an accept.
      if (accept && ALL.test(t)) return { kind: 'accept', strength: 3, norm: t };
      return { kind: 'save', strength: 2, norm: t };
    }
    if (accept && SELECTION.test(t)) return { kind: 'save', strength: 2, norm: t };
    // "Allen Zwecken zustimmen" names purposes and is still an accept.
    if (accept && ALL.test(t)) return { kind: 'accept', strength: 3, norm: t };
    if (SETTINGS.test(t)) return { kind: 'settings', strength: 1, norm: t };
    if (accept) return { kind: 'accept', strength: ALL.test(t) ? 3 : 2, norm: t };
    if (ACCEPT_WEAK.test(t)) return { kind: 'accept', strength: 1, norm: t };
    if (CLOSE.test(t)) return { kind: 'close', strength: 1, norm: t };
    return { kind: '', strength: 0, norm: t };
  }

  // ---- DOM helpers (as in detect.js) ------------------------------------------

  const styles = new Map();
  function style(el) {
    let s = styles.get(el);
    if (!s) { s = getComputedStyle(el); styles.set(el, s); }
    return s;
  }
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
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE' || tag === 'SVG' || tag === 'svg') return;
        if (style(node).display === 'none') return;
        if (tag === 'INPUT' && /^(button|submit)$/i.test(node.type) && node.value) parts.push(node.value);
        if (node.shadowRoot) { walk(node.shadowRoot); return; }
        if (tag === 'SLOT') {
          const assigned = node.assignedNodes({ flatten: true });
          if (assigned.length) { for (const c of assigned) walk(c); return; }
        }
      } else if (node.nodeType !== 11) {
        return;
      }
      for (const c of node.childNodes) walk(c);
    })(root);
    return parts.join(' ').slice(0, max);
  }
  // Every element below root in the composed tree: light DOM and open shadow
  // roots. Slotted elements are light DOM children of their host, so they are
  // found once.
  function deepAll(root, acc) {
    acc = acc || [];
    const base = root.shadowRoot && root.nodeType === 1 ? [root.shadowRoot, root] : [root];
    for (const r of base) {
      for (const el of r.querySelectorAll('*')) {
        acc.push(el);
        if (el.shadowRoot) deepAll(el.shadowRoot, acc);
      }
    }
    return acc;
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
  // Fixed, or absolute with a high z-index. Sticky elements are part of the
  // page (headers, ad columns) and a consent UI that is sticky is found by its
  // id or class instead.
  function overlayRoot(el) {
    for (let e = el; e && e !== document.body && e !== document.documentElement; e = parentOf(e)) {
      const s = style(e);
      if (s.position === 'fixed') return e;
      if (s.position === 'absolute' && parseInt(s.zIndex, 10) >= 1000) return e;
    }
    return null;
  }
  function round(n) { return Math.round(n * 10) / 10; }

  // ---- 1. consent containers -------------------------------------------------

  const CMP_SEL = [
    '#onetrust-banner-sdk', '#onetrust-pc-sdk', '#CybotCookiebotDialog', '#didomi-popup', '#didomi-notice',
    '#usercentrics-root', '#usercentrics-cmp-ui', '[id^="sp_message_container_"]', '.fc-consent-root',
    '#cmpbox', '#cmpbox2', '#qc-cmp2-container', '#truste-consent-track', '#consent_blackbar',
    '.truste_box_overlay', '.cky-consent-container', '.cky-modal', '#BorlabsCookieBox', '.cmplz-cookiebanner',
    '#iubenda-cs-banner', '.osano-cm-window', '#axeptio_overlay', '.klaro', '#cc-main', '#cc--main', '#cc_div',
    '.cc-window', '#termly-code-snippet-support', '#ccc', '#sd-cmp', '#lanyard_root', '#_evidon_banner',
    '#_evidon-barrier-wrapper', '#footer_tc_privacy', '#tc-privacy-wrapper', '#coiOverlay', '.cookiefirst-root',
    '#hs-eu-cookie-confirmation', '#shopify-pc__banner', '#ez-cookie-dialog-wrapper', '#truendo_container',
    '.ccm-root', '[data-hook="consent-banner-root"]', '#wcpConsentBannerCtrl', '#sp-cc', '#sp-cc-wrapper',
    'ytd-consent-bump-v2-lightbox', '[data-testid="cookie-policy-manage-dialog"]', '[data-cookiebanner="banner"]',
    '#bbccookies', 'reddit-cookie-banner', '#pg-host-shadow-root', '#pg-root',
    '[role="dialog"]:has(#L2AGLb)', '[role="dialog"]:has(#W0wltc)',
  ].join(', ');
  const known = new Set(deepQueryAll(CMP_SEL));

  const candidatesC = new Set();
  // a. overlays on top, by a grid of probes
  const COLS = 24, ROWS = 16;
  const gridRoots = [];
  for (let i = 0; i < COLS; i++) {
    for (let j = 0; j < ROWS; j++) {
      const el = hitAt((i + 0.5) * VW / COLS, (j + 0.5) * VH / ROWS);
      const root = el ? overlayRoot(el) : null;
      gridRoots.push([el, root]);
      if (root) candidatesC.add(root);
    }
  }
  for (const y of [4, 14, VH - 14, VH - 4]) {
    for (let i = 0; i < COLS; i++) {
      const el = hitAt((i + 0.5) * VW / COLS, y);
      const root = el ? overlayRoot(el) : null;
      if (root) candidatesC.add(root);
    }
  }
  // b. known CMP banners, c. dialogs, d. id or class
  for (const el of known) candidatesC.add(el);
  for (const el of deepQueryAll('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]')) candidatesC.add(el);
  const idSel = '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], [id*="gdpr" i], [class*="gdpr" i], [id*="cmp" i], [id*="privacy" i], [class*="privacy" i]';
  for (const el of deepQueryAll(idSel).slice(0, 400)) candidatesC.add(el);

  function hostShown(el) {
    if (shown(el)) return true;
    // display: contents hosts (Usercentrics) have no box of their own.
    return !!(el.shadowRoot && style(el).display === 'contents');
  }
  const containers = [];
  if (opts.consentFrame) {
    containers.push({ el: document.body, why: 'consent frame' });
  }
  for (const el of candidatesC) {
    if (!el || el === document.body || el === document.documentElement) continue;
    if (!hostShown(el)) continue;
    if (SIGNIN.test((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : ''))) continue;
    const text = deepText(el, 6000);
    const isKnown = known.has(el);
    const isDialog = el.matches('dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"]');
    const why = isKnown ? 'cmp' : isDialog ? 'dialog' : (overlayRoot(el) === el ? 'overlay' : 'element');
    // Two weak words are enough for a CMP element or a dialog. An overlay or
    // an element with a telling id needs a strong one, because fixed headers
    // and page wrappers mention privacy and advertising too.
    if (!isKnown && !(isDialog ? consentText(text) : STRONG.test(text))) continue;
    // A container that holds the whole page (an app shell, a wrapper with
    // "cookie" in its class) is not a consent UI, unless a known CMP says so
    // or it starts with consent wording. A settings layer with hundreds of
    // vendors is long and full of links too, and corriere.it's starts with
    // "Authorize or deny the collection of your data ... cookie".
    const links = el.querySelectorAll('a[href]').length;
    const opens = STRONG.test(text.slice(0, 1500));
    if (!isKnown && !opens && text.length > 5000 && links > 40) continue;
    if (why === 'element' && ((text.length > 3000 && !opens) || areaPct(el) > 60)) continue;
    containers.push({ el, why, text });
  }
  // In a frame that is not marked as a consent frame, the body can still be
  // one when the frame holds nothing else (a CMP served from its own host).
  if (!opts.consentFrame && opts.inFrame) {
    const text = deepText(document.body, 8000);
    if (text.length < 8000 && STRONG.test(text)) containers.push({ el: document.body, why: 'frame text', text });
  }
  // Keep the outermost of nested containers. The outer one is what goes away
  // when the UI closes, and its candidates include the inner ones'.
  const outer = containers.filter(c => !containers.some(o => o !== c && o.el !== c.el && contains(o.el, c.el)));
  const seenEl = new Set();
  const finalC = [];
  for (const c of outer) {
    if (seenEl.has(c.el)) continue;
    seenEl.add(c.el);
    finalC.push(c);
  }

  // ---- 2. candidates ---------------------------------------------------------

  function clickable(el) {
    const tag = el.tagName;
    if (tag === 'BUTTON' || tag === 'A' || tag === 'SUMMARY') return true;
    if (tag === 'INPUT') return /^(button|submit)$/i.test(el.type);
    const role = el.getAttribute('role');
    if (role === 'switch' || role === 'checkbox' || role === 'tab') return false;
    if (role === 'button' || role === 'link' || role === 'menuitem') return true;
    if (el.hasAttribute('onclick')) return true;
    if (style(el).cursor === 'pointer') {
      const p = parentOf(el);
      return !p || p.nodeType !== 1 || style(p).cursor !== 'pointer';
    }
    return false;
  }
  function labelsOf(el) {
    const ls = [];
    const text = deepText(el, 200).trim();
    if (text) ls.push(text);
    for (const a of ['aria-label', 'title']) {
      const v = el.getAttribute(a);
      if (v && v.trim()) ls.push(v.trim());
    }
    if (el.tagName === 'INPUT' && el.value) ls.push(el.value);
    return ls;
  }
  // The point to click: the centre if the element is on top there, otherwise
  // the first of a few other points that is. Integer client coordinates.
  function clickPoint(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    const tries = [[0.5, 0.5], [0.3, 0.5], [0.7, 0.5], [0.5, 0.3], [0.5, 0.7], [0.15, 0.5], [0.85, 0.5]];
    for (const [fx, fy] of tries) {
      const x = Math.floor(r.left + r.width * fx), y = Math.floor(r.top + r.height * fy);
      if (x < 1 || y < 1 || x > VW - 2 || y > VH - 2) continue;
      const hit = hitAt(x, y);
      if (hit && (hit === el || contains(el, hit))) return [x, y];
    }
    return null;
  }

  const cands = [];
  finalC.forEach((c, ci) => {
    const text = c.text || deepText(c.el, 6000);
    const pct = areaPct(c.el);
    const info = {
      id: ci, element: describe(c.el), why: c.why, area_pct: pct,
      rect: (r => [round(r.left), round(r.top), round(r.width), round(r.height)])(c.el.getBoundingClientRect()),
      text: text.replace(/\s+/g, ' ').slice(0, 200), other_labels: [],
    };
    if (!out.price_offer) {
      const m = text.match(PRICE);
      if (m) out.price_offer = m[0].slice(0, 80);
    }
    const all = [c.el, ...deepAll(c.el)];
    for (const el of all) {
      if (el.nodeType !== 1) continue;
      // toggles, as they are
      const isBox = (el.tagName === 'INPUT' && el.type === 'checkbox') ||
        el.getAttribute('role') === 'switch' || el.getAttribute('role') === 'checkbox';
      if (isBox) {
        out.toggles.total++;
        const on = el.tagName === 'INPUT' ? el.checked : el.getAttribute('aria-checked') === 'true';
        const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
        if (on && !disabled) {
          out.toggles.on++;
          if (out.toggles.on_labels.length < 6) {
            const lab = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || el.id || el.name || '';
            out.toggles.on_labels.push(lab.replace(/\s+/g, ' ').trim().slice(0, 60));
          }
        }
        continue;
      }
      if (!clickable(el)) continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      let cls = null, label = '';
      for (const l of labelsOf(el)) {
        const k = classify(l);
        if (k && k.kind) { cls = k; label = l; break; }
        if (!label && k) label = l;
      }
      if (!cls) {
        if (label && info.other_labels.length < 8 && shown(el)) info.other_labels.push(label.replace(/\s+/g, ' ').slice(0, 50));
        continue;
      }
      if (!shown(el)) continue;
      const pt = clickPoint(el);
      if (!pt) { out.offscreen++; continue; }
      const r = el.getBoundingClientRect();
      const href = el.tagName === 'A' ? (el.getAttribute('href') || '') : '';
      const away = !!href && !/^(#|javascript:|$)/i.test(href.trim()) && (() => {
        try { const u = new URL(href, location.href); return u.href.split('#')[0] !== location.href.split('#')[0]; } catch (e) { return false; }
      })();
      cands.push({
        el, container: ci, kind: cls.kind, strength: cls.strength,
        label: label.replace(/\s+/g, ' ').trim().slice(0, 80),
        x: pt[0], y: pt[1], rect: [round(r.left), round(r.top), round(r.width), round(r.height)],
        tag: el.tagName.toLowerCase(), href_away: away, href: away ? href.slice(0, 120) : '',
      });
    }
    out.containers.push(info);
  });
  // A clickable that holds other candidates is a wrapper (corriere.it has a
  // div with a pointer cursor around "Deny All" and "Accept All"). Keep the
  // innermost ones.
  for (const c of cands) {
    if (cands.some(o => o !== c && o.el !== c.el && contains(c.el, o.el))) continue;
    const { el, ...rest } = c;
    out.candidates.push(rest);
  }

  // ---- 3. frames, scroll lock and cover --------------------------------------

  for (const f of deepQueryAll('iframe')) {
    if (!shown(f)) continue;
    const pct = areaPct(f);
    if (pct < 0.5) continue;
    const v = visibleRect(f);
    let hits = 0;
    for (const fx of [0.2, 0.5, 0.8]) {
      for (const fy of [0.2, 0.5, 0.8]) {
        if (hitAt(v.x0 + v.w * fx, v.y0 + v.h * fy) === f) hits++;
      }
    }
    if (!hits) continue;
    const r = f.getBoundingClientRect();
    const s = style(f);
    const left = r.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft);
    const top = r.top + parseFloat(s.borderTopWidth) + parseFloat(s.paddingTop);
    const label = [f.id, f.name, f.title, f.getAttribute('src')].filter(Boolean).join(' ').slice(0, 160);
    if (SIGNIN.test(label)) continue;
    out.frames.push({
      el: f, offset: [left, top], area_pct: pct, on_top: hits, label,
      cmp: IFRAME_CMP.test(label), in_container: finalC.some(c => contains(c.el, f)),
      rect: [round(r.left), round(r.top), round(r.width), round(r.height)],
    });
  }

  const html = style(document.documentElement), body = style(document.body);
  out.scroll_locked = html.overflow === 'hidden' || html.overflowY === 'hidden' ||
    body.overflow === 'hidden' || body.overflowY === 'hidden' || body.position === 'fixed';

  // Share of grid points where the consent UI, a consent frame or a backdrop
  // (an overlay with next to no text) is what the user sees.
  const consentFrames = out.frames.filter(f => f.cmp || f.in_container).map(f => f.el);
  const backdrop = new Map();
  function isBackdrop(root) {
    if (!backdrop.has(root)) {
      backdrop.set(root, areaPct(root) >= 25 && deepText(root, 300).length < 120 && !root.querySelector('img, video, iframe'));
    }
    return backdrop.get(root);
  }
  let covered = 0;
  for (const [el, root] of gridRoots) {
    if (!el) continue;
    if (finalC.some(c => contains(c.el, el))) { covered++; continue; }
    if (consentFrames.includes(el)) { covered++; continue; }
    if (root && finalC.length && isBackdrop(root)) covered++;
  }
  out.cover_pct = Math.round(1000 * covered / gridRoots.length) / 10;
  return out;
}

// Whether the given points of a frame element are on top in this document:
// clicks.py asks the parent of every frame it found candidates in.
function frameHits(frame, points) {
  function hitAt(x, y) {
    let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }
  return points.map(([x, y]) => hitAt(x, y) === frame);
}
